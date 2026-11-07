# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A procedure's statements are placed as one unit (#1654, ADR-031 Amendment 21).

On the source, a procedure's statements run together in the procedure (in one
transaction where it opens one), and a trigger's run with the write that fires it. A collection that names the routine each
statement ran in (``QueryPattern.parent_routine_ids``, #1891) gives each routine a
*routine unit*: its statements routed on their own (no utility statement, no statement
that follows its routine), plus, for a trigger, the routed writes that fire it. The unit
joins the placement-group merge (``write_groups.placement_groups``) as a third source,
next to co-dependency groups and write groups, so it moves as one through every pass;
its engine comes from the shared group rule and the single scorer, never from a new
preference. The fixture is synthetic (``tests/fixtures/sqlserver_routine_units_
collection.json``): ``dbo.usp_PlaceOrder`` writes ``dbo.Orders``, ``dbo.OrderItems`` and
``dbo.Inventory``, and ``dbo.trg_Orders_Audit`` writes ``dbo.OrderAudit`` after every
insert into ``dbo.Orders``. Without the unit, the procedure's Inventory update goes to
Aurora PostgreSQL with Inventory's other writes, and its two inserts go to DynamoDB.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.assignment_overrides import (
    ExcludedEngineOverride,
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.agents.referee.routine_units import (
    build_routine_units,
    collector_routine_units,
    routine_unit_facts,
    routine_unit_members,
    saved_routine_units,
)
from src.agents.referee.synthesis_handler import run_synthesis
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.triage_handler import run_triage
from src.agents.referee.write_groups import (
    assignment_placement_groups,
    build_write_groups,
    placement_groups,
)
from src.assessment.reason_codes import REASON_CODES
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import ReasonCode
from src.shared.move_reasons import render_move_reason
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / "tests" / "fixtures" / "sqlserver_routine_units_collection.json"
DB = "RoutineUnits"
JOB = "job-1654"
SQLSERVER = SourceScope(DB, "sqlserver")
PLACE_ORDER = "dbo.usp_PlaceOrder"
AUDIT = "dbo.trg_Orders_Audit"
SHIPMENT_LOG = "dbo.trg_Shipments_Log"
PROCEDURE = ("po-insert-order", "po-insert-item", "po-take-stock")
# The procedure's statements and the trigger its insert into dbo.Orders fires.
UNIT = (*PROCEDURE, "trg-audit")


def _collection(*, links: bool = True) -> dict:
    collection: dict = json.loads(FIXTURE.read_text())
    if not links:
        for q in collection["queries"]["query_patterns"]:
            q["parent_routine_ids"] = None
    return collection


def _queries(**kwargs: bool) -> list[dict]:
    return list(_collection(**kwargs)["queries"]["query_patterns"])


def _run(
    base: Path, *, scope: list[str] | None = None, collection: dict | None = None
) -> tuple[LocalArtifactStore, int]:
    store = LocalArtifactStore(base_dir=str(base))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collection or _collection())
    run_triage(JOB, DB, store, engine_scope=scope)
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    for engine in [a["agent_type"] for a in triage["selected_agents"]]:
        run_analysis(JOB, DB, engine, store, llm_mode="none")
    run_assignment_resolver(JOB, DB, store)
    run_reality_check_handler(JOB, DB, store, llm_mode="none")
    version = resolve_downstream_assignment_version(store, DB, JOB)
    run_synthesis(JOB, DB, store, version, llm_mode="none")
    return store, version


def _raw(store: LocalArtifactStore, version: int) -> dict:
    raw: dict = store.read_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json")
    return raw


def _engines(store: LocalArtifactStore, version: int) -> dict[str, str]:
    return {
        qa["query_id"]: qa["assigned_engine"] for qa in _raw(store, version)["query_assignments"]
    }


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> tuple[LocalArtifactStore, int]:
    return _run(tmp_path_factory.mktemp("routine-units"))


# --------------------------------------------------------------------------- the units


def test_a_procedures_statements_form_one_unit() -> None:
    units = {u["routine_id"]: u for u in build_routine_units(_queries(), SQLSERVER)}
    assert units[PLACE_ORDER]["kind"] == "procedure"
    assert units[PLACE_ORDER]["query_ids"] == list(PROCEDURE)
    assert units[PLACE_ORDER]["fired_by"] == []


