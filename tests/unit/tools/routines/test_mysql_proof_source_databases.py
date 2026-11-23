# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""On the Aurora MySQL test cluster the routine proof's setup tables sit in their source
database, by its literal name (#2336).

A MySQL routine body names another database literally (``inventory.stock``), so the target
makes each source database of the setup with that name, as the master user, and grants it to
the run's runner user only. A name the cluster already holds is refused (never used, never
dropped). The names carry no job prefix, so the target records each one in its harness
database (where the runner has no grant) before it creates it, makes an owner marker in it,
and both the target's close and the job's sweep revoke the runner's grant and drop only a
recorded database that holds its marker, holding the marker from before that check to the
drop (#2582).

The fake-client tests run everywhere. The live tests run :class:`DataApiTarget` through
:class:`LocalMySqlDataApi` on a disposable ``mysqld`` (no AWS call) and are marked
``integration``, since the gated CI run has no ``mysqld``."""

from __future__ import annotations

import contextlib
import datetime
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines import data_api_target as dat
from src.tools.routines.proof_inputs import (
    ProofCall,
    RoutineParameter,
    RoutineProofInput,
    build_proof_inputs,
)
from src.tools.routines.proof_targets import ProofTargetError
from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets
from tests.unit.tools.routines.test_data_api_target import ARNS, JOB, _factory

needs_mysql = pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")

#: Synthetic: a restock that moves stock in one source database and bills it in another.
RESTOCK = ("CREATE PROCEDURE restock(IN n INT) BEGIN "
           "UPDATE inventory.stock SET qty = qty + n WHERE id = 1; "
           "INSERT INTO billing.ledger (id, qty) VALUES (10, n); END")  # fmt: skip


def _col(name: str, data_type: str, nullable: bool = False, **kw: Any) -> dict[str, Any]:
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


def _table(schema: str, name: str) -> dict[str, Any]:
    return {"table_id": f"{schema}.{name}", "table_name": name, "schema_name": schema,
            "row_count": 3, "primary_key": ["id"],
            "columns": [_col("id", "int", is_auto_increment=True), _col("qty", "int", True)]}  # fmt: skip


def _setup() -> Any:
    proof = RoutineProofInput(routine_id="shop.restock", kind="procedure", name="restock",
                              tables=("inventory.stock", "billing.ledger"),
                              parameters=(RoutineParameter(name="n", data_type="int"),))  # fmt: skip
    setup = build_proof_inputs(
        [proof], [_table("inventory", "stock"), _table("billing", "ledger")], dialect="mysql"
    ).setup
    assert sorted(setup.schemas) == ["billing", "inventory"]
    return setup


def _master(client: FakeDataApi) -> list[str]:
    return [s.sql for s in client.sent if s.op == "execute" and s.secret == ARNS["secret_arn"]]


def _record(target: Any) -> str:
    return f"`{target.harness_database}`.`modernizer_made`"


# --------------------------------------------------------------------------- the fake client


def _sweep(client: FakeDataApi) -> tuple[list[str], list[str]]:
    return dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                            admin_database="routine_proof")  # fmt: skip


def _third_party(client: FakeDataApi, name: str) -> None:
    """``name`` dropped and made again by someone else: a database with no marker of the run."""
    client.databases.add(name)
    client.tables[name] = {"theirs"}


def test_each_source_database_is_made_by_its_literal_name_and_granted_to_the_runner_only():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    user = target._runner.username
    runner = f"'{user}'@'%'"
    master = _master(client)
    record = _record(target)
    assert f"CREATE TABLE {record} (name VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin " \
           "PRIMARY KEY, token CHAR(16) NOT NULL, runner VARCHAR(32) NOT NULL)" in master  # fmt: skip
    sent = [(s.sql, [p["value"].get("stringValue") for p in s.parameters]) for s in client.sent
            if s.op == "execute" and s.secret == ARNS["secret_arn"]]  # fmt: skip
    token = target._token
    for name in ("inventory", "billing"):
        made = sent.index((f"CREATE DATABASE `{name}`", []))  # no IF NOT EXISTS: never reused
        # recorded just before it is made, so a killed run's sweep finds it
        assert sent[made - 1] == (f"INSERT INTO {record} (name, token, runner) VALUES "
                                  "(:p0, :p1, :p2)", [name, token, user])  # fmt: skip
        assert sent[made + 1] == (f"CREATE VIEW `{name}`.`modernizer_owner_{token}` "
                                  "AS SELECT 1 AS id", [])  # fmt: skip
        assert sent[made + 2] == (f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
                                  f"REFERENCES, TRIGGER ON `{name}`.* TO {runner}", [])  # fmt: skip
        assert name in client.databases and client.records[target.harness_database][name] == (
            token,
            user,
        )
    # never DROP on a source database, nothing on the record (the harness's grant stays the
    # sentinel's), and the runner never reads or writes the record
    assert not [s for s in master if s.startswith("GRANT ALL") and "`inventory`" in s]
    assert not [s for s in master if s.startswith("GRANT") and "modernizer_made" in s]
    runner_sql = [s.sql for s in client.sent if s.secret != ARNS["secret_arn"]]
    assert not [s for s in runner_sql if "modernizer_made" in s or "modernizer_owner" in s]
    assert any(s.startswith("CREATE TABLE `inventory`.`stock`") for s in runner_sql)
    assert set(target._schemas) == {"inventory", "billing"}
    target.close()
    assert client.databases == set() and client.db_grants == {
        g for g in client.db_grants if g[1] == target.database.replace("_", "\\_")
    }


def test_the_setup_table_reads_cover_every_source_database():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target.prepare_calls()
    assert set(target.tables()) == {"inventory.stock", "billing.ledger"}  # never the marker
    sent = [s for s in client.sent if s.op == "execute"]
    listing = next(s.sql for s in sent if "TABLE_TYPE = 'BASE TABLE'" in s.sql
                   and "ENGINE" not in s.sql and "COUNT" not in s.sql)  # fmt: skip
    assert "'inventory'" in listing and "'billing'" in listing
    made = next(s for s in sent if s.sql.startswith("SELECT TABLE_NAME, ENGINE"))
    assert {p["value"]["stringValue"] for p in made.parameters} >= {"inventory", "billing"}
    client.sent.clear()
    target.begin_call(datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC), 0.5)
    target.snapshot(["inventory.stock", "billing.ledger"])
    target.column_types(["inventory.stock"])
    target.end_call()
    sql = [s.sql for s in client.sent if s.op == "execute"]
    assert "SHOW CREATE TABLE `inventory`.`stock`" in sql  # the per-read base-table check
    assert "SHOW CREATE TABLE `billing`.`ledger`" in sql
    columns = [s for s in client.sent if "FROM information_schema.COLUMNS" in s.sql]
    assert columns and all(s.parameters[0]["value"]["stringValue"] in ("inventory", "billing")
                           for s in columns)  # fmt: skip
    target.close()


def test_a_source_database_the_cluster_already_holds_is_refused_and_never_dropped():
    client = FakeDataApi("mysql")
    client.databases.add("billing")  # a customer's, or another run's
    target = _factory(client, engine="mysql")()
    with pytest.raises(ProofTargetError, match="already exists"):
        target.load_setup(_setup())
    target.close()
    master = _master(client)
    assert "billing" in client.databases
    assert not [s for s in master if s.startswith("DROP") and "`billing`" in s]
    assert not [s for s in master if s.startswith("GRANT") and "`billing`" in s]
    assert "billing" not in client.records.get(target.harness_database, {})
    # what it did make is gone with the target
    assert client.databases == {"billing"}


def test_a_create_that_fails_for_another_reason_keeps_its_note_for_the_sweep():
    client = FakeDataApi("mysql")
    client.runs_then_fails = ("CREATE DATABASE `billing`",)
    target = _factory(client, engine="mysql")()
    with pytest.raises(ProofTargetError, match="could not be made"):
        target.load_setup(_setup())
    client.runs_then_fails = ()
    target.close()
    # the response was lost, but the server made it: no marker, so kept and named every time
    harness = target.harness_database
    assert target.drop_failed and "billing" in client.databases
    assert "billing" in client.records[harness] and harness in client.databases
    for _ in range(2):
        dropped, left = _sweep(client)
        assert "billing" in client.databases and harness in client.databases
        assert "billing (it holds no owner marker of this run, so it is not dropped)" in left
    client.databases.discard("billing")  # once it is gone, the record goes
    dropped, left = _sweep(client)
    assert left == [] and harness in dropped and client.databases == set()


@pytest.mark.parametrize("name", ["modernizer_harness", "mdz_proof_0a1b2c3d4e_0a0b0c0d",
                                  "MDZ_PROOF_x", "mysql", "Sys"])  # fmt: skip
def test_a_source_database_named_like_the_harness_or_the_server_is_refused(name):
    from src.tools.routines.proof_inputs import ProofSetup

    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    with pytest.raises(ProofTargetError, match="keeps for the harness or the server"):
        target.load_setup(ProofSetup("mysql", (), (), (), schemas=(name,)))
    target.close()
    assert not [s for s in _master(client) if s.startswith("CREATE DATABASE") and name in s]
    assert client.databases == set()


def test_the_sweep_drops_only_the_source_databases_that_hold_the_runs_marker():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target._created = False  # left as a killed run leaves it
    harness = target.harness_database
    # a customer database the record never names, and one the record names that never got
    # its marker (a run killed between its create and its marker, or another party's)
    client.databases |= {"orders", "routine_proof"}
    _third_party(client, "audit")
    client.records[harness]["audit"] = (target._token, target._runner.username)
    dropped, left = _sweep(client)
    assert sorted(dropped) == sorted(["inventory", "billing", target.database])
    assert client.databases == {"orders", "audit", "routine_proof", harness}
    assert left == ["audit (it holds no owner marker of this run, so it is not dropped)", harness]
    assert client.records[harness] == {"audit": (target._token, target._runner.username)}


def test_close_and_the_sweep_revoke_the_runners_grant_before_each_drop():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    user = target._runner.username
    assert {(user, "inventory"), (user, "billing")} <= client.db_grants
    client.sent.clear()
    target.close()
    master = _master(client)
    for name in ("inventory", "billing"):
        revoke = master.index(f"REVOKE ALL PRIVILEGES ON `{name}`.* FROM '{user}'@'%'")
        assert revoke < master.index(f"DROP DATABASE IF EXISTS `{name}`")
    assert not {g for g in client.db_grants if g[1] in ("inventory", "billing")}
    # the sweep too, for what a killed run left granted
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target._created = False
    client.sent.clear()
    _sweep(client)
    master = _master(client)
    revoke = master.index(f"REVOKE ALL PRIVILEGES ON `inventory`.* FROM '{user}'@'%'")
    assert revoke < master.index("DROP DATABASE IF EXISTS `inventory`")
    assert not {g for g in client.db_grants if g[1] in ("inventory", "billing")}


@pytest.mark.parametrize("path", ["close", "sweep"])
def test_the_drop_holds_the_marker_from_before_its_check_in_one_transaction(path):
    """#2582: the marker check and the ``DROP`` share one transaction of the master user that
    holds the marker (``SHOW COLUMNS``, which runs nothing of the view) from before the check,
    so another session's ``DROP DATABASE`` of the name waits for the run's ``DROP``."""
    client = FakeDataApi("mysql")
    begun: list[dict[str, Any]] = []
    real_begin = client.begin_transaction

    def begin(**kw: Any) -> Any:
        begun.append(kw)
        return real_begin(**kw)

    client.begin_transaction = begin  # type: ignore[method-assign]
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    token = target._token
    client.sent.clear()
    begun.clear()
    if path == "close":
        target.close()
        assert not target.drop_failed
    else:
        target._created = False  # killed: the sweep finds what it left
        dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB, admin_database=None)
    sent = client.sent
    for name in ("inventory", "billing"):
        hold = next(i for i, s in enumerate(sent)
                    if s.sql == f"SHOW COLUMNS FROM `{name}`.`modernizer_owner_{token}`")  # fmt: skip
        check = next(i for i, s in enumerate(sent) if s.sql.startswith("SELECT COUNT(*) FROM "
                     "information_schema.TABLES") and s.parameters[0]["value"]["stringValue"]
                     == name)  # fmt: skip
        drop = next(i for i, s in enumerate(sent)
                    if s.sql == f"DROP DATABASE IF EXISTS `{name}`")  # fmt: skip
        assert hold < check < drop
        tx = sent[hold].transaction
        assert tx is not None and sent[check].transaction == tx == sent[drop].transaction
        assert sent[hold].secret == ARNS["secret_arn"]  # the master user's own transaction
        ended = [s for s in sent if s.op in ("commit", "rollback") and s.transaction == tx]
        assert ended and sent.index(ended[0]) > drop
    master_begins = [kw for kw in begun if kw["secretArn"] == ARNS["secret_arn"]]
    if path == "sweep":  # a cluster that names no database: the transaction names none either
        assert master_begins and all("database" not in kw for kw in master_begins)
    else:
        assert master_begins and all(kw["database"] == "routine_proof" for kw in master_begins)


def test_a_marker_that_cannot_be_held_keeps_the_database_and_its_row():
    """#2582: a hold that fails for any reason but "no such table" keeps the database."""
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    client.fail_on = {"SHOW COLUMNS FROM `billing`": "40001"}
    target.close()
    assert target.drop_failed and "billing" in client.databases
    assert "billing" in client.records[target.harness_database]
    client.fail_on = {}
    _, left = _sweep(client)
    assert "billing" not in client.databases and left == []


def test_a_database_another_party_made_again_during_the_run_is_never_dropped():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    client.databases.discard("inventory")  # dropped by someone else, then made again
    client.tables.pop("inventory", None)
    client.views.pop("inventory", None)
    _third_party(client, "inventory")
    target.close()
    assert target.drop_failed and "inventory" in client.databases
    assert client.tables["inventory"] == {"theirs"}
    _, left = _sweep(client)
    assert "inventory" in client.databases and target.harness_database in left
    assert not {g for g in client.db_grants if g[1] == "inventory"}  # nor left open to the runner


def test_a_base_table_named_like_the_marker_in_a_database_made_again_is_never_taken_for_it():
    """The runner can read the token and make a base table: a marker is a view, which it
    cannot make (review of #2581)."""
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    client.databases.discard("billing")
    client.tables.pop("billing", None)
    client.views.pop("billing", None)
    _third_party(client, "billing")
    client.tables["billing"].add(f"modernizer_owner_{target._token}")  # what a runner can make
    target.close()
    assert target.drop_failed and "billing" in client.databases
    _, left = _sweep(client)
    assert "billing" in client.databases
    assert "billing (it holds no owner marker of this run, so it is not dropped)" in left


def test_a_drop_that_ran_but_answered_an_error_never_drops_a_later_database_of_its_name():
    client = FakeDataApi("mysql")
    client.runs_then_fails = ("DROP DATABASE IF EXISTS `billing`",)
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target.close()
    assert target.drop_failed and "billing" not in client.databases
    harness = target.harness_database
    assert "billing" in client.records[harness]
    client.runs_then_fails = ()
    _third_party(client, "billing")  # the name taken by someone else before the sweep
    dropped, left = _sweep(client)
    assert "billing" in client.databases and "billing" not in dropped
    assert "billing (it holds no owner marker of this run, so it is not dropped)" in left


def test_the_sweep_keeps_the_record_when_a_recorded_database_is_not_dropped():
    client = FakeDataApi("mysql", drop_fails=("`inventory`",))
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target._created = False
    harness = target.harness_database
    dropped, left = _sweep(client)
    assert "inventory" in left and "billing" in dropped
    assert harness in client.databases and harness in left  # the record stays for the next sweep
    assert list(client.records[harness]) == ["inventory"]


def test_the_sweep_drops_nothing_a_record_names_unless_it_is_a_plain_source_name():
    client = FakeDataApi("mysql")
    mine, other = dat.scratch_name(JOB), dat.scratch_name("other-job")
    harness = mine + "_harness"
    kept = {"mysql", other, "routine_proof", "odd-name"}
    client.databases |= {mine, harness, *kept}
    client.records[harness] = {name: ("0a" * 8, "mdzr_0123456789_0a0b0c0d") for name in kept}
    dropped, left = _sweep(client)
    assert kept <= client.databases and mine not in client.databases
    assert dropped == [mine] and harness in left  # the record is kept, never acted on


def test_the_target_close_drops_its_source_databases_before_the_record():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    client.sent.clear()
    target.close()
    drops = [s.sql for s in client.sent if s.sql.startswith("DROP DATABASE")]
    harness = f"DROP DATABASE IF EXISTS `{target.harness_database}`"
    assert drops[-1] == harness
    assert drops.index("DROP DATABASE IF EXISTS `inventory`") < drops.index(harness)
    assert drops.index("DROP DATABASE IF EXISTS `billing`") < drops.index(harness)
    assert client.databases == set()


def test_a_target_whose_source_database_drop_fails_keeps_its_record_for_the_sweep():
    client = FakeDataApi("mysql", drop_fails=("`billing`",))
    target = _factory(client, engine="mysql")()
    target.load_setup(_setup())
    target.close()
    assert target.drop_failed
    assert client.databases == {"billing", target.harness_database}
    assert list(client.records[target.harness_database]) == ["billing"]
    client.drop_fails = ()
    dropped, left = _sweep(client)
    assert sorted(dropped) == sorted(["billing", target.harness_database]) and left == []
    assert client.databases == set()


# --------------------------------------------------------------------------- the local stand-in


def _major() -> str:
    majors = de.available_majors("mysql")
    return "8.0" if "8.0" in majors else majors[0]


def _skip_as_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("the disposable engine refuses root")


@contextlib.contextmanager
def _stand_in() -> Iterator[tuple[Any, Any, Any]]:
    """A disposable ``mysqld`` behind :class:`LocalMySqlDataApi`, the run's runner made on it,
    and a way to run a statement as root (its rows)."""
    from tests.unit.tools.routines.local_mysql_data_api import LocalMySqlDataApi

    handle = de.start("mysql", _major())
    secrets = FakeSecrets()
    client = LocalMySqlDataApi(handle, secrets)
    try:
        runner = dat.create_runner(client, secrets, cluster_arn=ARNS["cluster_arn"],
                                   secret_arn=ARNS["secret_arn"], job_id=JOB,
                                   admin_database=None, tags=[], engine="mysql")  # fmt: skip

        def as_root(sql: str) -> list[Any]:
            conn = client.connect(ARNS["secret_arn"], None)
            try:
                cur = conn.cursor()
                cur.execute(sql)
                return list(cur.fetchall()) if cur.description else []
            finally:
                conn.close()

        yield client, runner, as_root
    finally:
        client.close_all()
        de.stop(handle)


def _open(client: Any, runner: Any) -> Any:
    return dat.DataApiTarget(client, cluster_arn=ARNS["cluster_arn"],
                             secret_arn=ARNS["secret_arn"], engine="mysql", version="8.0",
                             job_id=JOB, admin_database=None, runner=runner)  # fmt: skip


def _databases(as_root: Any) -> set[str]:
    return {str(r[0]) for r in as_root("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")}


def _as_runner(client: Any, runner: Any, database: str | None, sql: str) -> int | None:
    conn = client.connect(runner.secret_arn, database)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description:
            cur.fetchall()
        return None
    except Exception as exc:  # noqa: BLE001 - the error number is the answer
        return int(getattr(exc, "errno", 0) or 0)
    finally:
        conn.close()


@pytest.mark.integration
@needs_mysql
def test_a_routine_naming_two_source_databases_runs_on_the_test_cluster_stand_in():
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        before = _databases(as_root)
        target = _open(client, runner)
        try:
            target.load_setup(_setup())
            assert target.create(de.SourceRoutine("restock", "procedure", RESTOCK)) is None
            target.prepare_calls()
            tables = ["inventory.stock", "billing.ledger"]
            assert set(target.tables()) == set(tables)
            assert {t for t, _, _ in target._counters} >= set(tables)
            target.begin_call(datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC), 0.5)
            seeded = target.snapshot(tables)
            call = ProofCall("shop.restock", "shop.restock", "call", "restock", "procedure", (5,))
            assert target.run(call).error is None
            after = target.snapshot(tables)
            assert after["inventory.stock"] != seeded["inventory.stock"]
            assert 10 in [r["id"] for r in after["billing.ledger"]]
            target.end_call()
            assert target.snapshot(tables) == seeded  # rolled back in both databases
        finally:
            target.close()
        assert _databases(as_root) == before  # both source databases left with the target


