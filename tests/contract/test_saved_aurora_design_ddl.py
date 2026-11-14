# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved Aurora design's DDL, index and foreign-key strings are checked on every load (#2199).

#2155 checks each saved column type against the DDL type allowlist. The same saved
``schema-aurora_*/[v<N>/]schema_output.json`` also carries ``generated_ddl`` and each
table's ``indexes`` and ``foreign_keys`` statements, which the UI shows and the assess()
result hands on. Every read now rebuilds ``generated_ddl`` from the checked table
definitions (only each column's identity, NOT NULL and DEFAULT clause come from the
stored text, under the closed grammar the generator writes), and re-renders each index
and foreign key from its parsed parts with the generator's own renderer. A string that
does not come back identical is refused (``UnsafeArtifactError``), naming its position,
never its content. Designs the generator wrote, old and new, load unchanged.
"""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.shared.contract_versioning import ContractVersionError, read_contract_payload
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from tests.hypothesis_examples import scaled
from tests.unit.tools.schema.aurora_common.test_collector_text_never_reaches_ddl import _tables

REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / "tests/fixtures/real_runs"
WORDPRESS = RUNS / "wordpress-both/job/schema-aurora_mysql/v2/schema_output.json"
DISCOURSE = [
    RUNS / "discourse-both/job/schema-aurora_postgresql/v3/schema_output.json.gz",
    RUNS / "discourse-both/job/schema-aurora_postgresql/v4/schema_output.json.gz",
]
DB, JOB = "wordpress", "job1"
MODELS: dict[str, type[BaseModel]] = {
    "aurora_mysql": AuroraMySQLModelOutputContract,
    "aurora_postgresql": AuroraPostgresqlModelOutputContract,
}


def _load(path: Path) -> dict:
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    design: dict = json.loads(raw)
    return design


def _store(tmp_path: Path, engine: str, design: dict) -> tuple[LocalArtifactStore, str]:
    rel = f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)), rel


def _refused(tmp_path: Path, engine: str, design: dict, where: str, injected: str) -> None:
    store, key = _store(tmp_path, engine, design)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    exc = caught.value
    assert type(exc).__name__ == "UnsafeArtifactError"
    # The log detail names the file and the string's position, never its content.
    assert key in str(exc) and where in str(exc)
    assert injected not in str(exc)
    message = exc.public_message
    assert injected not in message
    assert "/" not in message and "schema_output" not in message


# --- Designs the generator wrote load unchanged ----------------------------------------


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("engine", "path"),
    [("aurora_mysql", WORDPRESS)] + [("aurora_postgresql", p) for p in DISCOURSE],
    ids=["wordpress-mysql-v2", "discourse-pg-v3", "discourse-pg-v4"],
)
def test_a_committed_saved_design_loads_unchanged(tmp_path: Path, engine: str, path: Path) -> None:
    """The real runs' saved designs (from earlier releases) read exactly as written."""
    saved = _load(path)
    store, key = _store(tmp_path, engine, saved)
    loaded = store.read_json(key)
    assert loaded == saved
    assert (
        MODELS[engine].model_validate(loaded).model_dump()["generated_ddl"]
        == saved["generated_ddl"]
    )


def _merged(engine: str, tables: list[AgentTable], delta: dict | None = None) -> dict:
    base = AuroraDesignBase(
        engine=engine,
        job_id=JOB,
        source_database=DB,
        source_engine="postgresql" if engine == "aurora_postgresql" else "mysql",
        tables=tables,
        source_data_types={},
    )
    result = merge_design_delta(base, delta or {"delta_version": "1.0"}, strict=False)
    assert result.output is not None
    return result.output


@pytest.mark.passes_on_main
@settings(max_examples=scaled(150), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    tables=_tables(),
    more=_tables(),
    engine=st.sampled_from(sorted(MODELS)),
)
def test_every_design_the_generator_writes_loads_unchanged(
    tmp_path_factory: pytest.TempPathFactory, tables, more, engine: str
) -> None:
    """Hostile collector text (quotes, backticks, newlines, comments, backslashes in
    names, defaults and predicates) makes no design the generator writes unloadable."""
    if tables[0].table_name == more[0].table_name:
        more = []
    design = _merged(engine, tables + more)
    store, key = _store(tmp_path_factory.mktemp("d"), engine, design)
    assert store.read_json(key) == json.loads(json.dumps(design))


