# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement on temporary objects follows its routine (#1891).

#1876 (fixed in #1879) kept a SQL Server statement that touches only temporary tables or
table variables with the utility statements, because the collector did not say which
procedure or trigger it ran in. The collector now records that
(``QueryPattern.parent_routine_ids``). Such a statement whose routine is known follows
the routine's other statements (maintainer decision on #1891, 2026-11-05): it goes to the
engine they are routed to, but it neither scores nor gates them, so a temporary-table
step never pulls its routine to Aurora on its own. Every routing pass places the routine's
statements without it, then puts it on their engine through the move ledger. Aurora,
which has temporary tables, is the last resort: no routine named, two named, a routine
with nothing routed, or statements on more than one engine. The fixture is synthetic
(``tests/fixtures/sqlserver_temp_with_procedure_collection.json``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee import (
    assignment_resolver,
    co_dependency,
    consolidation_validator,
    pending_choices,
    reality_check_rules,
)
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.assignment_overrides import (
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.move_ledger import apply_to_assignment, ledger_for
from src.agents.referee.post_schema_routing_handler import PostSchemaRouting, router_held_ids
from src.agents.referee.reality_check_handler import (
    _settle_records,
    run_reality_check_deterministic,
    run_reality_check_handler,
)
from src.agents.referee.routine_units import (
    RoutineFollowers,
    follow_reason,
    routine_followers,
    unplaced_reason,
)
from src.agents.referee.synthesis_grounding import _GROUPS_NEVER_TOP
from src.agents.referee.synthesis_handler import run_synthesis
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.triage import detect_query_capabilities
from src.agents.referee.triage_handler import run_triage
from src.agents.referee.utility_statements import PROCEDURE_INTERNAL_REASON
from src.agents.schema_design.handler import filter_collector_for_assignment
from src.contracts.move_ledger import MovePass, ReasonCode
from src.contracts.post_schema_router_output import QueryRouting
from src.report.engineering_report import (
    _procedure_internal_routine_on_text,
    _procedure_internal_routine_text,
    _procedure_internal_text,
    render_engineering_report_md,
)
from src.shared.report_facts import (
    BLAME_GROUP_LABELS,
    PROCEDURE_INTERNAL_GROUP_LABEL,
    PROCEDURE_INTERNAL_ROUTINE_GROUP_LABEL,
)
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / "tests" / "fixtures" / "sqlserver_temp_with_procedure_collection.json"
DB = "TempWithProcedure"
JOB = "job-1891"
SQLSERVER = SourceScope(DB, "sqlserver")
AURORA = {"aurora_postgresql", "aurora_mysql"}
SEARCH = "dbo.GetUsersAdvancedSearch"
ADD_ORDER = "dbo.AddOrder"

# Each follower and its routine's routed statements (its leaders).
FOLLOWERS = {"search-count": ("search-read",), "order-tmp": ("order-insert",)}
ROUTINE = {"search-count": SEARCH, "order-tmp": ADD_ORDER}
# Procedure-internal statements the #1876 pin keeps: no routine named (None, or []: the
# collection says it ran in none), two routines, a routine with nothing routed.
PINNED = ("no-routine-temp", "adhoc-temp", "shared-temp", "only-temp")
# The busy key-value reads that keep DynamoDB in the plan; without them the reality check
# sweeps DynamoDB away, and the routine's statements move with the follower behind them.
BUSY = ("app-order-get", "app-order-total", "app-user-name", "app-order-delete")


def _collection(*, links: bool = True, busy: bool = True) -> dict:
    collection: dict = json.loads(FIXTURE.read_text())
    patterns = collection["queries"]["query_patterns"]
    if not busy:
        collection["queries"]["query_patterns"] = [q for q in patterns if q["query_id"] not in BUSY]
    if not links:
        for q in collection["queries"]["query_patterns"]:
            q.pop("parent_routine_ids", None)
    return collection


def _queries(**kwargs: bool) -> list[dict]:
    return list(_collection(**kwargs)["queries"]["query_patterns"])


def _run(
    base: Path, *, scope: list[str] | None = None, collection: dict | None = None, **kwargs: bool
) -> tuple[LocalArtifactStore, int]:
    store = LocalArtifactStore(base_dir=str(base))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collection or _collection(**kwargs))
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


def _by_id(store: LocalArtifactStore, version: int) -> dict[str, dict]:
    return {qa["query_id"]: qa for qa in _raw(store, version)["query_assignments"]}


def _follows(qas: dict[str, dict], qid: str) -> None:
    """``qid`` is on its leaders' engine, by the follow rule."""
    leaders = {qas[m]["assigned_engine"] for m in FOLLOWERS[qid]}
    assert leaders == {qas[qid]["assigned_engine"]}, qid
    assert qas[qid]["assignment_reason"] == follow_reason(
        PROCEDURE_INTERNAL_REASON, ROUTINE[qid], qas[qid]["assigned_engine"]
    )
    assert qas[qid]["pinned"] is None
    assert qas[qid]["source_tables"] == []
    assert qas[qid]["tied_engines"] == []


class _GroupSpy:
    """Every member list a group rule (``group_engine_scores``) scored or gated."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.members: list[list[str]] = []
        original = co_dependency.group_engine_scores

        def spy(members, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            members = list(members)
            self.members.append(members)
            return original(members, *args, **kwargs)

        for module in (
            co_dependency,
            assignment_resolver,
            reality_check_rules,
            consolidation_validator,
            pending_choices,
        ):
            mp.setattr(module, "group_engine_scores", spy)

    def assert_no_follower(self, *, scored: bool = True) -> None:
        assert self.members or not scored, "no group was scored: the spy is not on the rule"
        leaked = [m for m in self.members if set(m) & set(FOLLOWERS)]
        assert leaked == [], f"a unit was scored or gated on a follower: {leaked}"


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> Iterator[tuple[LocalArtifactStore, int]]:
    with pytest.MonkeyPatch.context() as mp:
        spy = _GroupSpy(mp)
        out = _run(tmp_path_factory.mktemp("busy"))
        spy.assert_no_follower()
    yield out


@pytest.fixture(scope="module")
def swept(tmp_path_factory) -> Iterator[tuple[LocalArtifactStore, int]]:
    """No busy reads: the reality check sweeps DynamoDB and moves the routine's writes."""
    with pytest.MonkeyPatch.context() as mp:
        spy = _GroupSpy(mp)
        out = _run(tmp_path_factory.mktemp("swept"), busy=False)
        spy.assert_no_follower()
    yield out


@pytest.fixture(scope="module")
def without_links(tmp_path_factory) -> tuple[LocalArtifactStore, int]:
    """The same collection as one gathered before #1891: no routine is recorded."""
    return _run(tmp_path_factory.mktemp("without-links"), links=False)


@pytest.fixture(scope="module")
def report(pipeline) -> dict:
    store, version = pipeline
    out: dict = store.read_json(f"{DB}/{JOB}/synthesis/v{version}/report.json")
    return out


# -- Which statements follow their routine --------------------------------------------


def test_a_temp_statement_follows_its_routine() -> None:
    assert routine_followers(_queries(), SQLSERVER) == ROUTINE


@pytest.mark.parametrize("qid", [*PINNED, "search-nocount"])
def test_with_no_single_routine_to_follow_it_follows_none(qid: str) -> None:
    # ``search-nocount`` (SET NOCOUNT ON) runs in the search procedure but is
    # administration, not a statement on temporary objects: it keeps its utility pin.
    assert qid not in routine_followers(_queries(), SQLSERVER)


def test_only_a_sql_server_source_has_followers() -> None:
    assert routine_followers(_queries(), SourceScope(DB, "postgresql")) == {}
    assert routine_followers(_queries(), None) == {}


def test_a_collection_that_names_no_routine_has_none() -> None:
    assert routine_followers(_queries(links=False), SQLSERVER) == {}


def test_a_follower_requires_no_capability() -> None:
    """It neither scores nor gates, so triage records no capability for it."""
    caps = detect_query_capabilities(_collection(), [])
    assert not set(FOLLOWERS) & set(caps)
    # The same statement with no routine named still needs the relational engine.
    assert "sql_admin" in detect_query_capabilities(_collection(links=False), [])["order-tmp"]


def test_the_follow_target_is_its_leaders_one_engine() -> None:
    followers = RoutineFollowers.of(_queries(), SQLSERVER)
    assert followers.target("order-tmp", {"order-insert": "dynamodb"}) == "dynamodb"
    # Its routine's statements on two engines, or on none: no target (the last resort).
    assert followers.target("order-tmp", {}) is None
    assert followers.target("order-tmp", {"order-insert": "unservable"}) is None
    # An engine that may not own the follower (a cache, for its insert) is no target.
    assert followers.target("order-tmp", {"order-insert": "elasticache"}) is None


# -- Routing ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["first", "final"])
@pytest.mark.parametrize("qid", sorted(FOLLOWERS))
def test_it_follows_its_routine(pipeline, qid: str, version: str) -> None:
    store, final = pipeline
    _follows(_by_id(store, 1 if version == "first" else final), qid)


def test_a_temp_step_never_pulls_its_routine_to_aurora(pipeline, without_links) -> None:
    """dbo.AddOrder's insert and the ad hoc UPDATE of its table stay on DynamoDB, as
    without the link, and the statement on temporary objects follows them there."""
    store, final = pipeline
    old_store, old_final = without_links
    for version, old_version in ((1, 1), (final, old_final)):
        qas, old = _by_id(store, version), _by_id(old_store, old_version)
        for qid in ("order-insert", "app-order-update", "search-read"):
            assert qas[qid]["assigned_engine"] == old[qid]["assigned_engine"], qid
        assert qas["order-insert"]["assigned_engine"] == "dynamodb"
        assert qas["app-order-update"]["assigned_engine"] == "dynamodb"
        assert qas["order-tmp"]["assigned_engine"] == "dynamodb"
        # Without the link it was pinned to the relational engine (#1876).
        assert old["order-tmp"]["pinned"] == "utility"


def test_the_routing_of_every_other_statement_is_unchanged(pipeline, without_links) -> None:
    store, final = pipeline
    old_store, old_final = without_links
    for version, old_version in ((1, 1), (final, old_final)):
        qas, old = _by_id(store, version), _by_id(old_store, old_version)
        for qid in set(qas) - set(FOLLOWERS):
            assert (qas[qid]["assigned_engine"], qas[qid]["pinned"]) == (
                old[qid]["assigned_engine"],
                old[qid]["pinned"],
            ), qid


@pytest.mark.parametrize("qid", sorted(FOLLOWERS))
def test_the_ledger_places_it_after_its_routine(pipeline, qid: str) -> None:
    store, _ = pipeline
    raw = _raw(store, 1)
    moves = [m for m in raw["moves"] if m["query_id"] == qid]
    assert [(m["pass"], m["reason_code"]) for m in moves] == [
        (MovePass.CO_DEPENDENCY.value, ReasonCode.ROUTINE_FOLLOW.value)
    ]
    leader_seqs = [m["seq"] for m in raw["moves"] if m["query_id"] in FOLLOWERS[qid]]
    assert max(leader_seqs) < moves[0]["seq"]


def test_it_joins_no_unit_or_write_group(pipeline) -> None:
    raw = _raw(pipeline[0], 1)
    assert not any(set(FOLLOWERS) & set(g) for g in raw["co_dependency_groups"])
    assert not any(set(FOLLOWERS) & set(g) for g in raw["write_groups"].values())


def test_one_writer_per_table_holds(pipeline) -> None:
    store, final = pipeline
    for version in (1, final):
        qas = _by_id(store, version)
        writers = {qas[q]["assigned_engine"] for q in ("order-insert", "app-order-update")}
        assert len(writers) == 1


@pytest.mark.parametrize("qid", PINNED)
def test_with_no_routine_to_follow_the_pin_holds(pipeline, qid: str) -> None:
    qa = _by_id(pipeline[0], 1)[qid]
    assert qa["pinned"] == "utility"
    assert qa["assignment_reason"].startswith(f"{PROCEDURE_INTERNAL_REASON}, kept on the ")
    assert qa["source_tables"] == []
    assert qa["assigned_engine"] in AURORA


@pytest.mark.passes_on_main  # main pins every temp-only statement already
def test_a_collection_without_links_keeps_every_pin(without_links) -> None:
    store, version = without_links
    for v in (1, version):
        qas = _by_id(store, v)
        for qid in (*FOLLOWERS, *PINNED):
            assert qas[qid]["pinned"] == "utility", qid


# -- Later passes ------------------------------------------------------------------------


def test_the_reality_check_moves_it_with_its_routine(swept) -> None:
    """The sweep moves the routine's writes off DynamoDB; the follower, held out of the
    check, follows them, and the check counts it on no engine."""
    store, final = swept
    v1, last = _by_id(store, 1), _by_id(store, final)
    assert final > 1
    assert v1["order-tmp"]["assigned_engine"] == "dynamodb"
    assert last["order-insert"]["assigned_engine"] != "dynamodb"
    _follows(last, "order-tmp")
    moves = [m for m in _raw(store, final)["moves"] if m["query_id"] == "order-tmp"]
    assert [m["reason_code"] for m in moves] == [ReasonCode.ROUTINE_FOLLOW.value]


def test_the_reality_check_holds_it_out(swept) -> None:
    store, _ = swept
    det = run_reality_check_deterministic(JOB, DB, store)
    records = {c.get("query_id") for c in det["consolidations"]} | {
        q for c in det["consolidations"] for q in c.get("query_ids") or []
    }
    assert not set(FOLLOWERS) & records


def test_a_model_flag_that_moves_its_routine_moves_it(swept) -> None:
    """Whatever moved the routine's statements (a model's flag included), the records
    are settled with the follower on their engine."""
    store, _ = swept
    det = run_reality_check_deterministic(JOB, DB, store)
    ledger = det["move_ledger"]
    by_id = {qa["query_id"]: qa for qa in det["revised_assignments"]}
    for qid in ("order-insert", "app-order-update"):
        apply_to_assignment(
            ledger,
            by_id[qid],
            "dynamodb",
            pass_=MovePass.MODEL_FLAG,
            reason_code=ReasonCode.FLAG_CAPABILITY_GAP,
            score_after=50.0,
        )
    _settle_records(det)
    assert by_id["order-tmp"]["assigned_engine"] == "dynamodb"


def test_a_customer_move_of_its_routine_moves_it(tmp_path) -> None:
    store, _ = _run(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        spy = _GroupSpy(mp)
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("search-read", assigned_engine="dynamodb")]
        )
        # A customer pick places no group by score; any group it did score left it out.
        spy.assert_no_follower(scored=False)
    qas = {qa.query_id: qa.model_dump(mode="json") for qa in result.assignment.query_assignments}
    _follows(qas, "search-count")
    assert qas["search-count"]["assigned_engine"] == "dynamodb"