@pytest.mark.integration
@needs_mysql
def test_the_runner_cannot_touch_a_database_it_was_not_granted_on_the_stand_in():
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        as_root("CREATE DATABASE customer_db")
        as_root("CREATE TABLE customer_db.t (id INT PRIMARY KEY)")
        target = _open(client, runner)
        try:
            target.load_setup(_setup())
            record = _record(target)
            denied = {1044, 1142, 1227}
            for sql in ("SELECT * FROM customer_db.t", "INSERT INTO customer_db.t VALUES (1)",
                        "DROP TABLE customer_db.t", "CREATE TABLE customer_db.x (id INT)",
                        "DROP DATABASE customer_db", f"SELECT * FROM {record}",
                        f"DELETE FROM {record}",
                        f"INSERT INTO {record} VALUES ('customer_db', '0a0a0a0a0a0a0a0a', 'x')",
                        f"UPDATE {record} SET token = '0b0b0b0b0b0b0b0b'",
                        # its own source database: rows and DDL on its tables, never a drop
                        "DROP DATABASE inventory", "DROP TABLE inventory.stock",
                        f"DROP VIEW inventory.modernizer_owner_{target._token}",
                        f"CREATE OR REPLACE VIEW inventory.modernizer_owner_{target._token} "
                        "AS SELECT 2 AS id",
                        f"RENAME TABLE inventory.modernizer_owner_{target._token} TO "
                        "inventory.moved",
                        f"CREATE VIEW billing.modernizer_owner_{target._token}x AS SELECT 1"):  # fmt: skip
                assert _as_runner(client, runner, target.database, sql) in denied, sql
            for sql in ("SELECT COUNT(*) FROM inventory.stock", "DELETE FROM billing.ledger",
                        "ALTER TABLE inventory.stock AUTO_INCREMENT = 50"):  # fmt: skip
                assert _as_runner(client, runner, target.database, sql) is None, sql
        finally:
            target.close()
        assert "customer_db" in _databases(as_root)
        assert as_root("SELECT COUNT(*) FROM customer_db.t") == [(0,)]


