# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 inputs for the DMS step tests (#1798, reused by #1799 and #1800): three tables
(two keyed, one without a key), a view, a LOB table in each LOB mode.

``make_bundle`` builds the inputs in memory; ``write_job`` writes a job the real writers
turn into a step 1 contract and plan (``write_step1``, ``write_plans``), for the loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.contracts.data_movement_plan import DataMovementPlan, UnboundedVarchar
from src.contracts.lineage_ops import ColumnRef, CopyOp
from src.contracts.transformation_contract import FieldRule, Finding, Rule, StepContract, TargetSpec
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import DmsStepBundle, Step1Inputs, _replication, bundle_inputs
from src.tools.data_movement.plan import write_plans
from src.tools.transformation_contract.hashing import canonical_json, contract_sha256
from src.tools.transformation_contract.write import write_step1
from tests.unit.tools.transformation_contract.conftest import col, collector, table

NO_KEY = "No primary or unique key: DMS cannot validate this table, and Debezium needs declared key columns."


def _schema(engine: str) -> str:
    return "shop" if engine == "aurora_mysql" else "public"


def _rule(
    schema: str, name: str, key: list[str], cols: list[tuple[str, str]], *, no_key: bool = False
) -> Rule:
    table_id = f"{schema}.{name}"
    fields = [FieldRule(source_column=c, target_column=c, target_type=t,
                        op=CopyOp(op="copy", column=ColumnRef(table_id=table_id, column=c))) for c, t in cols]  # fmt: skip
    findings = (
        [Finding(code="no_key", severity="warning", message=NO_KEY, table_id=table_id)]
        if no_key
        else []
    )
    return Rule(input_schema="collector", source_table=table_id, target_table=name, primary_key=key,
                fields=fields, findings=findings)  # fmt: skip


def make_contract(engine: str = "aurora_mysql") -> StepContract:
    s = _schema(engine)
    text = "TEXT"
    rules = [
        _rule(s, "audit_log", [], [("msg", "VARCHAR(200)")], no_key=True),
        _rule(
            s,
            "customers",
            ["id"],
            [("id", "BIGINT"), ("bio", "LONGTEXT" if engine == "aurora_mysql" else text)],
        ),
        _rule(s, "orders", ["id"], [("id", "BIGINT"), ("note", text)]),
    ]
    return StepContract(job_id="job1", assignment_version=2, step=1, wave=1, wave_shape="homogeneous",
                        target=TargetSpec(engine=engine), status="valid", views=[f"{s}.v_orders"], rules=rules)  # fmt: skip


def make_plan(contract: StepContract) -> DataMovementPlan:
    engine = contract.target.engine
    s = _schema(engine)
    source_engine = "mysql" if engine == "aurora_mysql" else "postgresql"
    line = "8.0" if engine == "aurora_mysql" else "16"
    limited = {"table": f"{s}.orders", "mode": "limited", "columns": ["note"], "max_size_kb": 64}
    full = {"table": f"{s}.customers", "mode": "full", "columns": ["bio"]}
    return DataMovementPlan.model_validate({
        "job_id": "job1", "database_name": "shop", "assignment_version": 2, "wave": 1, "wave_count": 2,
        "title": "Move to Aurora", "step": 1,
        "contract": {"artifact": "transformation-contract/v2/step-1.json", "sha256": contract_sha256(contract)},
        "source": {"engine": source_engine, "dialect": source_engine, "database": "shop",
                   "tables": [{"table": r.source_table, "primary_key": r.primary_key} for r in contract.rules],
                   "views": list(contract.views)},
        "cdc": {"tool": "dms"}, "initial_load": {"strategy": "dms_full_load", "position_capture": "dms_managed"},
        "targets": [{"engine": engine, "role": "owner", "tables": [r.source_table for r in contract.rules],
                     "source_of_truth": engine}],
        "gates": {"wave_gate": "Aurora copy in sync", "max_lag_seconds": 5},
        "replication": {"target_engine": engine, "target_engine_line": line, "lob_max_size_kb": 64,
                        "sizing": {"row": "small", "instance_class": "dms.t3.medium", "allocated_storage_gb": 50,
                                   "database_gb": 0.5, "writes_per_second": 3.5, "reason": "0.5 GB and 3.5 writes "
                                   "per second: up to 10 GB and 50 writes per second."},
                        "lob_tables": sorted([limited, full], key=lambda t: t["table"])},
    })  # fmt: skip


def make_ddl(contract: StepContract) -> str:
    q = "`" if contract.target.engine == "aurora_mysql" else '"'
    return "\n\n".join(f"CREATE TABLE {q}{r.target_table}{q} (\n  " + ",\n  ".join(
        f"{q}{f.target_column}{q} {f.target_type}" for f in r.fields) + "\n);" for r in contract.rules) + "\n"  # fmt: skip