_FORMS = [
    {
        "table_id": "s.accounts",
        "table_name": "accounts",
        "row_count": 10,
        "primary_key": ["id", "Code"],
        "columns": [
            {"column_name": "id", "data_type": "bigint", "nullable": False,
             "is_auto_increment": True},
            {"column_name": "Code", "data_type": "varchar", "max_length": 40, "nullable": False},
            {"column_name": "note", "data_type": "text", "nullable": True,
             "default_value": "'it''s a\nback\\slash'"},
            {"column_name": "status", "data_type": "varchar", "max_length": 20,
             "nullable": False, "default_value": "'active'::character varying"},
            {"column_name": "score", "data_type": "numeric", "numeric_precision": 10,
             "numeric_scale": 2, "nullable": True, "default_value": "-1.5e3"},
            {"column_name": "made", "data_type": "timestamp", "nullable": False,
             "default_value": "now()"},
            {"column_name": "on", "data_type": "boolean", "nullable": True,
             "default_value": "true"},
            {"column_name": "body", "data_type": "text", "nullable": True},
            {"column_name": "wide", "data_type": "varchar", "max_length": 800, "nullable": True},
            {"column_name": "ref", "data_type": "uuid", "nullable": True,
             "default_value": "gen_random_uuid()"},
            {"column_name": "day", "data_type": "date", "nullable": True,
             "default_value": "CURRENT_DATE"},
            {"column_name": "parent_id", "data_type": "bigint", "nullable": True,
             "default_value": "NULL"},
        ],
        "indexes": [
            {"index_name": "ix_partial", "columns": ["status"], "is_unique": True,
             "predicate": "((status)::text = 'active'::text) AND score IS NOT NULL"},
            {"index_name": "ix gin", "is_unique": False, "columns": ["body"], "index_type": "gin"},
            {"index_name": "ix_covering", "is_unique": False, "columns": ["Code"], "included_columns": ["score"]},
            {"index_name": "ix_text", "is_unique": False, "columns": ["note", "body"]},
            {"index_name": "ix_fulltext", "is_unique": False, "columns": ["body"], "index_type": "fulltext"},
            {"index_name": "ix_wide", "columns": ["body", "note", "Code"], "is_unique": True},
            {"index_name": "ix too wide", "columns": ["wide", "body"], "is_unique": False},
        ],
        "foreign_keys": [
            {"constraint_name": "fk_parent", "columns": ["parent_id"],
             "referenced_table": "accounts", "referenced_columns": ["id"],
             "on_delete": "SET NULL"},
            {"constraint_name": "fk \"odd\" `name`", "columns": ["Code"],
             "referenced_table": "other table", "referenced_columns": ["code"]},
        ],
    }
]  # fmt: skip


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", sorted(MODELS))
def test_every_ddl_form_the_generator_writes_loads_unchanged(tmp_path: Path, engine: str) -> None:
    """Identity, NOT NULL, each DEFAULT form, casts, E'' and multi-line literals, composite
    and prefixed keys, partial, covering, GIN and FULLTEXT indexes, skipped-index comments
    and ON DELETE actions all come back as written, and a delta's own index too."""
    tables = [AgentTable.model_validate(t) for t in _FORMS]
    added = {"index_name": "ix_delta", "columns": ["Code", "status"], "unique": True}
    if engine == "aurora_postgresql":
        added |= {"method": "btree", "include": ["day"], "where": "score > 0 AND day IS NULL"}
    design = _merged(
        engine, tables, {"delta_version": "1.0", "tables": [
            {"table_name": "accounts", "add_indexes": [added]}]}
    )  # fmt: skip
    ddl = design["generated_ddl"]
    assert " DEFAULT " in ddl and " NOT NULL" in ddl and "FOREIGN KEY" in ddl
    assert any("ix_delta" in i for i in design["table_definitions"][0]["indexes"])
    if engine == "aurora_postgresql":
        assert " WHERE " in ddl and " INCLUDE " in ddl and "USING btree" in ddl and "E'" in ddl
    else:
        assert "FULLTEXT INDEX" in ddl and "(191)" in ddl and "\n\n-- Index " in ddl
    store, key = _store(tmp_path, engine, design)
    assert store.read_json(key) == json.loads(json.dumps(design))


# --- An injected DDL script, index or foreign key is refused ---------------------------

Mutation = Callable[[dict], str]  # edits the design in place, returns the injected text


def _ddl(old: str, new: str) -> Mutation:
    def edit(design: dict) -> str:
        assert old in design["generated_ddl"]
        design["generated_ddl"] = design["generated_ddl"].replace(old, new, 1)
        return new or old

    return edit


def _append(extra: str) -> Mutation:
    def edit(design: dict) -> str:
        design["generated_ddl"] += extra
        return extra.strip()

    return edit


_WP_DDL_EDITS = {
    "a second statement": _append("\n\nDROP TABLE `wp_users`;"),
    "a statement on the last line": _append(" DROP TABLE `wp_users`;"),
    "a trailing comment": _append("\n\n-- DROP"),
    "a function default": _ddl("DEFAULT '0'", "DEFAULT (SLEEP(10))"),
    "a default that ends the literal": _ddl("DEFAULT 'yes'", "DEFAULT 'yes', `x` TEXT"),
    "a type only in the DDL": _ddl("`args` VARCHAR(191)", "`args` VARCHAR(191), `evil` TEXT"),
    "an extra column": _ddl(
        "`status` VARCHAR(20) NOT NULL,", "`status` VARCHAR(20) NOT NULL,\n  `evil` TEXT,"
    ),
    "an unquoted table": _ddl("CREATE TABLE `wp_users`", "CREATE TABLE wp_users"),
    "a renamed table": _ddl("CREATE TABLE `wp_users`", "CREATE TABLE `wp_evil`"),
    "a lone backslash": _ddl("DEFAULT ''", "DEFAULT '\\'"),
    "an extra key": _ddl("PRIMARY KEY (`action_id`)", "PRIMARY KEY (`action_id`, `hook`)"),
    "a comment line": _ddl("CREATE INDEX `args`", "-- Index x not created: y\n\nDROP TABLE t;"),
    "a missing index": _ddl("CREATE INDEX `args` ON `wp_actionscheduler_actions` (`args`);\n\n", ""),
    "an identity with a default": _ddl("AUTO_INCREMENT NOT NULL,", "AUTO_INCREMENT NOT NULL DEFAULT 1,"),
}  # fmt: skip


