# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 carries the source's character set and collation into Aurora MySQL (#2276).

A MySQL job whose collection records the database's collation: the step 1 contract
carries it, ``step-1.ddl.sql`` gives it to the tables' database (and its own to a table
or column that differs on the source), and the routine scripts give the same clause to
every database they create, so the tables, the routines and their parameters compare
text alike (a mix fails a call with ERROR 1267, PR #2272 round 2). A collation the target
does not offer is refused with why and what to do; with none recorded, nothing changes.
Everything here runs through the writers a job runs (``write_step1``, ``load_step1``,
``render_ddl``, ``write_routine_scripts``), from a collection.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_control import CONTROL_SCHEMA
from src.tools.data_movement.dms_step import DmsStepError, load_step1, render_ddl
from src.tools.data_movement.plan import write_plans
from src.tools.data_movement.routine_scripts import DEPLOY_FILE, write_routine_scripts
from src.tools.transformation_contract.write import write_step1
from tests.unit.tools.data_movement.dms_step_fixtures import write_job
from tests.unit.tools.data_movement.routine_fixtures import (
    contract_with_routines,
    migration,
    mysql_source,
    row,
)

UNICODE = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
SERVER_DEFAULTS = "It sets no character set or collation: the cluster's server defaults apply."
EXISTING = ("A database that already exists keeps its own collation: CREATE DATABASE IF NOT EXISTS "
            "does not change it. If it holds nothing else yet, uncomment the ALTER DATABASE right "
            "after its CREATE DATABASE; it runs on a new database too.")  # fmt: skip
EDIT_DATABASE = ("edit the CREATE DATABASE statements in step-1.ddl.sql, routines-deploy.sql and "
                 "triggers-enable.sql together before you run them.")  # fmt: skip


def _job(
    tmp_path: Path,
    database: str | None = "utf8mb4_unicode_ci",
    charset: str | None = "utf8mb4",
    *,
    tables: dict[str, str] | None = None,
    columns: dict[tuple[str, str], tuple[str | None, str]] | None = None,
    version: str = "8.0.32",
) -> LocalArtifactStore:
    """``shop/job1`` (``write_job``'s three tables) with the collection recording the
    database's collation, ``tables``' own (by name) and ``columns``' own (by table and
    column: character set, collation); then the step 1 contract and plan."""
    store = write_job(tmp_path, contract=False, plan=False)
    key = "shop/job1/collector/output.json"
    collector = store.read_json(key)
    source = collector["metadata"]["source_database"]
    source["version"] = version
    if database is not None:
        source.update(default_character_set=charset, default_collation=database)
    for t in collector["database_schema"]["tables"]:
        t["collation"] = (tables or {}).get(t["table_name"], database)
        for c in t["columns"]:
            own = (columns or {}).get((t["table_name"], c["column_name"]))
            if own:
                c["character_set"], c["collation"] = own
    store.write_json(key, collector)
    write_step1(store, "shop", "job1")
    write_plans(store, "shop", "job1")
    return store


def _ddl(store: LocalArtifactStore) -> str:
    return render_ddl(load_step1(store, "shop", "job1"))


def _flat(text: str) -> str:
    """A script's ``--`` comment text as one line, so a wrapped sentence reads whole."""
    return " ".join(line[3:].strip() for line in text.splitlines() if line.startswith("-- "))


def _contract(store: LocalArtifactStore) -> dict[str, Any]:
    return store.read_json("shop/job1/transformation-contract/v2/step-1.json")


def _deploy(tmp_path: Path, store: LocalArtifactStore, schema: str = "shop_functions") -> str:
    """``routines-deploy.sql`` for one procedure in ``schema``, from the job's own contract."""
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.transformation_contract import StepContract
    from src.tools.routines.migration import routine_migration_path

    rows = [row(f"{schema}.p0", "procedure", schema=schema, name="p0", specific="p0",
                status="rewritten", signature="IN int")]  # fmt: skip
    artifact = migration(rows, "mysql")
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    sources = [mysql_source("p0", "CREATE DEFINER=`app`@`%` PROCEDURE `p0`(IN p INT)\nBEGIN\n  "
                            "SELECT p;\nEND", schema=schema, database_collation="utf8mb4_unicode_ci")]  # fmt: skip
    write_routine_sources(store, "shop", "job1",
                          {"metadata": {"source_database": {"engine": "mysql", "version": "8.0.32"}}}, sources)  # fmt: skip
    contract = StepContract.model_validate(_contract(store))
    step = tmp_path / "step"
    write_routine_scripts(store, "shop", "job1", contract_with_routines(artifact, contract=contract),
                          "a" * 64, step)  # fmt: skip
    return (step / DEPLOY_FILE).read_text()


def _triggers(tmp_path: Path, store: LocalArtifactStore, schema: str = "jobs") -> str:
    """``triggers-enable.sql`` for one deferred event in ``schema``, from the job's own contract."""
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.transformation_contract import StepContract
    from src.tools.data_movement.triggers_scripts import TRIGGERS_FILE, write_triggers_scripts
    from src.tools.routines.migration import routine_migration_path

    rows = [row(f"{schema}.ev0", "event", schema=schema, name="ev0", specific="ev0",
                status="rewritten")]  # fmt: skip
    artifact = migration(rows, "mysql")
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    sources = [mysql_source("ev0", "CREATE DEFINER=`app`@`%` EVENT `ev0` ON SCHEDULE EVERY 1 DAY "
                            "STARTS '2036-01-01 00:00:00' DISABLE DO SELECT 1", kind="event",
                            schema=schema)]  # fmt: skip
    write_routine_sources(store, "shop", "job1",
                          {"metadata": {"source_database": {"engine": "mysql", "version": "8.0.32"}}}, sources)  # fmt: skip
    contract = StepContract.model_validate(_contract(store))
    write_triggers_scripts(store, "shop", "job1", contract_with_routines(artifact, contract=contract),
                           "a" * 64, tmp_path / "step")  # fmt: skip
    return (tmp_path / "step" / TRIGGERS_FILE).read_text()


def _statements(text: str) -> str:
    """A script without its ``--`` comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("--"))


def _database_clauses(text: str) -> set[str]:
    return {
        options
        for name, options in re.findall(
            r"^CREATE DATABASE IF NOT EXISTS `((?:[^`]|``)+)`(.*);$", text, re.M
        )
        if name != CONTROL_SCHEMA  # DMS's own control database (#2133), not a tables' one
    }


def test_the_ddl_creates_the_tables_database_on_the_source_collation(tmp_path):
    ddl = _ddl(_job(tmp_path))
    assert f"CREATE DATABASE IF NOT EXISTS `shop` {UNICODE};\n" in ddl
    flat = _flat(ddl)
    assert SERVER_DEFAULTS not in flat
    # Only when the database does not exist yet: one that does keeps its own (review of #2282).
    assert ("It creates the database with CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci, the "
            "source's, when it does not exist yet") in flat  # fmt: skip
    assert EXISTING in flat
    # After its CREATE DATABASE, so uncommented in place it runs on a fresh cluster too.
    assert (f"CREATE DATABASE IF NOT EXISTS `shop` {UNICODE};\n-- ALTER DATABASE `shop` {UNICODE};\n"
            "USE `shop`;") in ddl  # fmt: skip
    # The tables take the database's: no table or column clause of their own.
    assert "DEFAULT CHARSET" not in ddl and _statements(ddl).count("COLLATE") == 1
    assert _contract(_job(tmp_path / "again"))["target"]["collation"] == {
        "character_set": "utf8mb4", "collation": "utf8mb4_unicode_ci"}  # fmt: skip


def test_the_routine_databases_get_the_ddls_clause_from_the_same_contract(tmp_path):
    store = _job(tmp_path)
    ddl, deploy = _ddl(store), _deploy(tmp_path, store)
    triggers = _triggers(tmp_path / "t", store)
    clauses = _database_clauses(ddl)
    assert clauses == {f" {UNICODE}"}
    assert _database_clauses(deploy) == _database_clauses(triggers) == clauses
    sentence = re.search(r"It creates the database .*? keeps its own\.", _flat(ddl))
    assert sentence and sentence.group(0) in _flat(deploy) and sentence.group(0) in _flat(triggers)
    # Each script names the ALTER DATABASE for each database it creates.
    for text, name in ((ddl, "shop"), (deploy, "shop_functions"), (triggers, "jobs")):
        assert EXISTING in _flat(text)
        assert (f"CREATE DATABASE IF NOT EXISTS `{name}` {UNICODE};\n"
                f"-- ALTER DATABASE `{name}` {UNICODE};\n") in text  # fmt: skip


def test_a_table_and_a_column_that_differ_on_the_source_keep_their_own(tmp_path):
    store = _job(tmp_path, tables={"customers": "latin1_swedish_ci"},
                 columns={("audit_log", "msg"): ("utf8mb4", "utf8mb4_bin")})  # fmt: skip
    ddl = _ddl(store)
    assert "`msg` VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin" in ddl
    customers = re.search(r"CREATE TABLE `customers` \(.*?\n\)[^;]*;", ddl, re.S)
    assert customers and customers.group(0).endswith(
        "\n) DEFAULT CHARSET=latin1 COLLATE=latin1_swedish_ci;"
    )
    orders = re.search(r"CREATE TABLE `orders` \(.*?\n\)[^;]*;", ddl, re.S)
    assert orders and orders.group(0).endswith("\n);")
    assert "Own collations" in _flat(ddl) and "shop.customers, shop.audit_log.msg" in _flat(ddl)


def test_a_non_text_column_never_gets_a_collation(tmp_path):
    ddl = _ddl(_job(tmp_path, columns={("orders", "id"): ("utf8mb4", "utf8mb4_bin")}))
    assert "`id` BIGINT CHARACTER SET" not in ddl and _statements(ddl).count("COLLATE") == 1


@pytest.mark.parametrize(
    ("collation", "charset", "said"),
    [
        ("utf8mb3_general_ci", "utf8mb3", "utf8mb3, MySQL's deprecated 3-byte utf8"),
        ("utf8_general_ci", "utf8", "utf8mb3, MySQL's deprecated 3-byte utf8"),
        # MariaDB's: utf8mb4 collations Aurora MySQL does not have, with the closest it does.
        (
            "utf8mb4_uca1400_ai_ci",
            "utf8mb4",
            "Aurora MySQL offers no collation named "
            "utf8mb4_uca1400_ai_ci; utf8mb4_0900_ai_ci is the closest one it offers",
        ),
        (
            "utf8mb4_unicode_520_nopad_ci",
            "utf8mb4",
            "Aurora MySQL offers no collation named "
            "utf8mb4_unicode_520_nopad_ci; utf8mb4_unicode_520_ci is the closest one it offers",
        ),
        # The NO PAD binary match; a PAD SPACE to NO PAD change says what it means.
        ("utf8mb4_nopad_bin", "utf8mb4", "utf8mb4_0900_bin is the closest one it offers"),
        (
            "utf8mb4_uca1400_ai_ci",
            "utf8mb4",
            "with it, trailing spaces count in comparisons and unique keys",
        ),
        (
            "utf8mb4_unicode_520_nopad_ci",
            "utf8mb4",
            "with it, trailing spaces stop counting in comparisons and unique keys",
        ),
        (
            "utf8mb4_uca1400_swedish_ai_ci",
            "utf8mb4",
            "utf8mb4_sv_0900_ai_ci is the closest one it offers",
        ),
        (
            "latin2_general_ci",
            "latin2",
            "is not a utf8mb4, latin1 or ascii collation step 1 carries",
        ),
        ("utf8mb4_bg_0900_ai_ci", "utf8mb4", "is not on every Aurora MySQL release for MySQL 8.0"),
        ("latin1_swedish_ci", "utf8mb4", "which is a latin1 collation"),
        ("latin1_swedish_ci", "x\n-- y", "as another character set with latin1_swedish_ci"),
        (
            "utf8mb4_bin;DROP DATABASE x",
            "utf8mb4",
            "a collation name that is not a plain identifier",
        ),
    ],
)
def test_a_database_collation_the_target_does_not_offer_keeps_the_server_defaults_and_says_why(
    tmp_path, collation, charset, said
):
    store = _job(tmp_path, collation, charset)
    ddl, deploy = _ddl(store), _deploy(tmp_path, store)
    assert "COLLATE" not in ddl and "CHARACTER SET" not in ddl.replace("no character set", "")
    assert _database_clauses(deploy) == _database_clauses(ddl) == {""}
    flat = _flat(ddl)
    assert SERVER_DEFAULTS in flat and said in flat
    # What the database gets instead, and which statements to edit for another collation.
    assert "so the database gets the cluster's server default instead" in flat
    assert EDIT_DATABASE in flat
    assert "ALTER DATABASE" not in ddl and "ALTER DATABASE" not in deploy
    assert "DROP DATABASE" not in ddl and "DROP DATABASE" not in deploy
    assert said in _flat(deploy) and EDIT_DATABASE in _flat(deploy)


def test_a_collation_newer_than_aurora_mysql_3_is_carried_on_the_8_4_line(tmp_path):
    ddl = _ddl(_job(tmp_path, "utf8mb4_bg_0900_ai_ci", version="8.4.3"))
    assert (
        "CREATE DATABASE IF NOT EXISTS `shop` CHARACTER SET utf8mb4 COLLATE utf8mb4_bg_0900_ai_ci;"
        in ddl
    )


def test_a_table_or_column_collation_the_target_does_not_offer_falls_back_with_a_warning(tmp_path):
    store = _job(tmp_path, tables={"customers": "utf8mb3_general_ci"},
                 columns={("audit_log", "msg"): ("utf8mb4", "utf8mb4_uca1400_ai_ci")})  # fmt: skip
    ddl = _ddl(store)
    assert "DEFAULT CHARSET" not in ddl and "`msg` VARCHAR(200) NOT NULL" in ddl
    flat = _flat(ddl)
    assert "Collations not carried:" in flat
    assert "shop.customers: The table is on utf8mb3_general_ci" in flat
    assert "so the table gets its database's utf8mb4_unicode_ci instead" in flat
    assert "edit that table's CREATE TABLE in step-1.ddl.sql" in flat
    assert (
        "shop.audit_log.msg: Aurora MySQL offers no collation named utf8mb4_uca1400_ai_ci" in flat
    )
    assert "so the column gets its table's utf8mb4_unicode_ci instead" in flat
    assert "edit that column's definition in step-1.ddl.sql" in flat
    findings = {(f["table_id"], f.get("column")): f["severity"] for r in _contract(store)["rules"]
                for f in r["findings"] if f["code"] == "collation_not_carried"}  # fmt: skip
    assert findings == {("shop.customers", None): "warning", ("shop.audit_log", "msg"): "warning"}


@pytest.mark.passes_on_main  # the old behaviour, kept: nothing recorded, nothing carried
def test_a_collection_with_no_database_collation_keeps_todays_ddl_and_contract_bytes(tmp_path):
    # Its tables' collations (recorded since collector_output 3.4) are not carried alone:
    # the routine databases would stay on the server default next to them.
    store = _job(tmp_path, None, tables={"customers": "latin1_swedish_ci"})
    ddl = _ddl(store)
    assert "COLLATE" not in ddl and "DEFAULT CHARSET" not in ddl
    assert "CREATE DATABASE IF NOT EXISTS `shop`;" in ddl and SERVER_DEFAULTS in _flat(ddl)
    raw = store.read_bytes("shop/job1/transformation-contract/v2/step-1.json").decode()
    assert "collation" not in raw


@pytest.mark.parametrize(
    # utf8mb4_bg_0900_ai_ci exists on the 8.4 line but not on every version 3 release: the
    # writers check the contract's value against the target line's list (review of #2282).
    # A line the catalog does not list reads as the oldest.
    ("edited", "line"),
    [
        ("utf8mb4_made_up_ci", None),
        ("utf8mb4_bg_0900_ai_ci", None),
        ("utf8mb4_bg_0900_ai_ci", "9.9"),
    ],
)
def test_a_contract_edited_to_a_collation_step_1_does_not_carry_is_refused(tmp_path, edited, line):
    from src.tools.data_movement.plan import write_plans as again
    from src.tools.transformation_contract.hashing import canonical_json, contract_sha256

    store = _job(tmp_path)
    data = _contract(store)
    data["target"]["collation"] = {"character_set": "utf8mb4", "collation": edited}
    if line:
        data["target"]["engine_line"] = line
    base = "shop/job1/transformation-contract/v2"
    from src.contracts.transformation_contract import StepContract

    contract = StepContract.model_validate(data)
    store.write_bytes(f"{base}/step-1.json", canonical_json(contract))
    store.write_bytes(f"{base}/step-1.sha256", (contract_sha256(contract) + "\n").encode())
    again(store, "shop", "job1")
    assert edited in json.dumps(_contract(store))
    with pytest.raises((DmsStepError, ValueError), match=edited):
        _ddl(store)
    with pytest.raises((DmsStepError, ValueError), match=edited):
        _deploy(tmp_path, store)


@pytest.mark.parametrize("name", ["sh\nop", "sh\rop", "sh op", ""])
def test_a_database_name_that_would_end_a_comment_line_is_refused_in_each_comment(name):
    # Defence in depth (review of #2282): the callers already refuse such a name, but a
    # newline in one would end the `-- ALTER DATABASE` or the `-- GRANT ... ON` comment and
    # leave the rest on a live line.
    from src.contracts.transformation_contract import TargetSpec
    from src.tools.data_movement.dms_step import _dms_user_lines
    from src.tools.data_movement.routine_scripts import schema_statements

    target = TargetSpec.model_validate({"engine": "aurora_mysql", "collation": {
        "character_set": "utf8mb4", "collation": "utf8mb4_unicode_ci"}})  # fmt: skip
    with pytest.raises((DmsStepError, ValueError), match="not a name"):
        schema_statements("aurora_mysql", [name], target)
    with pytest.raises((DmsStepError, ValueError), match="not a name"):
        _dms_user_lines("aurora_mysql", "8.0", name, "shop")
    # A plain name, a backtick included, is written.
    assert "-- ALTER DATABASE `odd``db` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" in (
        schema_statements("aurora_mysql", ["odd`db"], target)
    )
    assert any(
        "ON `odd``db`.*" in line
        for line in _dms_user_lines("aurora_mysql", "8.0", "odd`db", "shop")
    )