@pytest.mark.integration
@needs_mysql
def test_a_source_database_name_the_stand_in_already_holds_is_refused_and_kept():
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        as_root("CREATE DATABASE inventory")
        as_root("CREATE TABLE inventory.kept (id INT PRIMARY KEY)")
        as_root("INSERT INTO inventory.kept VALUES (7)")
        before = _databases(as_root)
        target = _open(client, runner)
        try:
            with pytest.raises(ProofTargetError, match="already exists"):
                target.load_setup(_setup())
        finally:
            target.close()
        assert _databases(as_root) == before
        assert as_root("SELECT id FROM inventory.kept") == [(7,)]
        grants = " ".join(str(r[0]) for r in as_root(f"SHOW GRANTS FOR {runner.account_sql}"))
        assert "inventory" not in grants


@pytest.mark.integration
@needs_mysql
def test_the_sweep_drops_only_what_a_killed_run_recorded_on_the_stand_in():
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        as_root("CREATE DATABASE orders")
        before = _databases(as_root)
        target = _open(client, runner)
        target.load_setup(_setup())
        target._created = False  # killed: its close never ran
        assert {"inventory", "billing"} <= _databases(as_root)
        dropped, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                         admin_database=None)  # fmt: skip
        assert left == []
        assert sorted(dropped) == sorted(["inventory", "billing", target.database,
                                          target.harness_database])  # fmt: skip
        assert _databases(as_root) == before and "orders" in before