def test_a_trigger_joins_the_writes_that_fire_it() -> None:
    """``dbo.trg_Orders_Audit`` fires after an insert into ``dbo.Orders``: the
    procedure's insert fires it, the application's ``UPDATE dbo.Orders`` does not."""
    collection = _collection()
    units = {u["routine_id"]: u for u in collector_routine_units(collection)}
    assert units[AUDIT]["kind"] == "trigger"
    assert units[AUDIT]["query_ids"] == ["trg-audit"]
    assert units[AUDIT]["fired_by"] == ["po-insert-order"]
    assert routine_unit_members(units.values())[AUDIT] == ["trg-audit", "po-insert-order"]


def test_a_trigger_nothing_fires_is_no_unit() -> None:
    """``dbo.trg_Shipments_Log`` fires on a delete from ``dbo.Shipments``, which no
    statement makes, and a disabled trigger fires on nothing: a unit of one statement
    is dropped."""
    collection = _collection()
    assert SHIPMENT_LOG not in {u["routine_id"] for u in collector_routine_units(collection)}
    for trigger in collection["database_schema"]["triggers"]:
        if trigger["trigger_name"] == "trg_Orders_Audit":
            trigger["is_enabled"] = False
    assert AUDIT not in {u["routine_id"] for u in collector_routine_units(collection)}


def test_a_trigger_matches_its_table_with_or_without_the_schema() -> None:
    collection = _collection()
    for trigger in collection["database_schema"]["triggers"]:
        trigger["table_id"] = trigger["table_id"].split(".", 1)[1]
    units = {u["routine_id"]: u for u in collector_routine_units(collection)}
    assert units[AUDIT]["fired_by"] == ["po-insert-order"]
    for trigger in collection["database_schema"]["triggers"]:
        trigger["table_id"] = "sales." + trigger["table_id"]
    units = {u["routine_id"]: u for u in collector_routine_units(collection)}
    assert AUDIT not in units  # another schema's Orders table


def test_a_merge_fires_every_event_and_an_update_only_its_own() -> None:
    collection = _collection()
    for trigger in collection["database_schema"]["triggers"]:
        if trigger["trigger_name"] == "trg_Orders_Audit":
            trigger["events"], trigger["event_type"] = ["UPDATE"], "UPDATE"
    units = {u["routine_id"]: u for u in collector_routine_units(collection)}
    assert units[AUDIT]["fired_by"] == ["app-order-status"]
    collection["queries"]["query_patterns"].append(
        {
            "query_id": "app-order-merge",
            "query_text": (
                "MERGE dbo.Orders AS t USING (SELECT ? AS OrderId) AS s ON t.OrderId = s.OrderId "
                "WHEN MATCHED THEN UPDATE SET Total = ? WHEN NOT MATCHED THEN INSERT (OrderId) "
                "VALUES (s.OrderId);"
            ),
            "query_type": "MERGE",
            "calls_per_second": 1.0,
            "tables_accessed": ["dbo.Orders"],
            "parent_routine_ids": [],
        }
    )
    units = {u["routine_id"]: u for u in collector_routine_units(collection)}
    assert units[AUDIT]["fired_by"] == ["app-order-status", "app-order-merge"]


def test_a_unit_keeps_its_routed_statements_and_is_dropped_below_two() -> None:
    """A utility statement (``SET NOCOUNT ON``) and a statement on temporary objects
    (which follows its routine, #1891) are in no unit; a routine left with one routed
    statement has none."""
    queries = _queries()
    queries += [
        {
            "query_id": "po-nocount",
            "query_text": "SET NOCOUNT ON",
            "query_type": "OTHER",
            "tables_accessed": ["unknown"],
            "parent_routine_ids": [PLACE_ORDER],
        },
        {
            "query_id": "po-tmp",
            "query_text": "INSERT INTO @lines (ProductId) VALUES (?)",
            "query_type": "INSERT",
            "tables_accessed": ["unknown"],
            "parent_routine_ids": [PLACE_ORDER],
        },
    ]
    units = {u["routine_id"]: u for u in build_routine_units(queries, SQLSERVER)}
    assert units[PLACE_ORDER]["query_ids"] == list(PROCEDURE)
    lone = [q for q in queries if q["query_id"] in {"po-insert-order", "po-nocount", "po-tmp"}]
    assert build_routine_units(lone, SQLSERVER) == []