@pytest.mark.parametrize("edit", list(_WP_DDL_EDITS.values()), ids=list(_WP_DDL_EDITS))
def test_an_edited_generated_ddl_is_refused(tmp_path: Path, edit: Mutation) -> None:
    saved = _load(WORDPRESS)
    injected = edit(saved)
    _refused(tmp_path, "aurora_mysql", saved, "generated_ddl", injected)


_PG_DDL_EDITS = {
    "a function default": _ddl("DEFAULT CURRENT_TIMESTAMP", "DEFAULT pg_sleep(10)"),
    "a cast to another type": _ddl("DEFAULT '{}'::json", "DEFAULT '{}'::jsonb"),
    "a dollar-quoted body": _ddl("DEFAULT ''", "DEFAULT $$x$$"),
    "a MySQL identity": _ddl("GENERATED BY DEFAULT AS IDENTITY", "AUTO_INCREMENT"),
}


@pytest.mark.parametrize("edit", list(_PG_DDL_EDITS.values()), ids=list(_PG_DDL_EDITS))
def test_an_edited_postgresql_generated_ddl_is_refused(tmp_path: Path, edit: Mutation) -> None:
    saved = _load(DISCOURSE[1])
    injected = edit(saved)
    _refused(tmp_path, "aurora_postgresql", saved, "generated_ddl", injected)


@pytest.mark.parametrize("bad", [None, 7, ["CREATE TABLE x (a INT);"], ""])
def test_a_generated_ddl_that_is_not_the_script_is_refused(tmp_path: Path, bad: object) -> None:
    saved = _load(WORDPRESS)
    saved["generated_ddl"] = bad
    store, key = _store(tmp_path, "aurora_mysql", saved)
    with pytest.raises(ContractVersionError, match="generated_ddl"):
        store.read_json(key)


def _both(design: dict, field: str, i: int, j: int, new: str) -> None:
    """Replace ``table_definitions[i].<field>[j]`` and the same statement in the DDL, so
    the script still matches the list and only the statement's own check can catch it."""
    old = design["table_definitions"][i][field][j]
    design["table_definitions"][i][field][j] = new
    assert old in design["generated_ddl"]
    design["generated_ddl"] = design["generated_ddl"].replace(old, new, 1)


_WP_INDEX = "CREATE INDEX `args` ON `wp_actionscheduler_actions` (`args`);"
_WP_INDEX_EDITS = {
    "a second statement": _WP_INDEX + " DROP TABLE `wp_users`;",
    "a comment": "CREATE INDEX `args` ON `wp_actionscheduler_actions` (`args`); -- x",
    "unquoted names": "CREATE INDEX args ON wp_actionscheduler_actions (args);",
    "another table": "CREATE INDEX `args` ON `wp_users` (`args`);",
    "an unknown method": "CREATE INDEX `args` ON `wp_actionscheduler_actions` USING evil (`args`);",
    "a PostgreSQL WHERE": _WP_INDEX[:-1] + " WHERE `args` IS NULL;",
    "a zero prefix": "CREATE INDEX `args` ON `wp_actionscheduler_actions` (`args`(0));",
    "an expression key": "CREATE INDEX `args` ON `wp_actionscheduler_actions` ((SLEEP(1)));",
    "no semicolon": _WP_INDEX[:-1],
}  # fmt: skip


@pytest.mark.parametrize("new", list(_WP_INDEX_EDITS.values()), ids=list(_WP_INDEX_EDITS))
def test_an_edited_index_is_refused(tmp_path: Path, new: str) -> None:
    saved = _load(WORDPRESS)
    assert saved["table_definitions"][0]["indexes"][0] == _WP_INDEX
    _both(saved, "indexes", 0, 0, new)
    _refused(tmp_path, "aurora_mysql", saved, "table_definitions[0].indexes[0]", new)


_PG_INDEX = 'CREATE INDEX "index_admin_notices_on_identifier" ON "admin_notices" ("identifier");'
_PG_INDEX_EDITS = {
    "a function in WHERE": _PG_INDEX[:-1] + ' WHERE "identifier" = pg_sleep(1);',
    "a subquery in WHERE": _PG_INDEX[:-1] + " WHERE (SELECT 1) = 1;",
    "an unknown column in WHERE": _PG_INDEX[:-1] + ' WHERE "nope" IS NULL;',
    "a MySQL FULLTEXT": _PG_INDEX.replace("CREATE INDEX", "CREATE FULLTEXT INDEX"),
    "a MySQL prefix": _PG_INDEX.replace('("identifier")', '("identifier"(10))'),
    "an injected INCLUDE": _PG_INDEX[:-1] + ' INCLUDE ("id")); DROP TABLE "users";',
    "a backticked name": _PG_INDEX.replace('"admin_notices"', "`admin_notices`"),
}  # fmt: skip