@pytest.mark.integration
@needs_mysql
def test_a_database_made_again_under_a_closed_targets_name_is_closed_to_the_runner():
    """MySQL keeps a database's grants after ``DROP DATABASE``: close revokes the runner's, so
    another party's later database of that name is not the runner's (review of #2581)."""
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        target = _open(client, runner)
        try:
            target.load_setup(_setup())
        finally:
            target.close()
        as_root("CREATE DATABASE inventory")
        as_root("CREATE TABLE inventory.theirs (id INT PRIMARY KEY)")
        as_root("INSERT INTO inventory.theirs VALUES (1)")
        grants = " ".join(str(r[0]) for r in as_root(f"SHOW GRANTS FOR {runner.account_sql}"))
        assert "inventory" not in grants and "billing" not in grants
        for sql in ("SELECT * FROM inventory.theirs", "DELETE FROM inventory.theirs",
                    "DROP DATABASE inventory"):  # fmt: skip
            assert _as_runner(client, runner, None, sql) in {1044, 1142}, sql
        # and the job's sweep leaves it alone: it holds no marker of the run
        dropped, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                         admin_database=None)  # fmt: skip
        assert "inventory" not in dropped
        assert as_root("SELECT id FROM inventory.theirs") == [(1,)]


@pytest.mark.integration
@needs_mysql
def test_a_database_made_again_during_the_run_is_kept_by_close_and_the_sweep_on_the_stand_in():
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        target = _open(client, runner)
        target.load_setup(_setup())
        as_root("DROP DATABASE billing")  # someone else's drop and create, mid-run
        as_root("CREATE DATABASE billing")
        as_root("CREATE TABLE billing.theirs (id INT PRIMARY KEY)")
        target.close()
        assert target.drop_failed
        assert {"billing", target.harness_database} <= _databases(as_root)
        _, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                   admin_database=None)  # fmt: skip
        assert "billing (it holds no owner marker of this run, so it is not dropped)" in left
        assert as_root("SELECT COUNT(*) FROM billing.theirs") == [(0,)]
        grants = " ".join(str(r[0]) for r in as_root(f"SHOW GRANTS FOR {runner.account_sql}"))
        assert "billing" not in grants
        as_root("DROP DATABASE billing")
        as_root(f"DROP DATABASE `{target.harness_database}`")