def test_a_customer_pick_of_the_follower_itself_stays(tmp_path) -> None:
    store, _ = _run(tmp_path)
    result = apply_assignment_overrides(
        store,
        DB,
        JOB,
        [QueryOverrideInput("order-tmp", assigned_engine="aurora_postgresql")],
    )
    qas = {qa.query_id: qa for qa in result.assignment.query_assignments}
    assert qas["order-tmp"].assigned_engine == "aurora_postgresql"
    assert qas["order-insert"].assigned_engine == "dynamodb"


def test_a_post_schema_reroute_and_its_rollback_move_it(pipeline) -> None:
    store, version = pipeline
    handler = PostSchemaRouting(store, redesign=lambda *a, **k: None)
    assignment = _raw(store, version)
    routings = [
        QueryRouting(
            query_id=qid,
            from_engine="dynamodb",
            to_engine="aurora_postgresql",
            reason="the design cannot serve it",
            score_after=50.0,
        )
        for qid in ("order-insert", "app-order-update")
    ]
    rerouted = handler._rerouted_assignment(
        JOB, DB, copy.deepcopy(assignment), version, version + 1, routings
    )
    qas = {qa["query_id"]: qa for qa in rerouted["query_assignments"]}
    _follows(qas, "order-tmp")
    assert qas["order-tmp"]["assigned_engine"] == "aurora_postgresql"
    # The rollback undoes the reroute's own Moves, the follow included.
    back = PostSchemaRouting._rollback_moves(rerouted, assignment)
    undone = [m for m in back if m["query_id"] == "order-tmp"]
    assert [m["to_engine"] for m in undone] == ["dynamodb"]