def test_a_statement_two_routines_run_merges_their_units() -> None:
    queries = _queries()
    for q in queries:
        if q["query_id"] == "app-order-get":
            q["parent_routine_ids"] = ["dbo.usp_GetOrder"]
        if q["query_id"] in {"app-order-total", "po-insert-item"}:
            q["parent_routine_ids"] = [PLACE_ORDER, "dbo.usp_GetOrder"]
    members = routine_unit_members(build_routine_units(queries, SQLSERVER))
    assert members["dbo.usp_GetOrder"] == ["app-order-get", "app-order-total", "po-insert-item"]
    units = placement_groups([], {}, members)
    assert len(units) == 1
    assert set(units[0]) == {*PROCEDURE, "app-order-get", "app-order-total"}


@pytest.mark.parametrize("links", [None, []])
def test_without_links_there_are_no_units(links: list | None) -> None:
    queries = _queries()
    for q in queries:
        q["parent_routine_ids"] = copy.copy(links)
    assert build_routine_units(queries, SQLSERVER) == []


def test_placement_groups_with_no_routine_units_is_todays() -> None:
    co = [["a", "b"], ["c", "d"]]
    writes = {"t1": ["b", "e"], "t2": ["f"]}
    assert placement_groups(co, writes, {}) == placement_groups(co, writes)
    assert placement_groups(co, writes, None) == [["a", "b", "e"], ["c", "d"]]


def test_a_routine_unit_merges_the_write_groups_it_touches() -> None:
    queries = _queries()
    writes = build_write_groups(queries, "sqlserver", SQLSERVER)
    members = routine_unit_members(collector_routine_units(_collection()))
    units = placement_groups([], writes, members)
    merged = next(u for u in units if "po-take-stock" in u)
    # The procedure, its trigger, the other writes of Orders and of Inventory.
    assert set(merged) == {*UNIT, "app-order-status", "app-restock"}
    # Without the routine unit, the procedure's tables are three units.
    apart = placement_groups([], writes)
    assert not any({"po-insert-order", "po-take-stock"} <= set(u) for u in apart)


# --------------------------------------------------------------------------- routing


def test_the_procedure_and_its_trigger_land_on_one_engine(
    pipeline: tuple[LocalArtifactStore, int],
) -> None:
    store, final = pipeline
    for version in (1, final):
        engines = _engines(store, version)
        assert len({engines[q] for q in UNIT}) == 1, (version, {q: engines[q] for q in UNIT})


def test_without_links_the_procedure_splits(tmp_path: Path) -> None:
    """The evidence the fixture needs a unit: without the links, today's routing puts
    the procedure's Inventory update and its Orders insert on two engines."""
    store, final = _run(tmp_path, collection=_collection(links=False))
    for version in (1, final):
        engines = _engines(store, version)
        assert engines["po-take-stock"] != engines["po-insert-order"]
        assert saved_routine_units(_raw(store, version)) == []


def test_the_units_are_in_the_assignment(pipeline: tuple[LocalArtifactStore, int]) -> None:
    store, final = pipeline
    for version in (1, final):
        raw = _raw(store, version)
        Assignment.model_validate(raw)
        units = {u["routine_id"]: u for u in saved_routine_units(raw)}
        assert set(units) == {PLACE_ORDER, AUDIT}
        assert units[PLACE_ORDER]["query_ids"] == list(PROCEDURE)
        assert units[AUDIT]["fired_by"] == ["po-insert-order"]
        merged = next(u for u in assignment_placement_groups(raw) if "po-take-stock" in u)
        assert set(UNIT) <= set(merged)