@pytest.mark.integration
@needs_mysql
def test_a_marker_the_runner_makes_in_a_database_made_again_never_gets_it_dropped():
    """Mid-run the runner's grant on a name survives another party's drop and create, and it
    can read the token: a base table it makes under the marker's name, and the view it is
    refused, never let close or the sweep drop that party's database (review of #2581)."""
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        target = _open(client, runner)
        target.load_setup(_setup())
        marker = f"modernizer_owner_{target._token}"
        seen = as_root("SELECT TABLE_NAME FROM information_schema.TABLES "
                       "WHERE TABLE_SCHEMA = 'inventory' AND TABLE_TYPE = 'VIEW'")  # fmt: skip
        assert seen == [(marker,)]
        as_root("DROP DATABASE inventory")  # someone else's drop and create, mid-run
        as_root("CREATE DATABASE inventory")
        as_root("CREATE TABLE inventory.theirs (id INT PRIMARY KEY)")
        as_root("INSERT INTO inventory.theirs VALUES (1)")
        made = _as_runner(client, runner, target.database,
                          f"CREATE TABLE inventory.{marker} (id INT)")  # fmt: skip
        assert made is None  # the grant outlived the drop: a base table it can make
        refused = _as_runner(client, runner, target.database,
                             f"CREATE VIEW inventory.{marker}_v AS SELECT 1")  # fmt: skip
        assert refused == 1142  # a view it cannot
        target.close()
        assert target.drop_failed
        assert as_root("SELECT id FROM inventory.theirs") == [(1,)]
        _, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                   admin_database=None)  # fmt: skip
        assert "inventory (it holds no owner marker of this run, so it is not dropped)" in left
        assert as_root("SELECT id FROM inventory.theirs") == [(1,)]
        grants = " ".join(str(r[0]) for r in as_root(f"SHOW GRANTS FOR {runner.account_sql}"))
        assert "inventory" not in grants
        as_root("DROP DATABASE inventory")
        as_root(f"DROP DATABASE `{target.harness_database}`")