def test_the_router_never_moves_it(pipeline) -> None:
    store, version = pipeline
    handler = PostSchemaRouting(store, redesign=lambda *a, **k: None)
    scoring = handler._router_scoring_inputs(JOB, DB, ["dynamodb", "aurora_postgresql"], version)
    held = router_held_ids(_raw(store, version), scoring["followers"])
    assert set(FOLLOWERS) <= held
    assert not set(PINNED) & held


def test_no_design_serves_it(pipeline) -> None:
    store, version = pipeline
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    filtered = filter_collector_for_assignment(collector, _raw(store, version), "dynamodb")
    ids = {q["query_id"] for q in filtered["queries"]["query_patterns"]}
    assert "order-insert" in ids and "order-tmp" not in ids


def test_the_last_resort_pins_it_when_its_routine_splits(pipeline) -> None:
    from src.agents.referee.routine_units import follow_routines

    store, version = pipeline
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    queries = [
        *collector["queries"]["query_patterns"],
        {
            "query_id": "order-insert-2",
            "query_text": "SELECT Total FROM dbo.Orders WHERE UserId = ?",
            "query_type": "SELECT",
            "calls_per_second": 1.0,
            "tables_accessed": ["dbo.Orders"],
            "parent_routine_ids": [ADD_ORDER],
        },
    ]
    followers = RoutineFollowers.of(queries, SQLSERVER)
    qas = copy.deepcopy(_raw(store, version)["query_assignments"])
    qas.append({**copy.deepcopy(qas[0]), "query_id": "order-insert-2"})
    by_id = {qa["query_id"]: qa for qa in qas}
    by_id["order-insert-2"]["assigned_engine"] = "aurora_postgresql"
    ledger = ledger_for(qas)
    follow_routines(qas, followers, ledger, relational_engine="aurora_postgresql")
    assert by_id["order-tmp"]["assigned_engine"] == "aurora_postgresql"
    assert by_id["order-tmp"]["pinned"] == "utility"
    assert by_id["order-tmp"]["assignment_reason"].startswith(
        f"{PROCEDURE_INTERNAL_REASON}, kept on the chosen relational engine"
    )