@pytest.mark.parametrize("new", list(_PG_INDEX_EDITS.values()), ids=list(_PG_INDEX_EDITS))
def test_an_edited_postgresql_index_is_refused(tmp_path: Path, new: str) -> None:
    saved = _load(DISCOURSE[1])
    assert saved["table_definitions"][0]["indexes"][0] == _PG_INDEX
    _both(saved, "indexes", 0, 0, new)
    _refused(tmp_path, "aurora_postgresql", saved, "table_definitions[0].indexes[0]", new)


def _fk_table(design: dict) -> int:
    return next(i for i, t in enumerate(design["table_definitions"]) if t["foreign_keys"])


_PG_FK_EDITS = {
    "a second statement": '; DROP TABLE "users";',
    "an unknown action": " ON DELETE EVIL;",
    "another table": None,
}


@pytest.mark.parametrize("kind", list(_PG_FK_EDITS))
def test_an_edited_foreign_key_is_refused(tmp_path: Path, kind: str) -> None:
    saved = _load(DISCOURSE[1])
    i = _fk_table(saved)
    old = saved["table_definitions"][i]["foreign_keys"][0]
    tail = _PG_FK_EDITS[kind]
    if tail is None:
        table = saved["table_definitions"][i]["table_name"]
        new = old.replace(f'ALTER TABLE "{table}"', 'ALTER TABLE "users"', 1)
    else:
        new = old.removesuffix(" ON DELETE NO ACTION;") + tail
    assert new != old
    _both(saved, "foreign_keys", i, 0, new)
    _refused(tmp_path, "aurora_postgresql", saved, f"table_definitions[{i}].foreign_keys[0]", new)


@pytest.mark.parametrize("field", ["indexes", "foreign_keys"])
@pytest.mark.parametrize("bad", [None, 7, {"sql": "x"}])
def test_a_statement_that_is_not_a_string_is_refused(
    tmp_path: Path, field: str, bad: object
) -> None:
    saved = _load(DISCOURSE[1])
    saved["table_definitions"][_fk_table(saved)][field].append(bad)
    store, key = _store(tmp_path, "aurora_postgresql", saved)
    with pytest.raises(ContractVersionError, match=field):
        store.read_json(key)


@pytest.mark.parametrize("engine", sorted(MODELS))
def test_an_uploaded_design_is_refused_like_a_stored_one(engine: str) -> None:
    """ATX and the CLI read an uploaded contract through ``read_contract_payload``."""
    saved = _load(WORDPRESS if engine == "aurora_mysql" else DISCOURSE[1])
    read_contract_payload(copy.deepcopy(saved), MODELS[engine], artifact="upload")
    saved["generated_ddl"] += "\n\nDROP TABLE x;"
    with pytest.raises(ContractVersionError):
        read_contract_payload(saved, MODELS[engine], artifact="upload")


# --- SQL text with no table definitions to rebuild it from is refused ------------------

DROP = 'CREATE TABLE "users" ("id" BIGINT);\n\nDROP TABLE "users";'


def _untabled(engine: str, shape: str) -> dict:
    """The reviewer's four shapes: a design with SQL text and no list of tables."""
    design: dict = {
        "contract_version": "1.1",
        "job_id": JOB,
        "source_database": DB,
        "target_engine": engine,
        "migration_strategy": "carry_over",
        "generated_ddl": DROP,
        "trade_offs": [{"description": "Carry over", "impact": "Same shape."}],
        "validation_passed": True,
    }
    if shape == "null":
        design["table_definitions"] = None
    elif shape == "object":
        design["table_definitions"] = {}
    elif shape == "skipped":
        design = {"status": "skipped", "target_type": engine, "reason": "x", "generated_ddl": DROP}
    return design


UNTABLED = ["missing", "null", "object", "skipped"]


@pytest.mark.parametrize("shape", UNTABLED)
@pytest.mark.parametrize("engine", sorted(MODELS))
def test_sql_text_with_no_table_definitions_is_refused(
    tmp_path: Path, engine: str, shape: str
) -> None:
    _refused(tmp_path, engine, _untabled(engine, shape), "generated_ddl", "DROP")


@pytest.mark.parametrize(
    "extra",
    [
        {"table_definitions": [{"table_name": "t", "indexes": ["CREATE INDEX x;"]}]},
        {"indexes": ["DROP TABLE t;"]},
        {"foreign_keys": ["DROP TABLE t;"]},
    ],
    ids=["table definitions", "indexes", "foreign keys"],
)
@pytest.mark.parametrize("engine", ["aurora_mysql", "dynamodb"])
def test_a_skipped_design_that_carries_a_design_is_refused(
    tmp_path: Path, engine: str, extra: dict
) -> None:
    """A skip placeholder carries no tables and no SQL, whatever its engine."""
    design = {"status": "skipped", "target_type": engine, "reason": "x", **extra}
    store, key = _store(tmp_path, engine, design)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert type(caught.value).__name__ == "UnsafeArtifactError"
    assert "DROP" not in str(caught.value) and "DROP" not in caught.value.public_message