@contextlib.contextmanager
def _made_again_before_the_drop(client: Any, as_root: Any, name: str) -> Iterator[list[str]]:
    """Another party's ``DROP DATABASE`` and ``CREATE DATABASE`` of ``name`` with a row of its
    own, started from a hook just before the run's ``DROP`` of it reaches the server, after the
    marker check (#2582). The hook lets the run's ``DROP`` go once that party is done, or once
    its drop waits on a metadata lock. Yields what the hook saw."""
    real = client.execute_statement
    seen: list[str] = []
    done = threading.Event()
    errors: list[BaseException] = []

    def third_party() -> None:
        conn = client.connect(ARNS["secret_arn"], None)  # the engine's root, not the run
        try:
            cur = conn.cursor()
            for sql in ("SET SESSION lock_wait_timeout = 20", f"DROP DATABASE {name}",
                        f"CREATE DATABASE {name}", f"CREATE TABLE {name}.theirs (id INT PRIMARY KEY)",
                        f"INSERT INTO {name}.theirs VALUES (1)"):  # fmt: skip
                cur.execute(sql)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            conn.close()
            done.set()

    worker = threading.Thread(target=third_party)

    def execute_statement(**kw: Any) -> Any:
        if kw["sql"] == f"DROP DATABASE IF EXISTS `{name}`" and not seen:
            worker.start()
            for _ in range(500):  # at most about 10 seconds of polls
                if done.is_set():
                    seen.append("done")
                    break
                waiting = as_root("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE "
                                  f"INFO = 'DROP DATABASE {name}' AND STATE LIKE 'Waiting for%lock'")  # fmt: skip
                if waiting[0][0]:
                    seen.append("waited")
                    break
                time.sleep(0.02)
            else:
                seen.append("neither")
        return real(**kw)

    client.execute_statement = execute_statement
    try:
        yield seen
    finally:
        client.execute_statement = real
        if worker.is_alive() or seen:
            worker.join(30)
    assert not errors and done.is_set()


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("path", ["close", "sweep"])
def test_a_database_made_again_between_the_marker_check_and_the_drop_is_kept_on_the_stand_in(path):
    """#2582: the run holds its marker from before the check, so another party's drop of the
    name waits for the run's ``DROP``, which then finds the name gone; that party's database,
    made after it, keeps its row."""
    _skip_as_root()
    with _stand_in() as (client, runner, as_root):
        target = _open(client, runner)
        target.load_setup(_setup())
        with _made_again_before_the_drop(client, as_root, "inventory") as seen:
            if path == "close":
                target.close()
            else:
                target._created = False  # killed: its close never ran
                _, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                           admin_database=None)  # fmt: skip
                assert left == []
        assert "inventory" in _databases(as_root)  # the other party's database is kept
        assert as_root("SELECT id FROM inventory.theirs") == [(1,)]
        assert seen == ["waited"]  # its drop waited on the run's hold
        assert "billing" not in _databases(as_root)
        grants = " ".join(str(r[0]) for r in as_root(f"SHOW GRANTS FOR {runner.account_sql}"))
        assert "inventory" not in grants
        as_root("DROP DATABASE inventory")