def test_the_routine_rule_places_the_members_no_write_group_holds(
    pipeline: tuple[LocalArtifactStore, int],
) -> None:
    """``po-insert-item`` (the only write of ``dbo.OrderItems``) and ``trg-audit`` (the
    only write of ``dbo.OrderAudit``) are in the unit only through their routines: the
    ledger records ``group.routine``. The writes of ``dbo.Orders`` and ``dbo.Inventory``
    keep ``group.write``. Every Move carries the unit's key and its group scores."""
    store, _ = pipeline
    raw = _raw(store, 1)
    first = {m["query_id"]: m for m in raw["moves"] if m["from_engine"] is None}
    assert first["po-insert-item"]["reason_code"] == ReasonCode.ROUTINE.value
    assert first["trg-audit"]["reason_code"] == ReasonCode.ROUTINE.value
    for qid in ("po-insert-order", "po-take-stock", "app-order-status", "app-restock"):
        assert first[qid]["reason_code"] == ReasonCode.WRITE_GROUP.value, qid
    keys = {first[q]["group_key"] for q in UNIT}
    assert len(keys) == 1 and None not in keys
    assert first["po-insert-item"]["evidence"]["group_scores"]
    reasons = {qa["query_id"]: qa["assignment_reason"] for qa in raw["query_assignments"]}
    assert reasons["po-insert-item"].startswith(f"routine unit ({PLACE_ORDER})")
    assert reasons["trg-audit"].startswith(f"routine unit ({AUDIT})")


def test_the_routine_rule_reads_as_a_sentence_and_a_public_code() -> None:
    from src.contracts.move_ledger import Move, MovePass

    move = Move(
        seq=1,
        query_id="q",
        from_engine=None,
        to_engine="aurora_postgresql",
        pass_=MovePass.CO_DEPENDENCY,
        reason_code=ReasonCode.ROUTINE,
        score_after=70.0,
    )
    assert "routine" in render_move_reason(move)
    assert "routine" in render_move_reason(move.model_copy(update={"from_engine": "dynamodb"}))
    assert REASON_CODES[ReasonCode.ROUTINE] == "co_dependency"


def test_the_unit_is_in_the_report(pipeline: tuple[LocalArtifactStore, int]) -> None:
    store, final = pipeline
    report = store.read_json(f"{DB}/{JOB}/synthesis/v{final}/report.json")
    engines = _engines(store, final)
    (unit,) = report["routine_units"]
    assert unit["merged_by"] == [
        {"kind": "procedure", "id": PLACE_ORDER},
        {"kind": "trigger", "id": AUDIT},
    ]
    assert set(UNIT) <= set(unit["query_ids"])
    assert {"dbo.Orders", "dbo.OrderItems", "dbo.Inventory", "dbo.OrderAudit"} <= set(
        unit["tables"]
    )
    assert unit["engine"] == engines["po-take-stock"]
    assert unit["engines"] == [engines["po-take-stock"]]


def test_the_report_facts_name_a_split_unit_honestly() -> None:
    """A unit a customer split states its engines and no single engine."""
    assignment = {
        "co_dependency_groups": [],
        "write_groups": {},
        "routine_units": [
            {
                "routine_id": PLACE_ORDER,
                "kind": "procedure",
                "query_ids": ["a", "b"],
                "fired_by": [],
            }
        ],
        "query_assignments": [
            {"query_id": "a", "assigned_engine": "dynamodb", "source_tables": ["dbo.T"]},
            {"query_id": "b", "assigned_engine": "aurora_postgresql", "source_tables": []},
        ],
    }
    (unit,) = routine_unit_facts(assignment)
    assert unit["engine"] is None
    assert unit["engines"] == ["aurora_postgresql", "dynamodb"]
    assert routine_unit_facts({"query_assignments": []}) == []


def test_a_customer_pick_moves_the_whole_unit(tmp_path: Path) -> None:
    """Without ``app-restock`` (a three-table join DynamoDB cannot run), a pick of the
    other engine on ``po-insert-item``, the procedure's one statement on
    ``dbo.OrderItems``, moves the whole unit with it: the procedure's writes of other
    tables and its trigger too."""
    collection = _collection()
    patterns = collection["queries"]["query_patterns"]
    collection["queries"]["query_patterns"] = [
        q for q in patterns if q["query_id"] != "app-restock"
    ]
    store, final = _run(tmp_path, collection=collection)
    before = _engines(store, final)
    assert len({before[q] for q in UNIT}) == 1
    target = "aurora_postgresql" if before["po-insert-item"] == "dynamodb" else "dynamodb"
    result = apply_assignment_overrides(
        store, DB, JOB, [QueryOverrideInput(query_id="po-insert-item", assigned_engine=target)]
    )
    engines = {qa.query_id: qa.assigned_engine for qa in result.assignment.query_assignments}
    assert {engines[q] for q in UNIT} == {target}
    reasons = {qa.query_id: qa.assignment_reason for qa in result.assignment.query_assignments}
    assert f"routine {AUDIT}" in reasons["trg-audit"]