@pytest.mark.parametrize("field", ["indexes", "foreign_keys"])
def test_a_top_level_statement_list_is_refused(tmp_path: Path, field: str) -> None:
    saved = _load(WORDPRESS)
    saved[field] = ["DROP TABLE `wp_users`;"]
    _refused(tmp_path, "aurora_mysql", saved, field, "DROP")


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql", "dynamodb"])
def test_a_plain_skipped_design_still_loads(tmp_path: Path, engine: str) -> None:
    design = {"status": "skipped", "target_type": engine, "reason": "no queries"}
    store, key = _store(tmp_path, engine, design)
    assert store.read_json(key) == design


def test_the_refusal_says_what_to_do_and_names_no_stored_text(tmp_path: Path) -> None:
    saved = _load(WORDPRESS)
    _both(saved, "indexes", 0, 0, _WP_INDEX + " DROP TABLE `wp_users`;")
    store, key = _store(tmp_path, "aurora_mysql", saved)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert caught.value.public_message == (
        "This assessment's saved Aurora MySQL design failed its safety check: it has an index "
        "definition the tool does not write, so it was not loaded. Rerun the Aurora MySQL "
        "schema design."
    )


# --- Skipped-index comments: only the generator's own line, once per skipped index ----

COMMENT = (
    "-- Index accounts.ix too wide not created: its key parts cannot share 3072 bytes on "
    "Aurora MySQL (4 bytes per character on utf8mb4) with a useful prefix on its TEXT or "
    "BLOB columns. Index fewer or shorter columns, or serve the lookups another way."
)


def _forms(engine: str = "aurora_mysql", rename: str | None = None) -> dict:
    tables: list[dict] = copy.deepcopy(_FORMS)
    if rename is not None:
        tables[0]["indexes"][-1]["index_name"] = rename
    return _merged(engine, [AgentTable.model_validate(t) for t in tables])


@pytest.mark.passes_on_main
def test_the_generator_writes_one_comment_per_skipped_index() -> None:
    ddl = _forms()["generated_ddl"]
    assert ddl.count("\n-- Index ") == 1 and f"\n\n{COMMENT}\n\n" in ddl


_COMMENT_EDITS = {
    "another reason": COMMENT.replace("serve the lookups another way.", "run DROP DATABASE p."),
    "another index": COMMENT.replace("ix too wide", "ix invented"),
    "another table": COMMENT.replace("accounts.", "wp_users."),
    "a carriage return": COMMENT + "\rDROP TABLE t;",
    "a control character": COMMENT.replace("too wide", "too\x01wide"),
    "a second copy": COMMENT + "\n\n" + COMMENT,
    "an extra line": COMMENT + "\n\n-- Index accounts.x not created: y",
}


@pytest.mark.parametrize("new", list(_COMMENT_EDITS.values()), ids=list(_COMMENT_EDITS))
def test_an_edited_skipped_index_comment_is_refused(tmp_path: Path, new: str) -> None:
    design = _forms()
    assert COMMENT in design["generated_ddl"]
    design["generated_ddl"] = design["generated_ddl"].replace(COMMENT, new, 1)
    _refused(tmp_path, "aurora_mysql", design, "generated_ddl", "DROP")


def test_a_comment_without_its_index_note_is_refused(tmp_path: Path) -> None:
    design = _forms()
    design["app_layer_notes"] = [
        n for n in design["app_layer_notes"] if n.get("feature") != "index_key_prefix"
    ]
    _refused(tmp_path, "aurora_mysql", design, "generated_ddl", "DROP")


def test_a_skipped_index_comment_on_aurora_postgresql_is_refused(tmp_path: Path) -> None:
    design = _forms("aurora_postgresql")
    first = design["table_definitions"][0]["indexes"][0]
    design["generated_ddl"] = design["generated_ddl"].replace(first, f"{first}\n\n{COMMENT}", 1)
    design["app_layer_notes"].append(
        {"feature": "index_key_prefix", "source_object": "accounts.ix too wide",
         "recommendation": "Index not created: " + COMMENT.split(" not created: ", 1)[1]}
    )  # fmt: skip
    _refused(tmp_path, "aurora_postgresql", design, "generated_ddl", "DROP")


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", ["ix\x7fwide", "ix\x01wide", "ix\x9bwide", "ix wide", "ixé​"])
def test_a_skipped_index_with_a_control_character_in_its_name_loads(
    tmp_path: Path, name: str
) -> None:
    """The generator and the check write the comment's name the same way, so a name with a
    control character gives a design that loads (and its comment carries none)."""
    design = _forms(rename=name)
    [line] = [x for x in design["generated_ddl"].split("\n") if x.startswith("-- Index ")]
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "  " for c in line)
    store, key = _store(tmp_path, "aurora_mysql", design)
    assert store.read_json(key) == json.loads(json.dumps(design))


def test_a_cast_of_the_same_length_to_another_type_is_refused(tmp_path: Path) -> None:
    """``'{}'::text`` on a JSON column: as long as the column's own ``json`` cast."""
    saved = _load(DISCOURSE[1])
    assert "\"details\" JSON NOT NULL DEFAULT '{}'::json," in saved["generated_ddl"]
    saved["generated_ddl"] = saved["generated_ddl"].replace(
        "\"details\" JSON NOT NULL DEFAULT '{}'::json,",
        "\"details\" JSON NOT NULL DEFAULT '{}'::text,",
        1,
    )
    _refused(tmp_path, "aurora_postgresql", saved, "generated_ddl", "::text")