def make_bundle(
    engine: str = "aurora_mysql", *, unbounded_varchar_bytes: int | None = None
) -> DmsStepBundle:
    """``unbounded_varchar_bytes`` raises the PostgreSQL source's UnboundedVarcharMaxSize, as
    the plan does for a varchar column without a length (the golden example uses it)."""
    contract = make_contract(engine)
    plan = make_plan(contract)
    if unbounded_varchar_bytes is not None:
        replication = _replication(
            bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl="x"))
        )
        replication.unbounded_varchar = UnboundedVarchar(
            max_bytes=unbounded_varchar_bytes, columns=1, column_names=["public.customers.bio"],
            reason=f"One step 1 column is a varchar without a length: the UnboundedVarcharMaxSize "
                   f"endpoint setting is raised to {unbounded_varchar_bytes} bytes.")  # fmt: skip
    return DmsStepBundle(plan=plan, contract=contract, ddl=make_ddl(contract))


def make_step1_inputs(engine: str = "aurora_mysql", *, measured: bool = False) -> Step1Inputs:
    """``measured``: every LOB table of the plan has a measured size (#2123), so preflight's
    ``lob_sizes`` check passes on a MySQL source; the default plan lists ``customers`` in full
    LOB mode, as a plan written before then does."""
    bundle = make_bundle(engine)
    if measured:
        tables = [t.model_copy(update={"mode": "limited", "max_size_kb": 64, "size_source": "measured",
                                       "reason": "Measured: the longest LOB value is 10 bytes."})
                  for t in bundle.plan.replication.lob_tables]  # fmt: skip
        replication = bundle.plan.replication.model_copy(update={"lob_tables": tables})
        bundle = bundle.model_copy(
            update={"plan": bundle.plan.model_copy(update={"replication": replication})}
        )
    return bundle_inputs(bundle)


def _job_tables(fk: bool = False, dates: bool = False) -> list[dict[str, Any]]:
    orders = table("shop", "orders", [col("id", "bigint", 1), col("note", "text", 2)], pk=["id"])
    if dates:
        orders["columns"] += [col("created", "datetime", 4, default_value="0000-00-00 00:00:00"),
                              col("shipped", "date", 5)]  # fmt: skip
    if fk:
        orders["columns"].append(col("customer_id", "bigint", 3))
        orders["foreign_keys"] = [{"constraint_name": "fk_orders_customer", "columns": ["customer_id"],
                                   "referenced_table": "customers", "referenced_columns": ["id"]}]  # fmt: skip
    return [
        table("shop", "audit_log", [col("msg", "varchar(200)", 1, max_length=200)]),
        table("shop", "customers", [col("id", "bigint", 1), col("bio", "longtext", 2)], pk=["id"]),
        orders,
    ]