def test_a_pick_the_unit_cannot_follow_is_rejected(
    pipeline: tuple[LocalArtifactStore, int], tmp_path: Path
) -> None:
    """A pick of DynamoDB on ``po-insert-item`` would pull ``app-restock`` along (it
    shares the Inventory write group with the procedure's update), and DynamoDB cannot
    run that join: the pick is rejected as a unit, never applied to half of it."""
    import shutil

    store, _ = pipeline
    shutil.copytree(store.base_dir, tmp_path / "copy")
    work = LocalArtifactStore(base_dir=str(tmp_path / "copy"))
    with pytest.raises(ExcludedEngineOverride) as raised:
        apply_assignment_overrides(
            work,
            DB,
            JOB,
            [QueryOverrideInput(query_id="po-insert-item", assigned_engine="dynamodb")],
        )
    assert "app-restock" in {e.query_id for e in raised.value.excluded}


def test_a_unit_no_engine_in_the_scope_serves_is_unservable_as_one(tmp_path: Path) -> None:
    """With DynamoDB and DocumentDB only, no engine runs ``app-restock`` (a three-table
    join update), which the procedure's Inventory update shares a write group with: the
    whole unit is unservable, never half of it placed (ADR-031 Amendment 15). A statement
    on temporary objects in the procedure, which follows its routine (#1891), has no
    engine to follow, and no relational engine is its last resort: unservable too."""
    from src.agents.referee.routine_units import unplaced_reason
    from src.agents.referee.utility_statements import PROCEDURE_INTERNAL_REASON

    collection = _collection()
    collection["queries"]["query_patterns"].append(
        {
            "query_id": "po-tmp",
            "query_text": "INSERT INTO @lines (ProductId) VALUES (?)",
            "query_type": "INSERT",
            "calls_per_second": 5.0,
            "tables_accessed": ["unknown"],
            "parent_routine_ids": [PLACE_ORDER],
        }
    )
    store, final = _run(tmp_path, scope=["dynamodb", "documentdb"], collection=collection)
    for version in (1, final):
        raw = _raw(store, version)
        unservable = {u["query_id"]: u for u in raw.get("unservable_queries") or []}
        assert set(UNIT) <= set(unservable), (version, sorted(unservable))
        assert {qa["query_id"] for qa in raw["query_assignments"]}.isdisjoint([*UNIT, "po-tmp"])
        assert unservable["po-tmp"]["reason"] == unplaced_reason(
            PROCEDURE_INTERNAL_REASON, PLACE_ORDER
        )
        # A unit unservable as one is left out of the saved units (#1329).
        assert PLACE_ORDER not in {u["routine_id"] for u in saved_routine_units(raw)}


def test_an_assignment_saved_before_routine_units_reads_none() -> None:
    assert saved_routine_units({"co_dependency_groups": [["a", "b"]]}) == []
    assert assignment_placement_groups({"co_dependency_groups": [["a", "b"]]}) == [["a", "b"]]


def test_every_caller_of_the_merge_passes_the_routine_units() -> None:
    """A structural backstop to the behaviour tests below: each call of
    ``write_groups.placement_groups`` in ``src/`` passes a third source."""
    import ast

    missing: list[str] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        if path.name == "write_groups.py" and path.parent.name == "referee":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "placement_groups" and len(node.args) + len(node.keywords) < 3:
                missing.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert missing == []


# --------------------------------------------------------------------------- every pass