def test_an_index_on_a_column_the_table_lacks_is_refused(tmp_path: Path) -> None:
    saved = _load(WORDPRESS)
    _both(saved, "indexes", 0, 0, _WP_INDEX.replace("(`args`)", "(`nope`)"))
    _refused(tmp_path, "aurora_mysql", saved, "table_definitions[0].indexes[0]", "nope")


def test_a_foreign_key_on_a_column_its_table_lacks_is_refused(tmp_path: Path) -> None:
    saved = _load(DISCOURSE[1])
    i = _fk_table(saved)
    old = saved["table_definitions"][i]["foreign_keys"][0]
    new = old.replace('FOREIGN KEY ("ai_agent_id")', 'FOREIGN KEY ("nope")')
    assert new != old
    _both(saved, "foreign_keys", i, 0, new)
    _refused(
        tmp_path, "aurora_postgresql", saved, f"table_definitions[{i}].foreign_keys[0]", "nope"
    )


def test_a_long_script_of_comments_is_checked_in_linear_time() -> None:
    """The comment loop reads each line once, so a script of many comments (an upload goes
    through the same read) is checked in time linear in its length."""
    from src.tools.schema.aurora_common.saved_design import check_design_ddl
    from tests.unit.tools.sql._timing import assert_linear

    def make_text(n: int) -> str:
        copies = max(1, n // len(COMMENT))
        design = _forms()
        note = next(
            x for x in design["app_layer_notes"]
            if str(x.get("recommendation")).startswith("Index not created")
        )  # fmt: skip
        design["app_layer_notes"] += [note] * (copies - 1)
        design["generated_ddl"] = design["generated_ddl"].replace(
            COMMENT, "\n\n".join([COMMENT] * copies), 1
        )
        return json.dumps(design)

    assert_linear(make_text, lambda text: check_design_ddl(json.loads(text), "aurora_mysql"))


# --- Content decides, never the folder: legacy and case-variant folders ----------------

_FOLDERS = ["schema-aurora", "schema-Aurora_MySQL", "schema-AURORA_MYSQL", "schema-dynamodb"]


def _at(
    tmp_path: Path, folder: str, design: dict, versioned: bool
) -> tuple[LocalArtifactStore, str]:
    rel = f"{DB}/{JOB}/{folder}/" + ("v1/" if versioned else "") + "schema_output.json"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)), rel


@pytest.mark.parametrize("versioned", [True, False], ids=["v1", "unversioned"])
@pytest.mark.parametrize("folder", _FOLDERS)
def test_an_injected_script_in_any_schema_folder_is_refused(
    tmp_path: Path, folder: str, versioned: bool
) -> None:
    """The reviewer's DROP TABLE, in the legacy ``schema-aurora/``, a case variant and
    another engine's folder, on the versioned and unversioned paths."""
    saved = _load(WORDPRESS)
    saved["generated_ddl"] += '\n\nDROP TABLE "users";'
    store, key = _at(tmp_path, folder, saved, versioned)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert type(caught.value).__name__ == "UnsafeArtifactError"
    assert "DROP" not in str(caught.value) and "DROP" not in caught.value.public_message


@pytest.mark.parametrize("versioned", [True, False], ids=["v1", "unversioned"])
@pytest.mark.parametrize("folder", ["schema-aurora", "schema-Aurora_MySQL"])
def test_aurora_content_that_names_no_aurora_engine_is_refused(
    tmp_path: Path, folder: str, versioned: bool
) -> None:
    saved = _load(WORDPRESS)
    del saved["target_engine"]
    store, key = _at(tmp_path, folder, saved, versioned)
    with pytest.raises(ContractVersionError, match="target_engine"):
        store.read_json(key)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("versioned", [True, False], ids=["v1", "unversioned"])
@pytest.mark.parametrize("folder", ["schema-aurora", "schema-Aurora_MySQL"])
def test_a_valid_design_in_a_legacy_or_case_variant_folder_loads(
    tmp_path: Path, folder: str, versioned: bool
) -> None:
    """Checked with the engine the file names, it loads as written."""
    saved = _load(WORDPRESS)
    store, key = _at(tmp_path, folder, saved, versioned)
    assert store.read_json(key) == saved


@pytest.mark.passes_on_main
def test_a_design_with_no_aurora_content_in_another_folder_is_not_checked(tmp_path: Path) -> None:
    other = {"contract_version": "1.0", "collections": [{"indexes": [{"keys": {"a": 1}}]}]}
    store, key = _at(tmp_path, "schema-aurora", other, True)
    assert store.read_json(key) == other


# --- Skipped-index comments: two skipped indexes, the note's feature and reason ---------


def _two_skipped() -> tuple[dict, str, str]:
    """The forms design with a second index the key limit rules out: its two comments."""
    tables: list[dict] = copy.deepcopy(_FORMS)
    tables[0]["indexes"].append(
        {"index_name": "ix too wide 2", "columns": ["wide", "note"], "is_unique": False}
    )
    design = _merged("aurora_mysql", [AgentTable.model_validate(t) for t in tables])
    a, b = COMMENT, COMMENT.replace("ix too wide", "ix too wide 2")
    assert f"\n\n{a}" in design["generated_ddl"] and f"\n\n{b}" in design["generated_ddl"]
    return design, a, b