# -- The deliverables -----------------------------------------------------------------


def test_the_report_groups_and_counts_it_apart_from_the_pinned_ones(report) -> None:
    def grouped(label: str) -> set[str]:
        return {
            q
            for g in report["query_groups"]
            if g["group_name"] == label
            for ap in g["access_patterns"]
            for q in ap["query_ids"]
        }

    # The relational one is listed in its engine's no-table group; the DynamoDB one is in
    # no group (no design serves it), and both are in the report's followers.
    assert grouped(PROCEDURE_INTERNAL_ROUTINE_GROUP_LABEL) == {"search-count"}
    assert grouped(PROCEDURE_INTERNAL_GROUP_LABEL) == set(PINNED)
    assert report["routine_followers"] == [
        {"query_id": "search-count", "routine_id": SEARCH, "engine": "aurora_postgresql"},
        {"query_id": "order-tmp", "routine_id": ADD_ORDER, "engine": "dynamodb"},
    ]
    counts = report["facts"]["query_group_counts"]
    assert counts["procedure_internal_routine_queries"] == len(FOLLOWERS)
    assert counts["procedure_internal_queries"] == len(PINNED)


@pytest.mark.passes_on_main  # main never groups a statement with its routine
def test_without_links_nothing_follows_its_routine(without_links) -> None:
    store, version = without_links
    report = store.read_json(f"{DB}/{JOB}/synthesis/v{version}/report.json")
    assert PROCEDURE_INTERNAL_ROUTINE_GROUP_LABEL not in {
        g["group_name"] for g in report["query_groups"]
    }
    assert report["facts"]["query_group_counts"].get("procedure_internal_routine_queries", 0) == 0


def test_the_group_is_a_blame_group_never_a_busiest_one() -> None:
    assert PROCEDURE_INTERNAL_ROUTINE_GROUP_LABEL in BLAME_GROUP_LABELS
    assert PROCEDURE_INTERNAL_ROUTINE_GROUP_LABEL in _GROUPS_NEVER_TOP


def test_the_engineering_report_states_each_engines_figure_in_its_section(report) -> None:
    md = render_engineering_report_md(report)
    aurora = md[md.index("## Queries on Aurora by table") :]
    aurora = aurora[: aurora.index("\n## ", 3)]
    # One on Aurora PostgreSQL, in its section; the DynamoDB one under the target engines.
    assert _procedure_internal_routine_text(1) in aurora
    assert _procedure_internal_routine_text(2) not in md
    targets = md[md.index("## Target engines") :]
    targets = targets[: targets.index("\n## ", 3)]
    assert _procedure_internal_routine_on_text("Amazon DynamoDB", 1) in targets or (
        _procedure_internal_routine_on_text("DynamoDB", 1) in targets
    )
    assert _procedure_internal_text(len(PINNED)) in md
    assert report["facts"]["query_group_counts"]["procedure_internal_routine_counts"] == {
        "aurora_postgresql": 1,
        "dynamodb": 1,
    }