def _rewrite(store: LocalArtifactStore, version: int, engines: dict[str, str]) -> None:
    """Put ``engines``' queries on those engines in the saved ``version``, as a saved job
    that split them would hold (no Move: the split predates the pass under test)."""
    raw = _raw(store, version)
    for qa in raw["query_assignments"]:
        if qa["query_id"] in engines:
            qa["assigned_engine"] = engines[qa["query_id"]]
            qa["tied_engines"] = []
    store.write_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json", raw)


def test_the_split_write_reroute_moves_the_whole_unit(tmp_path: Path) -> None:
    """A saved version that split ``dbo.Orders``' writes, with the procedure's
    ``po-insert-item`` moved off too: the reroute puts the whole merged unit on one
    engine, never only the table's writes."""
    from src.agents.referee.assignment_overrides import reroute_split_writes

    store, final = _run(tmp_path)
    _rewrite(store, final, {"app-order-status": "dynamodb", "po-insert-item": "dynamodb"})
    rerouted = reroute_split_writes(store, DB, JOB)
    assert rerouted is not None
    engines = {qa.query_id: qa.assigned_engine for qa in rerouted.assignment.query_assignments}
    assert len({engines[q] for q in (*UNIT, "app-order-status")}) == 1, engines


def _tied(qid: str, tables: list[str]) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": "dynamodb",
        "source_tables": tables,
        "tied_engines": ["aurora_postgresql", "dynamodb"],
        "in_scope": True,
    }


def test_a_pending_choice_covers_the_whole_unit() -> None:
    """Two members of one routine unit, each tied on its own table: one choice, so a
    pick moves the unit as one. The version's saved units are read; without a version
    (or for one saved before routine units) the collection's."""
    from src.agents.referee.pending_choices import build_pending_choices

    collection = _collection()
    qas = [_tied("po-insert-item", ["dbo.OrderItems"]), _tied("trg-audit", ["dbo.OrderAudit"])]
    assignments: tuple[dict | None, ...] = (
        {"routine_units": collector_routine_units(collection)},
        {"co_dependency_groups": []},
        None,
    )
    for assignment in assignments:
        (choice,) = build_pending_choices(qas, collection, None, assignment=assignment)
        assert choice.query_ids == ["po-insert-item", "trg-audit"]
    # A version that saved no unit offers one choice per table.
    apart = build_pending_choices(qas, collection, None, assignment={"routine_units": []})
    assert len(apart) == 2


class _Captured(Exception):
    """Raised by a spy once it has the units a pass handed on."""


def _spy(mp: pytest.MonkeyPatch, module: object, name: str) -> list[list[list[str]]]:
    """Replace ``module.name`` with a spy that records the placement units it is given
    (its ``co_dependency_groups``) and stops the pass."""
    seen: list[list[list[str]]] = []

    def spy(*args: object, **kwargs: Any) -> object:
        seen.append([list(g) for g in kwargs.get("co_dependency_groups") or []])
        raise _Captured

    mp.setattr(module, name, spy)
    return seen


@pytest.mark.parametrize("old_job", [False, True])
def test_the_reality_check_sweep_places_the_unit_as_one(
    pipeline: tuple[LocalArtifactStore, int], tmp_path: Path, old_job: bool
) -> None:
    """The sanity sweep gets the version's placement units with the routine unit merged
    in (``assignment_placement_groups``), so it moves the unit as one; for a version
    saved before routine units, the collection's."""
    import shutil

    from src.agents.referee import reality_check_handler

    store, _ = pipeline
    shutil.copytree(store.base_dir, tmp_path / "copy")
    work = LocalArtifactStore(base_dir=str(tmp_path / "copy"))
    job = work.base_dir / DB / JOB
    shutil.rmtree(job / "reality-check")
    for path in job.glob("assignment/v[2-9]"):
        shutil.rmtree(path)
    if old_job:
        raw = _raw(work, 1)
        raw.pop("routine_units")
        work.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", raw)
    with pytest.MonkeyPatch.context() as mp:
        seen = _spy(mp, reality_check_handler, "sanity_sweep")
        with pytest.raises(_Captured):
            run_reality_check_handler(JOB, DB, work, llm_mode="none")
    assert any(set(UNIT) <= set(g) for g in seen[0])