@pytest.mark.passes_on_main
def test_a_design_with_two_skipped_indexes_loads(tmp_path: Path) -> None:
    design, _, _ = _two_skipped()
    store, key = _store(tmp_path, "aurora_mysql", design)
    assert store.read_json(key) == json.loads(json.dumps(design))


def test_one_skipped_index_comment_twice_and_the_other_not_at_all_is_refused(
    tmp_path: Path,
) -> None:
    design, a, b = _two_skipped()
    design["generated_ddl"] = design["generated_ddl"].replace(b, a, 1)
    assert design["generated_ddl"].count(a) == 2
    _refused(tmp_path, "aurora_mysql", design, "generated_ddl", "DROP")


@pytest.mark.parametrize(
    "edit",
    [
        {"feature": "index_dropped"},
        {"recommendation": "Index not created: run DROP DATABASE prod first."},
    ],
    ids=["another feature", "another reason"],
)
def test_a_comment_whose_note_has_another_feature_or_reason_is_refused(
    tmp_path: Path, edit: dict
) -> None:
    design = _forms()
    for note in design["app_layer_notes"]:
        if str(note.get("recommendation")).startswith("Index not created"):
            note.update(edit)
    _refused(tmp_path, "aurora_mysql", design, "generated_ddl", "DROP")


def test_a_comment_for_an_index_the_table_creates_is_refused(tmp_path: Path) -> None:
    """A note naming ``ix_text``, which the table creates, allows no comment for it."""
    design = _forms()
    created = COMMENT.replace("ix too wide", "ix_text")
    design["generated_ddl"] = design["generated_ddl"].replace(COMMENT, created, 1)
    for note in design["app_layer_notes"]:
        if str(note.get("recommendation")).startswith("Index not created"):
            note["source_object"] = "accounts.ix_text"
    _refused(tmp_path, "aurora_mysql", design, "generated_ddl", "DROP")


# --- Only real Aurora content counts, and the refusal fits the folder's engine ---------

DYNAMODB = RUNS / "wordpress-both/job/schema-dynamodb/v2/schema_output.json"


def _dynamodb(edit: Callable[[dict], None] | None = None) -> dict:
    design = _load(DYNAMODB)
    if edit is not None:
        edit(design)
    return design


def _fk_list(design: dict) -> None:
    design["table_definitions"][0]["foreign_keys"] = []


def _gsi_names(design: dict) -> None:
    design["indexes"] = ["GSI1"]


def _null_ddl(design: dict) -> None:
    design["generated_ddl"] = None


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "edit",
    [None, _fk_list, _gsi_names, _null_ddl],
    ids=["as committed", "empty foreign_keys", "index names", "null generated_ddl"],
)
def test_a_dynamodb_design_with_no_aurora_content_loads(tmp_path: Path, edit) -> None:
    """Round 3: a key alone is no Aurora content; an empty, null or name-only value
    loads, in the DynamoDB folder and in the legacy Aurora one."""
    design = _dynamodb(edit)
    for folder in ("schema-dynamodb", "schema-aurora"):
        store, key = _at(tmp_path, folder, design, True)
        assert store.read_json(key) == design


def _ddl_only(design: dict) -> None:
    design["generated_ddl"] = "DROP TABLE users;"


def _table_fk_statement(design: dict) -> None:
    design["table_definitions"][0]["foreign_keys"] = [
        "ALTER TABLE t ADD CONSTRAINT f FOREIGN KEY (a) REFERENCES u (b); DROP TABLE u;"
    ]


@pytest.mark.parametrize(
    "edit", [_ddl_only, _table_fk_statement], ids=["a DDL script", "a table FK statement"]
)
def test_aurora_content_in_a_dynamodb_design_is_refused_as_dynamodb(
    tmp_path: Path, edit: Callable[[dict], None]
) -> None:
    """Each signal alone (no Aurora column types) is enough, and the message names the
    folder's engine and its own next step."""
    store, key = _at(tmp_path, "schema-dynamodb", _dynamodb(edit), True)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert caught.value.public_message == (
        "This assessment's saved DynamoDB design carries Aurora DDL content (a DDL script, "
        "SQL statements or Aurora column types), which a DynamoDB design does not have, so "
        "it was not loaded. Rerun the DynamoDB schema design."
    )
    assert "DROP" not in str(caught.value)


def test_a_legacy_aurora_design_with_only_a_script_is_refused(tmp_path: Path) -> None:
    """The ``generated_ddl`` signal alone: no table definitions, no Aurora column types."""
    design = {"target_engine": "aurora_mysql", "generated_ddl": "DROP TABLE `users`;"}
    store, key = _at(tmp_path, "schema-aurora", design, False)
    with pytest.raises(ContractVersionError, match="generated_ddl"):
        store.read_json(key)