def write_job(
    root: Path,
    *,
    design: list[dict[str, Any]] | None = None,
    contract: bool = True,
    plan: bool = True,
    fk: bool = False,
    dates: bool = False,
) -> LocalArtifactStore:
    """A finished MySQL job ``shop/job1`` (assignment v2): the three tables and the view in
    wave 1; ``design`` is a saved Aurora MySQL design's ``table_definitions`` (its
    ``generated_ddl`` covers those tables only, as a saved design does). ``contract`` and
    ``plan`` run the step 1 contract and plan writers; ``fk`` gives ``orders`` a
    ``customer_id`` foreign key to ``customers``; ``dates`` gives it a DATETIME column that
    defaults to a zero date and a DATE column without a default."""
    tables = _job_tables(fk, dates)
    views = [{"view_id": "shop.v_orders", "view_name": "v_orders", "schema_name": "shop",
              "definition": "select id from orders"}]  # fmt: skip
    coll = collector("mysql", tables, views)
    coll["metadata"]["source_database"]["database_name"] = "shop"
    wave = {"wave": 1, "title": "Move to Aurora MySQL", "engines": ["aurora_mysql"], "moves_from": ["mysql"],
            "homogeneity": "homogeneous", "tables": [t["table_id"] for t in tables] + ["shop.v_orders"],
            "gate": "Aurora MySQL serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("shop/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "shop/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("shop/job1/collector/output.json", coll)
    if design is not None:
        ddl = "\n\n".join(f"CREATE TABLE `{d['table_name']}` (\n  " + ",\n  ".join(
            f"`{c['name']}` {c['aurora_type']}" for c in d["columns"]) + "\n);" for d in design)  # fmt: skip
        store.write_json("shop/job1/schema-aurora_mysql/v2/schema_output.json",
                         {"generated_ddl": ddl, "table_definitions": design})  # fmt: skip
    if contract:
        write_step1(store, "shop", "job1")
    if plan:
        write_plans(store, "shop", "job1")
    return store


def write_pg_job(
    root: Path,
    *,
    extensions: list[dict[str, Any]] | None = None,
    procedures: list[dict[str, Any]] | None = None,
    triggers: list[dict[str, Any]] | None = None,
) -> LocalArtifactStore:
    """A finished PostgreSQL job ``shop/job1`` (assignment v2) with its step 1 contract and
    plan: ``public.orders`` has an identity column ``id`` (a ``nextval`` default) and a
    ``halfvec`` column, ``public.tags`` a quoted-name identity column, ``public.audit_log``
    no key and no identity, and one view. ``extensions`` are the collection's installed
    extensions; ``procedures`` and ``triggers`` its routines and triggers."""
    seq = "nextval('orders_id_seq'::regclass)"
    tables = [
        table("public", "audit_log", [col("msg", "varchar(200)", 1, max_length=200)]),
        table("public", "orders", [col("id", "int8", 1, default_value=seq, is_auto_increment=True),
                                   col("embedding", "halfvec", 2, nullable=True),
                                   col("note", "text", 3, nullable=True)], pk=["id"]),  # fmt: skip
        table("public", "tags", [col('we"ird\'id', "int4", 1, is_auto_increment=True),
                                 col("label", "citext", 2, nullable=True)], pk=['we"ird\'id']),  # fmt: skip
    ]
    views = [{"view_id": "public.v_orders", "view_name": "v_orders", "schema_name": "public",
              "definition": "select id from orders"}]  # fmt: skip
    coll = collector("postgresql", tables, views)
    coll["metadata"]["source_database"].update(database_name="shop", version="16.4")
    if extensions is not None:
        coll["metadata"]["extensions"] = extensions
    coll["database_schema"]["procedures"] = procedures or []
    coll["database_schema"]["triggers"] = triggers or []
    wave = {"wave": 1, "title": "Move to Aurora PostgreSQL", "engines": ["aurora_postgresql"],
            "moves_from": ["postgresql"], "homogeneity": "homogeneous",
            "tables": [t["table_id"] for t in tables] + ["public.v_orders"],
            "gate": "Aurora PostgreSQL serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("shop/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "shop/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("shop/job1/collector/output.json", coll)
    write_step1(store, "shop", "job1")
    write_plans(store, "shop", "job1")
    return store


def rewrite_contract(store: LocalArtifactStore, contract: StepContract) -> None:
    """Replace ``shop/job1``'s step 1 contract as its writer would (canonical bytes and
    their hash), then write the plans again so the plan pins it."""
    base = f"shop/job1/transformation-contract/v{contract.assignment_version}"
    store.write_bytes(f"{base}/step-1.json", canonical_json(contract))
    store.write_bytes(f"{base}/step-1.sha256", (contract_sha256(contract) + "\n").encode("ascii"))
    write_plans(store, "shop", "job1")


# Any wording: zero dates are read from the contract's field types, not from the message.
EDGE_MESSAGE = "an edge value in `{column}` is copied as is"


def rebuilt(bundle: DmsStepBundle, rules, zero_date_defaults=(), **update) -> Step1Inputs:
    contract = bundle.contract.model_copy(update={"rules": rules, **update})
    plan = bundle.plan.model_copy(update={"contract": None})
    return bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl=bundle.ddl,
                                       zero_date_defaults=list(zero_date_defaults)))  # fmt: skip


def edge_field(table_id: str, column: str, target_type: str) -> FieldRule:
    return FieldRule(source_column=column, target_column=column, target_type=target_type,
                     op=CopyOp(op="copy", column=ColumnRef(table_id=table_id, column=column)))  # fmt: skip


def with_zero_dates(
    engine: str = "aurora_mysql",
    tables=("orders", "customers"),
    columns=(("created", "DATETIME"), ("updated", "TIMESTAMP(6)")),
    defaults=("orders.created",),
) -> Step1Inputs:
    """Copy fields of ``columns`` on ``tables``, each with a copy_edge_value finding; the
    ``defaults`` (``table.column``) default to a zero date in the collector."""
    b = make_bundle(engine)
    schema = b.contract.rules[0].source_table.split(".")[0]
    rules = []
    for r in b.contract.rules:
        if r.target_table in tables:
            tid = f"{schema}.{r.target_table}"
            r = r.model_copy(update={
                "fields": [*r.fields, *(edge_field(tid, c, t) for c, t in columns)],
                "findings": [*r.findings, *(Finding(code="copy_edge_value", severity="warning",
                                                    message=EDGE_MESSAGE.format(column=c), table_id=tid, column=c)
                                            for c, _ in columns)]})  # fmt: skip
        rules.append(r)
    return rebuilt(b, rules, [f"{schema}.{d}" for d in defaults])