def test_with_no_follower_on_aurora_the_aurora_section_says_none(report) -> None:
    report = json.loads(json.dumps(report))
    report["routine_followers"] = [
        f for f in report["routine_followers"] if f["engine"] == "dynamodb"
    ]
    from src.report.facts import report_facts

    report.pop("facts", None)
    report["facts"] = report_facts(report)
    md = render_engineering_report_md(report)
    assert (
        "follows the routine"
        not in md[md.index("## Queries on Aurora by table") :].split("\n## ")[0]
    )


def test_the_engineering_report_formats_the_stored_figure(report) -> None:
    report = json.loads(json.dumps(report))
    report["facts"]["query_group_counts"]["procedure_internal_routine_counts"] = {
        "aurora_postgresql": 3
    }
    md = render_engineering_report_md(report)
    assert _procedure_internal_routine_text(3) in md
    assert _procedure_internal_routine_text(1) not in md
    report["facts"]["query_group_counts"]["procedure_internal_routine_counts"] = {}
    assert "follow the routine" not in render_engineering_report_md(report)
    assert "follows the routine" not in render_engineering_report_md(report)


@pytest.mark.parametrize("n", [1, 7])
def test_the_ui_and_the_engineering_report_say_the_same_sentence(n: int) -> None:
    en = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
    key = "analysis-results.aurora-design.procedure-internal-routine_" + (
        "one" if n == 1 else "other"
    )
    assert en[key].replace("{{count}}", str(n)) == _procedure_internal_routine_text(n)


@pytest.mark.parametrize("n", [1, 7])
def test_the_ui_and_the_engineering_report_name_another_engine_alike(n: int) -> None:
    en = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
    key = "analysis-results.procedure-internal-routine-on_" + ("one" if n == 1 else "other")
    ui = en[key].replace("{{count}}", str(n)).replace("{{engine}}", "DynamoDB")
    assert ui == _procedure_internal_routine_on_text("DynamoDB", n)


# -- One classification in every pass, whatever the engine scope -----------------------

SCOPE = ["dynamodb", "documentdb"]


def _split_collection() -> dict:
    """dbo.AddOrder's routed statements on two engines: search-read runs in it too."""
    collection = _collection(busy=False)
    for q in collection["queries"]["query_patterns"]:
        if q["query_id"] == "search-read":
            q["parent_routine_ids"] = [ADD_ORDER, SEARCH]
    return collection


@pytest.fixture(scope="module")
def scoped(tmp_path_factory) -> Iterator[tuple[LocalArtifactStore, int]]:
    """No Aurora engine in the scope (the review's DynamoDB + DocumentDB)."""
    with pytest.MonkeyPatch.context() as mp:
        spy = _GroupSpy(mp)
        out = _run(tmp_path_factory.mktemp("scoped"), scope=SCOPE, busy=False)
        spy.assert_no_follower()
    yield out


@pytest.fixture(scope="module")
def scoped_split(tmp_path_factory) -> tuple[LocalArtifactStore, int]:
    return _run(
        tmp_path_factory.mktemp("scoped-split"), scope=SCOPE, collection=_split_collection()
    )


def _versions(store: LocalArtifactStore, final: int) -> list[int]:
    return sorted({1, final})


def test_with_no_aurora_in_scope_it_still_follows(scoped) -> None:
    store, final = scoped
    for version in _versions(store, final):
        qas = _by_id(store, version)
        for qid in FOLLOWERS:
            _follows(qas, qid)
            assert qas[qid]["assigned_engine"] in SCOPE


def test_with_no_aurora_in_scope_it_is_never_tied_or_a_choice(scoped) -> None:
    store, final = scoped
    for version in _versions(store, final):
        qas = _by_id(store, version)
        assert all(qas[qid]["tied_engines"] == [] for qid in FOLLOWERS)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v{final}/report.json")
    pending = {
        q for c in report.get("pending_engine_choices") or [] for q in c.get("query_ids") or []
    }
    assert not set(FOLLOWERS) & pending
    assert not set(FOLLOWERS) & {t.get("query_id") for t in report.get("tied_queries") or []}


def test_every_pass_reads_the_same_followers(scoped, pipeline, swept) -> None:
    """The first assignment, the reality check and synthesis agree on who follows."""
    for store, final in (scoped, pipeline, swept):
        collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
        expected = set(RoutineFollowers.from_collector(collector).routine_of)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v{final}/report.json")
        assert {f["query_id"] for f in report["routine_followers"]} == expected == set(FOLLOWERS)
        for version in _versions(store, final):
            for move in _raw(store, version)["moves"]:
                if move["query_id"] in expected:
                    # Never placed by its own score, a tie or a fallback.
                    assert move["reason_code"] in {
                        ReasonCode.ROUTINE_FOLLOW.value,
                        ReasonCode.OVERRIDE_UTILITY.value,
                    }, move