def test_the_post_schema_reroute_places_the_unit_as_one(
    pipeline: tuple[LocalArtifactStore, int], tmp_path: Path
) -> None:
    """The post-schema router gets the same placement units, so a reroute moves the
    unit as one."""
    import shutil

    from src.agents.referee import post_schema_router
    from src.agents.referee.post_schema_routing_handler import PostSchemaRouting

    store, final = pipeline
    shutil.copytree(store.base_dir, tmp_path / "copy")
    work = LocalArtifactStore(base_dir=str(tmp_path / "copy"))
    engine = _engines(store, final)["po-take-stock"]
    work.write_json(f"{DB}/{JOB}/schema-{engine}/v{final}/schema_output.json", {})
    with pytest.MonkeyPatch.context() as mp:
        seen = _spy(mp, post_schema_router, "route_unsupported_queries")
        with pytest.raises(_Captured):
            PostSchemaRouting(work, redesign=lambda *a, **k: None).run(JOB, DB)
    assert any(set(UNIT) <= set(g) for g in seen[0])


# --------------------------------------------------------------------------- old saved jobs


def test_every_pass_reads_the_saved_units_and_an_old_job_the_collections() -> None:
    """The saved units are what later passes place (``routine_units_for``): a version's
    own list, empty included. A version saved before routine units records none, and
    reads the collection's; with no collection, none."""
    from src.agents.referee.routine_units import records_routine_units, routine_units_for

    collection = _collection()
    units = collector_routine_units(collection)
    saved = {"routine_units": units[:1], "co_dependency_groups": []}
    assert routine_units_for(saved, collection) == units[:1]
    assert routine_units_for({"routine_units": []}, collection) == []
    old: dict[str, list] = {"co_dependency_groups": []}
    assert routine_units_for(old, collection) == units
    assert routine_units_for(old, None) == []
    merged = next(u for u in assignment_placement_groups(old, collection) if "po-take-stock" in u)
    assert set(UNIT) <= set(merged)
    # A model read from an old file records none; one a pass built records its list.
    model = Assignment.model_validate(
        {
            "job_id": JOB,
            "version": 1,
            "status": "auto_generated",
            "timestamp": "2026-11-07T00:00:00Z",
            "query_assignments": [],
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
        }
    )
    assert not records_routine_units(model)
    assert routine_units_for(model, collection) == units
    assert records_routine_units(model.model_copy(update={"routine_units": []}))


def test_an_old_jobs_reality_check_places_and_saves_its_units(tmp_path: Path) -> None:
    """A first assignment saved before routine units (no ``routine_units`` key) whose
    unit sits on DocumentDB: the reality check reads the collection's units, moves the
    unit as one, and the version it writes saves them."""
    import shutil

    store, _ = _run(tmp_path)
    _rewrite(store, 1, {q: "documentdb" for q in (*UNIT, "app-order-status", "app-restock")})
    raw = _raw(store, 1)
    raw.pop("routine_units")
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", raw)
    for path in store.base_dir.glob(f"{DB}/{JOB}/assignment/v[2-9]"):
        shutil.rmtree(path)
    run_reality_check_handler(JOB, DB, store, llm_mode="none")
    final = resolve_downstream_assignment_version(store, DB, JOB)
    engines = _engines(store, final)
    assert len({engines[q] for q in UNIT}) == 1, {q: engines[q] for q in UNIT}
    assert {u["routine_id"] for u in saved_routine_units(_raw(store, final))} == {
        PLACE_ORDER,
        AUDIT,
    }


# --------------------------------------------------------------------------- a customer split


