# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What step 1 leaves behind (#1905) and the extensions its DDL needs (#1906).

DMS moves table rows only: it never advances an identity column's sequence on Aurora
PostgreSQL (``step-1.sequences.sql`` does), and it moves no procedure,
function, trigger, view or event (the DDL header and ``run_dms_step.py summary`` count
them from the collection). A moved column whose type comes from an extension (pgvector's
``vector``/``halfvec``, ``citext``) needs that extension before its table."""

from __future__ import annotations

import re

import pytest

from src.contracts.lineage_ops import ColumnRef, CopyOp
from src.contracts.transformation_contract import FieldRule, Finding
from src.tools.data_movement.dms_step import (
    DDL_FILE,
    SEQUENCES_FILE,
    DmsStepBundle,
    DmsStepError,
    SourceObjects,
    Step1Inputs,
    bundle_inputs,
    load_step1,
    not_moved,
    render_ddl,
    render_files,
    render_sequences,
    required_extensions,
    write_step1_delivery,
)

from .dms_step_fixtures import make_bundle, write_job, write_pg_job

SETVAL_ORDERS = (
    "SELECT setval(pg_get_serial_sequence('\"public\".\"orders\"', 'id'), "
    'COALESCE(MAX("id"), 0) + 1, false) FROM "public"."orders";'
)
# A column named we"ird'id: a doubled single quote in the literal, a doubled double quote
# in the identifier.
SETVAL_TAGS = (
    "SELECT setval(pg_get_serial_sequence('\"public\".\"tags\"', 'we\"ird''id'), "
    'COALESCE(MAX("we""ird\'id"), 0) + 1, false) FROM "public"."tags";'
)
ROUTINES = [
    {"procedure_id": "public.archive", "procedure_name": "archive", "procedure_type": "PROCEDURE"},
    {"procedure_id": "public.f1", "procedure_name": "f1", "procedure_type": "FUNCTION"},
    {"procedure_id": "public.f2", "procedure_name": "f2", "procedure_type": "FUNCTION"},
    {"procedure_id": "public.tf", "procedure_name": "tf", "procedure_type": "TRIGGER_FUNCTION"},
]
TRIGGERS = [
    {"trigger_id": "public.orders.t", "trigger_name": "t", "table_id": "public.orders",
     "event_type": "INSERT", "timing": "BEFORE"},
]  # fmt: skip


def _flat(text: str) -> str:
    """The comment header as one line of text (``with_header`` wraps it)."""
    return " ".join(line[3:] for line in text.splitlines() if line.startswith("-- "))


def _setvals(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("SELECT setval(")]


def _pg_bundle(**update) -> DmsStepBundle:
    return make_bundle("aurora_postgresql").model_copy(update=update)


def _with_fields(bundle: DmsStepBundle, table: str, cols: list[tuple[str, str]]) -> Step1Inputs:
    """``bundle`` with ``cols`` (name, target type) added to the rule of ``table`` (the plan's
    contract pin dropped, as the contract changed)."""
    rules = []
    for r in bundle.contract.rules:
        if r.target_table == table:
            fields = [FieldRule(source_column=c, target_column=c, target_type=t,
                                op=CopyOp(op="copy", column=ColumnRef(table_id=r.source_table, column=c)))
                      for c, t in cols]  # fmt: skip
            r = r.model_copy(update={"fields": [*r.fields, *fields]})
        rules.append(r)
    contract = bundle.contract.model_copy(update={"rules": rules})
    plan = bundle.plan.model_copy(update={"contract": None})
    return bundle_inputs(bundle.model_copy(update={"contract": contract, "plan": plan}))


# --- step-1.sequences.sql ----------------------------------------------------------------


def test_a_postgresql_job_gets_one_setval_per_moved_identity_column(tmp_path):
    inputs = load_step1(write_pg_job(tmp_path), "shop", "job1")
    assert inputs.identity_columns == (("public.orders", "id"), ("public.tags", "we\"ird'id"))
    files = render_files(inputs)
    assert SEQUENCES_FILE in files
    # Sorted by table id then column; audit_log has no identity column, so no line.
    assert _setvals(files[SEQUENCES_FILE]) == [SETVAL_ORDERS, SETVAL_TAGS]
    head = _flat(files[SEQUENCES_FILE])
    assert "The cutover is your responsibility." in head and "Identity columns: 2." in head
    assert "at cutover" not in head and "applied the last change" not in head
    assert "YOUR RESPONSIBILITY" in head and "never advances" in head


def test_only_moved_tables_get_a_setval():
    """A blocked rule's table does not move, so its identity column is left out; a pair
    naming a table outside the contract is ignored too."""
    b = _pg_bundle(identity_columns=[("public.orders", "id"), ("public.audit_log", "msg"),
                                     ("public.elsewhere", "id")])  # fmt: skip
    error = Finding(
        code="unknown_table", severity="error", message="gone", table_id="public.audit_log"
    )
    rules = [r.model_copy(update={"status": "blocked", "findings": [error]})
             if r.target_table == "audit_log" else r for r in b.contract.rules]  # fmt: skip
    contract = b.contract.model_copy(
        update={"rules": rules, "blocked_rule_count": 1, "status": "has_blocked_rules"}
    )
    plan = b.plan.model_copy(update={"contract": None})
    inputs = bundle_inputs(b.model_copy(update={"contract": contract, "plan": plan}))
    assert _setvals(render_sequences(inputs)) == [SETVAL_ORDERS]


def test_a_postgresql_target_without_identity_columns_still_gets_the_file_saying_none():
    text = render_files(bundle_inputs(_pg_bundle()))[SEQUENCES_FILE]
    assert _setvals(text) == [] and "Identity columns: 0." in _flat(text)


def test_a_mysql_target_gets_no_sequences_file_and_the_header_says_why():
    inputs = bundle_inputs(make_bundle("aurora_mysql").model_copy(
        update={"identity_columns": [("shop.orders", "id")]}))  # fmt: skip
    files = render_files(inputs)
    assert SEQUENCES_FILE not in files
    assert "- sequence values: none to set: an AUTO_INCREMENT counter moves past" in _flat(
        files[DDL_FILE]
    )
    with pytest.raises(DmsStepError, match="Aurora PostgreSQL"):
        render_sequences(inputs)


def test_the_postgresql_ddl_tells_the_customer_what_the_sequences_file_does_never_when():
    flat = _flat(render_ddl(bundle_inputs(_pg_bundle(identity_columns=[("public.orders", "id")]))))
    assert (
        f"6. {SEQUENCES_FILE} sets the identity columns' sequences, which DMS never advances: run it "
        "the same way." in flat
    )
    assert f"- sequence values: 1 ({SEQUENCES_FILE} sets them)." in flat
    assert "At cutover" not in flat and "until the step is cut over" not in flat


def test_the_delivery_writes_the_sequences_file_and_counts_it(tmp_path):
    store = write_pg_job(tmp_path)
    out = write_step1_delivery(store, "shop", "job1")
    assert f"shop/job1/data-movement/step-1/{SEQUENCES_FILE}" in out["files"]
    assert out["sequences_to_set"] == 2
    mysql = write_step1_delivery(write_job(tmp_path / "mysql"), "shop", "job1")
    assert len(mysql["files"]) == 3 and mysql["sequences_to_set"] == 0


# --- What step 1 does not move ---------------------------------------------------------------


def test_the_ddl_header_counts_what_step_1_does_not_move_from_the_collection(tmp_path):
    store = write_pg_job(tmp_path, procedures=ROUTINES, triggers=TRIGGERS)
    inputs = load_step1(store, "shop", "job1")
    assert inputs.source_objects == SourceObjects(procedures=1, functions=3, triggers=1, views=1)
    flat = _flat(render_ddl(inputs))
    for line in ("- procedures: 1 (recreate them on Aurora yourself).",
                 "- functions: 3 (recreate them on Aurora yourself).",
                 "- triggers: 1 (recreate them on Aurora yourself).",
                 "- views: 1 (recreate them on Aurora yourself).",
                 f"- sequence values: 2 ({SEQUENCES_FILE} sets them)."):  # fmt: skip
        assert line in flat, line
    assert "- events:" not in flat  # a PostgreSQL source has no events


def test_a_mysql_source_names_its_events_as_not_collected(tmp_path):
    inputs = load_step1(write_job(tmp_path), "shop", "job1")
    kinds = {n.kind: n for n in not_moved(inputs)}
    assert kinds["views"].count == 1 and kinds["procedures"].count == 0
    assert kinds["events"].count is None and "SHOW EVENTS" in kinds["events"].note
    assert "- events: not in the collection: list them on the source with SHOW EVENTS" in _flat(
        render_ddl(inputs)
    )


def test_without_the_collection_the_counts_are_not_known():
    flat = _flat(render_ddl(bundle_inputs(_pg_bundle())))
    assert "- procedures: not known: the collection was not read." in flat


# --- Extensions -------------------------------------------------------------------------


def test_vector_and_halfvec_columns_create_the_vector_extension_before_the_tables():
    inputs = _with_fields(
        _pg_bundle(), "orders", [("embedding", "HALFVEC(768)"), ("v", "VECTOR(3)")]
    )
    assert [(e.name, e.types, e.columns) for e in required_extensions(inputs)] == [
        ("vector", ("halfvec", "vector"), 2)
    ]
    ddl = render_ddl(inputs)
    assert ddl.count('CREATE EXTENSION IF NOT EXISTS "vector";') == 1
    assert (
        ddl.index('SET search_path TO "public";')
        < ddl.index("CREATE EXTENSION")
        < ddl.index("CREATE TABLE")
    )
    flat = _flat(ddl)
    assert "- vector, for 2 columns of type halfvec, vector: Aurora PostgreSQL 16 offers" in flat
    assert "halfvec and sparsevec need version 0.7.0 or later." in flat


def test_an_installed_extension_whose_name_is_a_column_type_is_created(tmp_path):
    store = write_pg_job(tmp_path, extensions=[{"name": "citext", "version": "1.6"},
                                               {"name": "vector", "version": "0.8.0"}])  # fmt: skip
    inputs = load_step1(store, "shop", "job1")
    assert inputs.extensions == ("citext", "vector")
    ddl = render_ddl(inputs)
    assert (
        'CREATE EXTENSION IF NOT EXISTS "citext";\nCREATE EXTENSION IF NOT EXISTS "vector";' in ddl
    )
    assert ddl.index("CREATE EXTENSION") < ddl.index("CREATE TABLE")


def test_an_installed_extension_no_column_uses_is_not_created():
    b = _pg_bundle(extensions=["pg_trgm", "hstore"])
    inputs = bundle_inputs(b)
    assert required_extensions(inputs) == [] and "CREATE EXTENSION" not in render_ddl(inputs)


def test_an_extension_the_target_line_does_not_list_is_named_in_the_header():
    b = _pg_bundle()
    b.plan.replication.target_engine_line = None
    inputs = _with_fields(b, "orders", [("v", "VECTOR(3)")])
    assert required_extensions(inputs)[0].offered is None
    assert (
        "not in the extension list for your Aurora PostgreSQL version: check that it is available"
        in _flat(render_ddl(inputs))
    )


def test_a_mysql_target_creates_no_extension():
    inputs = bundle_inputs(make_bundle("aurora_mysql"))
    assert required_extensions(inputs) == [] and "CREATE EXTENSION" not in render_ddl(inputs)
    assert not re.search(r"(?m)^-- Extensions:", render_ddl(inputs))