def test_with_no_relational_engine_the_last_resort_is_unservable(scoped_split) -> None:
    store, final = scoped_split
    for version in _versions(store, final):
        raw = _raw(store, version)
        assert "order-tmp" not in {qa["query_id"] for qa in raw["query_assignments"]}
        (record,) = [u for u in raw["unservable_queries"] if u["query_id"] == "order-tmp"]
        assert record["reason"] == unplaced_reason(PROCEDURE_INTERNAL_REASON, ADD_ORDER)
        # Its routine's other follower still follows.
        _follows(_by_id(store, version), "search-count")


def test_followers_count_in_no_aurora_choice(pipeline) -> None:
    """The heterogeneous Aurora choice (ADR-031 Decision 6) totals no follower's score."""
    store, _ = pipeline
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    # A follower the scorer gives a real score on the Aurora engines (the regex fallback
    # reads a table on ``@words``, as on the DNN collection).
    for q in collector["queries"]["query_patterns"]:
        if q["query_id"] == "order-tmp":
            q["query_text"] = "IF NOT EXISTS (SELECT 1 FROM @words WHERE [string] = @s)"
            q["query_type"] = "SELECT"
    scores = _run_resolver_only(store, collector, score_of="order-tmp")
    assert any(scores.values()), "the follower must score on an Aurora engine for this test"
    without = copy.deepcopy(collector)
    without["queries"]["query_patterns"] = [
        q for q in without["queries"]["query_patterns"] if q["query_id"] not in FOLLOWERS
    ]
    totals = _run_resolver_only(store, collector)["aurora_engine_choice"]["totals"]
    assert totals == _run_resolver_only(store, without)["aurora_engine_choice"]["totals"]


def _run_resolver_only(
    store: LocalArtifactStore, collector: dict, score_of: str | None = None
) -> dict:
    """The first assignment of ``collector``, or with ``score_of`` that query's Aurora
    scores as the resolver computed them."""
    from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace

    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    analyses = {
        a["agent_type"]: store.read_json(f"{DB}/{JOB}/analysis-{a['agent_type']}/analysis.json")
        for a in triage["selected_agents"]
    }
    trace = RoutingTrace()
    out: dict = (
        AssignmentResolver()
        .resolve(triage, analyses, collector, routing_trace=trace)
        .model_dump(mode="json")
    )
    if score_of is None:
        return out
    ladder = trace.by_query[score_of]["engines"]
    return {e: v for e, v in ladder.items() if e in AURORA and isinstance(v, (int, float))}


def test_its_borrowed_score_counts_in_no_engine_fit(pipeline) -> None:
    from src.agents.referee.routed_confidence import routed_fits

    store, version = pipeline
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    analyses = {
        a["agent_type"]: store.read_json(f"{DB}/{JOB}/analysis-{a['agent_type']}/analysis.json")
        for a in triage["selected_agents"]
    }
    assignment = _raw(store, version)
    patterns = collector["queries"]["query_patterns"]
    fits = routed_fits(assignment, triage, patterns, analyses, source_engine="sqlserver")
    stripped = copy.deepcopy(assignment)
    stripped["query_assignments"] = [
        qa for qa in stripped["query_assignments"] if qa["query_id"] not in FOLLOWERS
    ]
    assert fits == routed_fits(stripped, triage, patterns, analyses, source_engine="sqlserver")


def test_a_stale_tie_or_pin_on_its_target_is_cleared(pipeline) -> None:
    from src.agents.referee.routine_units import follow_routines

    store, version = pipeline
    qas = copy.deepcopy(_raw(store, version)["query_assignments"])
    by_id = {qa["query_id"]: qa for qa in qas}
    by_id["order-tmp"]["tied_engines"] = ["documentdb", "dynamodb"]
    by_id["search-count"]["pinned"] = "utility"
    ledger = ledger_for(qas)
    moved = follow_routines(
        qas, RoutineFollowers.from_collector(_collection()), ledger, relational_engine=None
    )
    assert set(moved) == set(FOLLOWERS)
    for qid in FOLLOWERS:
        _follows(by_id, qid)


def test_the_post_schema_reroute_follows_after_the_counts(pipeline, monkeypatch) -> None:
    """A count among the routine's statements re-placed by the reroute moves the
    follower too: the follow runs after the counts are re-placed."""
    store, version = pipeline
    handler = PostSchemaRouting(store, redesign=lambda *a, **k: None)

    def replace_counts(self, prefix, assignment, query_assignments, ledger, *rest):  # noqa: ANN001
        for qa in query_assignments:
            if qa["query_id"] == "search-read":
                apply_to_assignment(
                    ledger,
                    qa,
                    "dynamodb",
                    pass_=MovePass.POST_SCHEMA_FLAG,
                    reason_code=ReasonCode.KEY_SCOPED_COUNT,
                    score_after=50.0,
                )
        return []

    monkeypatch.setattr(PostSchemaRouting, "_replace_lapsed_counts", replace_counts)
    rerouted = handler._rerouted_assignment(
        JOB, DB, copy.deepcopy(_raw(store, version)), version, version + 1, []
    )
    qas = {qa["query_id"]: qa for qa in rerouted["query_assignments"]}
    assert qas["search-read"]["assigned_engine"] == "dynamodb"
    _follows(qas, "search-count")