def test_a_legacy_aurora_design_naming_no_engine_says_which_reruns_fit(tmp_path: Path) -> None:
    saved = _load(WORDPRESS)
    del saved["target_engine"]
    store, key = _at(tmp_path, "schema-aurora", saved, True)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert caught.value.public_message == (
        "This assessment's saved Aurora design does not say whether it is for Aurora MySQL or "
        "Aurora PostgreSQL, so its DDL could not be checked and it was not loaded. Rerun the "
        "Aurora MySQL or Aurora PostgreSQL schema design."
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["listed first", "listed last"])
def test_the_canonical_folder_wins_a_version_tie(reverse: bool) -> None:
    """On a case-sensitive store, ``schema-aurora_mysql/v1`` and ``schema-Aurora_MySQL/v1``
    both list under ``aurora_mysql``; the canonical one is served, in either order."""
    from src.api.services.local_s3 import LocalS3Service
    from tests.unit.agents.schema_design.test_rerun_over_refused_design import _MemoryStore

    class _Ordered(_MemoryStore):
        def list_prefix(self, prefix: str) -> list[str]:
            keys = super().list_prefix(prefix)
            return list(reversed(keys)) if reverse else keys

    store = _Ordered()
    store.base_dir = Path("/nonexistent")  # type: ignore[attr-defined]  # read only by other routes
    canonical, variant = _load(WORDPRESS), _load(WORDPRESS)
    variant["migration_strategy"] = "translate"
    for folder, design in (("schema-aurora_mysql", canonical), ("schema-Aurora_MySQL", variant)):
        store.write_json(f"{DB}/{JOB}/{folder}/v1/schema_output.json", design)
    [served] = LocalS3Service(store).read_all_schema_designs(DB, JOB)
    assert served["target_type"] == "aurora_mysql"
    assert served["artifact_path"] == f"{DB}/{JOB}/schema-aurora_mysql/v1/schema_output.json"


# --- Round 4: statements at any depth count; descriptive text does not ---------------

_NESTED = {
    "a nested list": [["DROP TABLE users;"]],
    "a dict entry": [{"name": "ix", "sql": "DROP TABLE users;"}],
}
_FOREIGN_FOLDERS = ["schema-aurora", "schema-Aurora_MySQL", "schema-dynamodb"]


def _place(design: dict, where: str, field: str, value: object) -> None:
    if where == "top level":
        design[field] = value
    else:
        design["table_definitions"][0][field] = value


@pytest.mark.parametrize("where", ["top level", "on a table"])
@pytest.mark.parametrize("field", ["indexes", "foreign_keys"])
@pytest.mark.parametrize("value", list(_NESTED.values()), ids=list(_NESTED))
@pytest.mark.parametrize("folder", _FOREIGN_FOLDERS)
def test_a_statement_nested_in_a_list_or_dict_is_refused(
    tmp_path: Path, folder: str, value: object, field: str, where: str
) -> None:
    design = _dynamodb()
    _place(design, where, field, copy.deepcopy(value))
    store, key = _at(tmp_path, folder, design, True)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert type(caught.value).__name__ == "UnsafeArtifactError"
    assert "DROP" not in str(caught.value) and "DROP" not in caught.value.public_message


_DESCRIPTIVE = {
    "an index with its keys": ("indexes", "GSI1 on (pk, sk)"),
    "an index with its key names": ("indexes", "GSI1: PK=user_id, SK=created_at"),
    "a reference": ("foreign_keys", "orders.user_id references users.id"),
}


@pytest.mark.passes_on_main
@pytest.mark.parametrize("where", ["top level", "on a table"])
@pytest.mark.parametrize("case", list(_DESCRIPTIVE.values()), ids=list(_DESCRIPTIVE))
@pytest.mark.parametrize("folder", ["schema-dynamodb", "schema-aurora"])
def test_descriptive_text_in_a_dynamodb_design_loads(
    tmp_path: Path, folder: str, case: tuple[str, str], where: str
) -> None:
    field, text = case
    design = _dynamodb()
    _place(design, where, field, [text])
    store, key = _at(tmp_path, folder, design, True)
    assert store.read_json(key) == design


_STATEMENTS = {
    "lowercase": "create index ix on t (a)",
    "a leading comment": "-- note\nDROP TABLE users",
    "a leading block comment": "/* x */ DROP TABLE users",
    "leading whitespace": "\n   DROP TABLE users",
    "WITH": "WITH x AS (SELECT 1) DELETE FROM users",
    "ALTER": "ALTER TABLE users DROP COLUMN id",
    "GRANT": "GRANT ALL ON users TO public",
    "a later statement": "ix_name; DROP TABLE users",
}


@pytest.mark.parametrize("where", ["top level", "on a table"])
@pytest.mark.parametrize("text", list(_STATEMENTS.values()), ids=list(_STATEMENTS))
@pytest.mark.parametrize("folder", _FOREIGN_FOLDERS)
def test_a_statement_in_any_form_is_refused(
    tmp_path: Path, folder: str, text: str, where: str
) -> None:
    design = _dynamodb()
    _place(design, where, "indexes", [text])
    store, key = _at(tmp_path, folder, design, True)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    assert "DROP" not in str(caught.value) and "GRANT" not in str(caught.value)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in RUNS.glob("*/job/schema-*/**/schema_output.json*")
        if not p.parent.name.startswith("schema-aurora")
    ),
    ids=lambda p: str(p.relative_to(RUNS)),
)
def test_every_committed_non_aurora_design_loads(tmp_path: Path, path: Path) -> None:
    design = _load(path)
    folder = next(part for part in path.parts if part.startswith("schema-"))
    store, key = _at(tmp_path, folder, design, True)
    assert store.read_json(key) == design