@pytest.mark.integration
@needs_mysql
def test_holding_the_marker_runs_nothing_of_a_view_of_its_name_on_the_stand_in():
    """The hold (#2582) never reads the marker: a view of the marker's name that another party
    made over a function with a side effect, run as its caller, is held without running it."""
    _skip_as_root()
    with _stand_in() as (client, _runner, as_root):
        token = "0a" * 8  # any 16 hex digits
        for sql in ("SET GLOBAL log_bin_trust_function_creators = 1", "CREATE DATABASE theirs",
                    "CREATE TABLE theirs.calls (n INT)",
                    "CREATE FUNCTION theirs.f() RETURNS INT MODIFIES SQL DATA SQL SECURITY "
                    "INVOKER BEGIN INSERT INTO theirs.calls VALUES (1); RETURN 1; END",
                    f"CREATE SQL SECURITY INVOKER VIEW theirs.modernizer_owner_{token} AS "
                    "SELECT theirs.f() AS id"):  # fmt: skip
            as_root(sql)
        admin = dat._DataApiBackend(client, ARNS["cluster_arn"], ARNS["secret_arn"], "mysql",
                                    "", None, dat.CALL_TIMEOUT_S)  # fmt: skip
        admin.begin()
        try:
            admin._guarded(dat._MY_HOLD.format(table=dat._owner_table("theirs", token)))
        finally:
            admin.rollback()
        assert as_root("SELECT COUNT(*) FROM theirs.calls") == [(0,)]
        as_root(f"SELECT * FROM theirs.modernizer_owner_{token}")  # a read would run it
        assert as_root("SELECT COUNT(*) FROM theirs.calls") == [(1,)]