def test_a_dynamodb_follower_is_no_ungrouped_query(report) -> None:
    from src.shared.report_facts import query_group_facts

    report = copy.deepcopy(report)
    report["query_pattern_groups"] = [
        {"members": [{"query_id": "order-tmp", "engine": "dynamodb"}]},
        {"members": [{"query_id": "app-user", "engine": "dynamodb"}]},
    ]
    rows = (query_group_facts(report) or {})["ungrouped_queries"]
    assert [(r["engine"], r["query_count"]) for r in rows] == [("dynamodb", 1)]


def test_assess_reports_a_follower_as_co_dependency(tmp_path) -> None:
    from src.assessment import assess
    from src.contracts.collector_output import CollectorOutputContract
    from src.shared.contract_versioning import current_version

    collection = {**_collection(), "contract_version": current_version(CollectorOutputContract)}
    result = assess(
        {"database_name": DB, "collection": collection},
        store=LocalArtifactStore(base_dir=str(tmp_path)),
    )
    by_id = {s.query_id: s for s in result.statements}
    assert by_id["order-tmp"].engine == "dynamodb"
    assert by_id["order-tmp"].reason_code == "co_dependency"
    assert by_id["order-tmp"].tied_engines == []
    assert by_id["order-tmp"].reason_text == follow_reason(
        PROCEDURE_INTERNAL_REASON, ADD_ORDER, "dynamodb"
    )


# -- Later passes with no relational engine in scope -----------------------------------


def _two_leader_collection() -> dict:
    """dbo.AddOrder runs a read of dbo.Orders too: two leaders, both on DynamoDB at first."""
    collection = _collection(busy=False)
    for q in collection["queries"]["query_patterns"]:
        if q["query_id"] == "app-orders":
            q["parent_routine_ids"] = [ADD_ORDER]
    return collection


def _sweep_moving(mp: pytest.MonkeyPatch, qid: str, engine: str) -> None:
    """The sanity sweep, then a move of ``qid`` to ``engine`` through the ledger, as the
    sweep's own Moves are: a later pass that leaves the routine's statements elsewhere."""
    from src.agents.referee import reality_check_handler as handler

    original = handler.sanity_sweep

    def sweep(revised, consolidations, *args, ledger, **kwargs):  # noqa: ANN001, ANN003
        revised, consolidations, decisions = original(
            revised, consolidations, *args, ledger=ledger, **kwargs
        )
        for qa in revised:
            if qa["query_id"] == qid and qa["assigned_engine"] != engine:
                apply_to_assignment(
                    ledger,
                    qa,
                    engine,
                    pass_=MovePass.SWEEP,
                    reason_code=ReasonCode.SWEEP_ORPHAN,
                    score_after=50.0,
                )
        return revised, consolidations, decisions

    mp.setattr(handler, "sanity_sweep", sweep)


def _unplaced(store: LocalArtifactStore, version: int) -> None:
    raw = _raw(store, version)
    assert "order-tmp" not in {qa["query_id"] for qa in raw["query_assignments"]}
    (record,) = [u for u in raw["unservable_queries"] if u["query_id"] == "order-tmp"]
    assert record["reason"] == unplaced_reason(PROCEDURE_INTERNAL_REASON, ADD_ORDER)
    moves = [m for m in raw["moves"] if m["query_id"] == "order-tmp"]
    assert [m["to_engine"] for m in moves][-1:] == ["unservable"]


def test_a_sweep_that_splits_its_routine_makes_it_unservable(tmp_path) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _sweep_moving(mp, "app-orders", "documentdb")
        store, final = _run(tmp_path, scope=SCOPE, collection=_two_leader_collection())
    assert _by_id(store, 1)["order-tmp"]["assigned_engine"] == "dynamodb"
    assert final > 1
    _unplaced(store, final)


def test_a_sweep_that_joins_its_routine_brings_it_back(tmp_path) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _sweep_moving(mp, "search-read", "dynamodb")
        store, final = _run(tmp_path, scope=SCOPE, collection=_split_collection())
    assert "order-tmp" in {u["query_id"] for u in _raw(store, 1)["unservable_queries"]}
    assert final > 1
    last = _by_id(store, final)
    _follows(last, "order-tmp")
    assert last["order-tmp"]["assigned_engine"] == "dynamodb"
    assert "order-tmp" not in {u["query_id"] for u in _raw(store, final)["unservable_queries"]}


def test_a_customer_edit_that_splits_its_routine_makes_it_unservable(tmp_path) -> None:
    store, _ = _run(tmp_path, scope=SCOPE, collection=_two_leader_collection())
    result = apply_assignment_overrides(
        store, DB, JOB, [QueryOverrideInput("app-orders", assigned_engine="documentdb")]
    )
    _unplaced(store, result.assignment.version)


def test_a_post_schema_reroute_that_splits_its_routine_makes_it_unservable(tmp_path) -> None:
    store, version = _run(tmp_path, scope=SCOPE, collection=_two_leader_collection())
    handler = PostSchemaRouting(store, redesign=lambda *a, **k: None)
    routing = QueryRouting(
        query_id="app-orders",
        from_engine="dynamodb",
        to_engine="documentdb",
        reason="the design cannot serve it",
        score_after=50.0,
    )
    rerouted = handler._rerouted_assignment(
        JOB, DB, copy.deepcopy(_raw(store, version)), version, version + 1, [routing]
    )
    assert "order-tmp" not in {qa["query_id"] for qa in rerouted["query_assignments"]}
    (record,) = [u for u in rerouted["unservable_queries"] if u["query_id"] == "order-tmp"]
    assert record["reason"] == unplaced_reason(PROCEDURE_INTERNAL_REASON, ADD_ORDER)
    # The rollback takes it from unservable back onto its routine's engine.
    undone = [
        m
        for m in PostSchemaRouting._rollback_moves(rerouted, _raw(store, version))
        if m["query_id"] == "order-tmp"
    ]
    assert [(m["from_engine"], m["to_engine"]) for m in undone] == [("unservable", "dynamodb")]