def test_a_customer_split_of_a_unit_is_a_feasibility_finding(tmp_path: Path) -> None:
    """Two picks in one edit that put the procedure's statements on two engines are the
    customer's choice and stand, as a co-dependency split's do, with an advisory
    finding that names the routine; ``report.json`` shows the unit with no one engine."""
    from src.agents.referee.feasibility_review import review_assignment_feasibility
    from src.contracts.feasibility_models import FindingKind, FindingSeverity

    collection = _collection()
    patterns = collection["queries"]["query_patterns"]
    collection["queries"]["query_patterns"] = [
        q for q in patterns if q["query_id"] != "app-restock"
    ]
    store, final = _run(tmp_path, collection=collection)
    before = _engines(store, final)
    other = "aurora_postgresql" if before["po-insert-item"] == "dynamodb" else "dynamodb"
    result = apply_assignment_overrides(
        store,
        DB,
        JOB,
        [
            QueryOverrideInput(query_id="po-insert-item", assigned_engine=other),
            QueryOverrideInput(query_id="po-take-stock", assigned_engine=before["po-take-stock"]),
        ],
    )
    findings = [
        f
        for f in review_assignment_feasibility(result.assignment, collection)
        if f.kind is FindingKind.ROUTINE_UNIT_SPLIT
    ]
    assert findings, "a split routine unit must be named"
    assert {f.severity for f in findings} == {FindingSeverity.ADVISORY}
    assert any(PLACE_ORDER in f.message for f in findings)
    assert "po-insert-item" in findings[0].query_ids
    (fact,) = routine_unit_facts(result.assignment.model_dump(mode="json"), collection)
    assert fact["engine"] is None and len(fact["engines"]) == 2


def test_a_unit_on_one_engine_is_no_finding(pipeline: tuple[LocalArtifactStore, int]) -> None:
    from src.agents.referee.feasibility_review import review_assignment_feasibility
    from src.contracts.feasibility_models import FindingKind

    store, final = pipeline
    assignment = Assignment.model_validate(_raw(store, final))
    collection = store.read_json(f"{DB}/{JOB}/collector/output.json")
    assert not [
        f
        for f in review_assignment_feasibility(assignment, collection)
        if f.kind is FindingKind.ROUTINE_UNIT_SPLIT
    ]


# --------------------------------------------------------------------------- waves


def test_a_waves_table_groups_keep_a_routines_tables_together() -> None:
    """DynamoDB's wave groups a routine unit's tables as one table group, kind
    ``routine``; without the unit they are independent tables."""
    from src.agents.referee.migration_wave_tables import _dynamodb_table_groups

    tables = ["dbo.Orders", "dbo.OrderItems", "dbo.OrderAudit", "dbo.Users"]
    rows = [{"table_id": t} for t in tables]
    qas = [
        {
            "query_id": "po-insert-order",
            "assigned_engine": "dynamodb",
            "source_tables": ["dbo.Orders"],
        },
        {
            "query_id": "po-insert-item",
            "assigned_engine": "dynamodb",
            "source_tables": ["dbo.OrderItems"],
        },
        {
            "query_id": "trg-audit",
            "assigned_engine": "dynamodb",
            "source_tables": ["dbo.OrderAudit"],
        },
        {"query_id": "app-user", "assigned_engine": "dynamodb", "source_tables": ["dbo.Users"]},
    ]
    members = routine_unit_members(collector_routine_units(_collection()))
    groups = placement_groups([], {}, members)
    ids = frozenset(q for m in members.values() for q in m)
    out = _dynamodb_table_groups("dynamodb", rows, groups, qas, ids)
    assert out[0] == {
        "tables": ["dbo.OrderAudit", "dbo.OrderItems", "dbo.Orders"],
        "query_count": 3,
        "kind": "routine",
    }
    assert out[1]["kind"] == "independent" and out[1]["tables"] == ["dbo.Users"]
    assert [g["kind"] for g in _dynamodb_table_groups("dynamodb", rows, [], qas)] == ["independent"]


def test_the_report_waves_read_the_routine_units(tmp_path: Path) -> None:
    """End to end: with the unit on DynamoDB, its wave's table groups hold the routine's
    tables as one ``routine`` group."""
    collection = _collection()
    patterns = collection["queries"]["query_patterns"]
    collection["queries"]["query_patterns"] = [
        q for q in patterns if q["query_id"] != "app-restock"
    ]
    store, final = _run(tmp_path, collection=collection)
    assert _engines(store, final)["po-insert-order"] == "dynamodb"
    report = store.read_json(f"{DB}/{JOB}/synthesis/v{final}/report.json")
    groups = [
        g
        for w in report.get("migration_waves") or []
        for g in w.get("table_groups") or []
        if g.get("kind") == "routine"
    ]
    assert groups and {"dbo.Orders", "dbo.OrderItems", "dbo.OrderAudit"} <= set(groups[0]["tables"])