def test_the_last_resort_engine_is_the_chosen_then_the_one_in_use() -> None:
    from src.agents.referee.routine_units import relational_engine_of

    collector = _collection()
    chosen = {"aurora_engine_choice": {"engine": "aurora_mysql"}, "query_assignments": []}
    assert relational_engine_of(chosen, collector) == ("aurora_mysql", False)
    in_use = {"query_assignments": [{"query_id": "q", "assigned_engine": "aurora_postgresql"}]}
    assert relational_engine_of(in_use, collector) == ("aurora_postgresql", False)
    # The first assignment's pick, where it pinned the utility statements, comes first.
    picked = {
        "query_assignments": [
            {"query_id": "q", "assigned_engine": "aurora_postgresql"},
            {"query_id": "u", "assigned_engine": "aurora_mysql", "pinned": "utility"},
        ]
    }
    assert relational_engine_of(picked, collector) == ("aurora_mysql", False)
    # No Aurora engine anywhere (an engine scope that leaves both out): none.
    none = {"query_assignments": [{"query_id": "q", "assigned_engine": "dynamodb"}]}
    assert relational_engine_of(none, collector) == (None, False)


def test_a_reroute_that_brings_it_back_rolls_back_to_unservable(scoped_split, caplog) -> None:
    """The review's case: unservable in v1, back on DynamoDB after a reroute moves its
    routine's other statement there, and unservable again after the rollback. The Move
    back leaves ``unservable``, so the history replays and the rollback has somewhere to
    go back to."""
    store, version = scoped_split
    v1 = _raw(store, version)
    assert "order-tmp" in {u["query_id"] for u in v1["unservable_queries"]}
    handler = PostSchemaRouting(store, redesign=lambda *a, **k: None)
    routing = QueryRouting(
        query_id="search-read",
        from_engine="documentdb",
        to_engine="dynamodb",
        reason="the design cannot serve it",
        score_after=50.0,
    )
    with caplog.at_level("WARNING", logger="src.agents.referee.move_ledger"):
        rerouted = handler._rerouted_assignment(
            JOB, DB, copy.deepcopy(v1), version, version + 1, [routing]
        )
    assert "without a Move" not in caplog.text
    qas = {qa["query_id"]: qa for qa in rerouted["query_assignments"]}
    _follows(qas, "order-tmp")
    assert "order-tmp" not in {u["query_id"] for u in rerouted["unservable_queries"]}
    (back_move,) = [m for m in rerouted["moves"] if m["query_id"] == "order-tmp"]
    assert (back_move["from_engine"], back_move["to_engine"]) == ("unservable", "dynamodb")
    undone = [
        m for m in PostSchemaRouting._rollback_moves(rerouted, v1) if m["query_id"] == "order-tmp"
    ]
    assert [(m["from_engine"], m["to_engine"]) for m in undone] == [("dynamodb", "unservable")]


def test_a_follower_that_comes_back_logs_no_unexplained_engine(tmp_path, caplog) -> None:
    with (
        pytest.MonkeyPatch.context() as mp,
        caplog.at_level("WARNING", logger="src.agents.referee.move_ledger"),
    ):
        _sweep_moving(mp, "search-read", "dynamodb")
        store, final = _run(tmp_path, scope=SCOPE, collection=_split_collection())
    assert "without a Move" not in caplog.text
    (move,) = [m for m in _raw(store, final)["moves"] if m["query_id"] == "order-tmp"]
    assert (move["from_engine"], move["to_engine"]) == ("unservable", "dynamodb")


def test_a_follower_counts_in_no_settled_workload(pipeline) -> None:
    """Decision 12's settled workload: the first assignment's snapshot is taken before
    followers are placed, and every later pass leaves them out too."""
    import ast

    from src.agents.referee.tie_break import settled_counts

    store, version = pipeline
    qas = _raw(store, version)["query_assignments"]
    with_them = settled_counts(qas)
    without = settled_counts(qas, exclude=set(FOLLOWERS))
    assert with_them["dynamodb"] - without["dynamodb"] == 1  # order-tmp
    # Every later pass's call names the followers; the sweep's input holds none.
    allowed = {"src/agents/referee/consolidation_validator.py"}
    missing = []
    for path in sorted((REPO / "src").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "settled_counts"
                and rel not in allowed
                and not any(k.arg == "exclude" for k in node.keywords)
            ):
                missing.append(f"{rel}:{node.lineno}")
    assert missing == []


def test_the_mean_fit_reads_followers_with_the_collectors_scope() -> None:
    import ast
    import inspect

    from src.agents.referee import synthesis_report

    tree = ast.parse(inspect.getsource(synthesis_report))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "routed_fits"
    ]
    assert calls and all(any(k.arg == "source_scope" for k in c.keywords) for c in calls)
