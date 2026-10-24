"""The detailed engineering report: the full routing detail, read from saved records (#1364).

``src.shared.report_facts.routing_detail_facts`` builds it from the job's assignment
lineage (its Moves), the access-pattern record and each engine's analysis, without
scoring anything again; ``src.shared.routing_detail`` reads those from the store;
``src.report.detailed_report`` only formats it and escapes every collected string. A
job saved before one of those records existed renders with the absence stated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report import deliverables, detailed_report
from src.shared.report_facts import routing_detail_facts
from src.shared.routing_detail import load_routing_detail
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures"

DB, JOB = "shop", "job-1"
ROOT = f"{DB}/{JOB}"


def _rung(engine: str, score: float | None, gate: str | None = None) -> dict[str, Any]:
    return {"engine": engine, "score": score, "gate": gate}


def _move(
    seq: int,
    qid: str,
    to: str,
    *,
    frm: str | None = None,
    pass_: str = "score",
    reason: str = "highest_score",
    ladder: list[dict[str, Any]] | None = None,
    group_scores: dict[str, float] | None = None,
    group_key: str | None = None,
    score_after: float | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "query_id": qid,
        "pass": pass_,
        "from_engine": frm,
        "to_engine": to,
        "score_before": None,
        "score_after": score_after,
        "runner_up": None,
        "reason_code": reason,
        "evidence": {
            "candidates": ladder or [],
            "group_scores": group_scores or {},
        },
        "group_key": group_key,
        "tied_engines": [],
        "actor": "rule",
    }


LADDER_W1 = [_rung("aurora_postgresql", 60.0), _rung("dynamodb", 20.0)]
LADDER_W2 = [_rung("aurora_postgresql", 70.0), _rung("dynamodb", 90.0)]
LADDER_R = [_rung("aurora_postgresql", 74.0), _rung("dynamodb", None, "capability")]
GROUP = {"aurora_postgresql": 66.04, "dynamodb": 55.0}


def _assignments() -> tuple[dict[str, Any], dict[str, Any]]:
    """v1 places the write group of ``shop.orders`` (w1, w2) on Aurora PostgreSQL as one
    unit and the read r1 on DynamoDB; v2 consolidates r1 onto Aurora PostgreSQL."""
    v1 = {
        "version": 1,
        "previous_version": None,
        "query_assignments": [],
        "write_groups": {"shop.orders": ["w1", "w2"], "shop.items": ["w3"]},
        "co_dependency_groups": [["w1", "r1"]],
        "moves": [
            _move(
                1,
                "w1",
                "aurora_postgresql",
                pass_="co_dependency",
                reason="group.write",
                ladder=LADDER_W1,
                group_scores=GROUP,
                group_key="g-orders",
            ),
            _move(
                2,
                "w2",
                "aurora_postgresql",
                pass_="co_dependency",
                reason="group.write",
                ladder=LADDER_W2,
                group_scores=GROUP,
                group_key="g-orders",
            ),
            _move(3, "w3", "aurora_postgresql", ladder=LADDER_W2),
            _move(
                4,
                "r1",
                "dynamodb",
                ladder=[_rung("aurora_postgresql", 74.0), _rung("dynamodb", 76.0)],
            ),
        ],
    }
    qas = [
        {
            "query_id": "w1",
            "assigned_engine": "aurora_postgresql",
            "source_tables": ["shop.orders"],
        },
        {
            "query_id": "w2",
            "assigned_engine": "aurora_postgresql",
            "source_tables": ["shop.orders"],
        },
        {"query_id": "w3", "assigned_engine": "aurora_postgresql", "source_tables": ["shop.items"]},
        {
            "query_id": "r1",
            "assigned_engine": "aurora_postgresql",
            # The collector's placeholder for a table it could not name: never listed.
            "source_tables": ["shop.orders", "shop.items", "unknown"],
        },
    ]
    v2 = {
        **v1,
        "version": 2,
        "previous_version": 1,
        "query_assignments": qas,
        "moves": [
            _move(
                1,
                "r1",
                "aurora_postgresql",
                frm="dynamodb",
                pass_="settle",
                reason="consolidate.no_unique_value",
                ladder=LADDER_R,
            )
        ],
    }
    v1["query_assignments"] = [
        {**qa, "assigned_engine": "dynamodb"} if qa["query_id"] == "r1" else qa for qa in qas
    ]
    return v1, v2


ACCESS_RECORD = {
    "schema_version": 3,
    "queries": [
        {
            "query_id": "r1",
            "class": "key_scoped",
            "reason": "rows of orders addressed by a non-unique key value",
            "tables": ["shop.orders", "shop.items"],
            "class_scores": {"aurora_postgresql": 70, "dynamodb": 75},
            "parity_anchors": {},
            "anti_pattern_findings": [
                {
                    "engine": "dynamodb",
                    "pattern": "complex-joins",
                    "penalty": 20,
                    "kind": "intrinsic",
                },
                {
                    "engine": "dynamodb",
                    "pattern": "complex-aggregation",
                    "penalty": 30,
                    "kind": "intrinsic",
                },
                {"engine": "dynamodb", "pattern": "scan-filter", "penalty": 0, "kind": "fixable"},
            ],
            "redesign": {
                "assumed_keys": [{"column": "orders.customer_id"}],
                "class": "key_lookup",
                "reason": "one row by its key",
                "class_scores": {"aurora_postgresql": 80, "dynamodb": 100},
            },
            "candidate_keys": [
                {
                    "column": "orders.customer_id",
                    "table_id": "shop.orders",
                    "via": "equality",
                    "outcome": "accepted",
                    "reasons": [],
                },
                {
                    "column": "orders.visible",
                    "table_id": "shop.orders",
                    "via": "equality",
                    "outcome": "refused",
                    "reasons": ["not_key_eligible"],
                },
            ],
        }
    ],
}

ANALYSES = {
    "aurora_postgresql": {
        "table_recommendations": [
            {
                "table_id": "shop.orders",
                "score_breakdown": {"complexity_score": 100, "performance_score": 60},
            },
            {
                "table_id": "shop.items",
                "score_breakdown": {"complexity_score": 90, "performance_score": 50},
            },
        ]
    },
    "dynamodb": {
        "table_recommendations": [
            {
                "table_id": "shop.orders",
                "score_breakdown": {"complexity_score": 70, "performance_score": 40},
            }
        ]
    },
}

CARRIED = [
    {
        "query_id": "w1",
        "table_id": "shop.orders",
        "engine": "aurora_postgresql",
        "score": 20.0,
        "floor": 50.0,
        "group_size": 2,
        "statement": "DELETE",
        "intent": "retention-delete",
        "intent_name": "Retention delete",
        "intent_summary": "removes rows past their retention period",
        "approach": "Use a partition | drop <b>per</b> month",
    }
]


OWNER_LINES = [
    {
        "table": "shop.orders",
        "owner": "aurora_postgresql",
        "owner_basis": "writes",
        "also_read_by": [],
        "cached_by": None,
    },
    {
        "table": "shop.items",
        "owner": "aurora_postgresql",
        "owner_basis": "writes",
        "also_read_by": [],
        "cached_by": None,
    },
]


def _report(**extra: Any) -> dict[str, Any]:
    return {
        "job_id": JOB,
        "database_name": DB,
        "assignment_summary": {"version": 2},
        "facts": {
            "engines": [],
            "engine_scope": ["aurora_postgresql", "dynamodb"],
            "table_owner_lines": {"table_lines": OWNER_LINES},
        },
        "query_traces": {
            "close_calls": [
                {"query_id": "r1", "settled_tie": False},
                {"query_id": "w2", "settled_tie": True},
            ]
        },
        "carried_writes": CARRIED,
        **extra,
    }


def _store(tmp_path: Path, *, with_records: bool = True) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    v1, v2 = _assignments()
    store.write_json(f"{ROOT}/assignment/v1/assignment.json", v1)
    store.write_json(f"{ROOT}/assignment/v2/assignment.json", v2)
    if with_records:
        store.write_json(f"{ROOT}/assignment/v2/access_patterns.json", ACCESS_RECORD)
        for engine, analysis in ANALYSES.items():
            store.write_json(f"{ROOT}/analysis-{engine}/analysis.json", analysis)
    store.write_json(
        f"{ROOT}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "r1",
                        "query_text": "SELECT * FROM orders o JOIN items i ON `x` | y",
                    }
                ]
            }
        },
    )
    return store


REPORT_KEY = f"{ROOT}/synthesis/v2/report.json"


@pytest.fixture
def detail(tmp_path: Path) -> dict[str, Any]:
    store = _store(tmp_path)
    return load_routing_detail(
        store, REPORT_KEY, _report(), store.read_json(f"{ROOT}/collector/output.json")
    )


def _query(detail: dict[str, Any], qid: str) -> dict[str, Any]:
    return next(q for q in detail["queries"] if q["query_id"] == qid)


def test_every_query_has_its_move_history_from_the_ledger(detail):
    r1 = _query(detail, "r1")
    assert [(s["version"], s["seq"], s["from_engine"], s["to_engine"]) for s in r1["steps"]] == [
        (1, 4, None, "dynamodb"),
        (2, 1, "dynamodb", "aurora_postgresql"),
    ]
    assert all(s["explanation"] for s in r1["steps"])
    assert r1["engine"] == "aurora_postgresql" and r1["final_score"] == 74.0
    assert detail["moved_query_count"] == 1
    assert detail["has_ledger"] and detail["effective_version"] == 2


def test_the_scores_per_engine_are_the_latest_ladder_never_rescored(detail):
    """r1's latest ladder (the v2 consolidation) gates DynamoDB: the earlier 76 is gone."""
    cells = {c["engine"]: c for c in _query(detail, "r1")["dimensions"]}
    assert cells["aurora_postgresql"]["routed"]["score"] == 74.0
    assert cells["dynamodb"]["routed"]["score"] is None
    assert cells["dynamodb"]["routed"]["gate"] == "capability"


def test_the_dimensions_come_from_the_access_record_and_the_analyses(detail):
    rows = {d["engine"]: d for d in _query(detail, "r1")["dimensions"]}
    ddb = rows["dynamodb"]
    assert (ddb["pattern_score"], ddb["redesigned_pattern_score"]) == (75.0, 100.0)
    # The biggest intrinsic finding is the penalty routing applied (ADR-031 Decision 8),
    # never the sum (50); the fixable one is listed apart and costs nothing.
    assert ddb["intrinsic_penalty"] == 30.0
    assert ddb["intrinsic_findings"] == ["complex-joins", "complex-aggregation"]
    assert ddb["fixable_findings"] == ["scan-filter"]
    # DynamoDB's analysis rated orders only; Aurora PostgreSQL's both tables.
    assert [t["table"] for t in ddb["tables"]] == ["shop.orders"]
    assert [(t["complexity"], t["performance"]) for t in rows["aurora_postgresql"]["tables"]] == [
        (100.0, 60.0),
        (90.0, 50.0),
    ]
    r1 = _query(detail, "r1")
    assert (r1["access_class"], r1["redesign_class"]) == ("key_scoped", "key_lookup")
    assert r1["assumed_keys"] == ["orders.customer_id"]


def test_the_refused_keys_carry_their_reason(detail):
    keys = _query(detail, "r1")["key_candidates"]
    assert [(k["column"], k["outcome"]) for k in keys] == [
        ("orders.customer_id", "accepted"),
        ("orders.visible", "refused"),
    ]
    assert keys[1]["explanation"]


def test_each_table_lists_its_owner_and_every_query_touching_it(detail):
    tables = {t["table"]: t for t in detail["tables"]}
    assert set(tables) == {"shop.orders", "shop.items"}
    orders = tables["shop.orders"]
    assert orders["owner"] == "aurora_postgresql" and orders["owner_basis"] == "writes"
    assert orders["owner_reason"]
    assert [q["query_id"] for q in orders["queries"]] == ["r1", "w1", "w2"]
    assert orders["query_count"] == 3
    assert detail["table_count"] == 2 and detail["has_owner_lines"]


def test_no_owner_line_is_computed_when_the_report_maps_no_table(tmp_path):
    """With no schema design, report.json has no owner lines: the detailed report states
    the absence rather than computing owners of its own."""
    store = _store(tmp_path)
    report = _report(facts={"engines": []})
    detail = load_routing_detail(store, REPORT_KEY, report)
    assert not detail["has_owner_lines"]
    assert all(t["owner"] is None and t["owner_reason"] is None for t in detail["tables"])
    md = detailed_report.render_detailed_engineering_report_md(report, detail)
    assert "## Tables the queries touch (2)" in md
    assert "states no table owner" in md and "Owner:" not in md
    assert "`unknown`" not in md


def test_close_calls_and_settled_ties_are_counted_apart_as_synthesis_selected_them(detail):
    assert (_query(detail, "r1")["close_call"], _query(detail, "r1")["settled_tie"]) == (
        True,
        False,
    )
    assert (_query(detail, "w2")["close_call"], _query(detail, "w2")["settled_tie"]) == (
        False,
        True,
    )
    assert (detail["close_call_count"], detail["settled_tie_count"]) == (1, 1)
    md = detailed_report.render_detailed_engineering_report_md(_report(), detail)
    assert "1 is a close call, 1 is an exact tie a routing rule settled" in md


def test_a_co_dependency_member_fits_poorly_only_as_a_carried_write(detail, tmp_path):
    (group,) = detail["co_dependency_groups"]
    members = {m["query_id"]: m for m in group["members"]}
    assert members["w1"]["poor_fit"] and not members["r1"]["poor_fit"]
    assert group["poor_fit_count"] == 1
    # Without the carried-write record, a co-dependency group marks nothing.
    report = _report()
    del report["carried_writes"]
    bare = load_routing_detail(_store(tmp_path / "bare"), REPORT_KEY, report)
    (group,) = bare["co_dependency_groups"]
    assert not any(m["poor_fit"] for m in group["members"]) and not group["guidance_recorded"]


def test_the_placeholder_tables_cover_the_pipelines_own():
    from src.agents.referee.synthesis_report import _PLACEHOLDER_TABLES
    from src.agents.referee.table_resolution import PSEUDO_TABLES
    from src.shared.report_facts import PLACEHOLDER_TABLE_NAMES

    assert PSEUDO_TABLES | _PLACEHOLDER_TABLES <= PLACEHOLDER_TABLE_NAMES


def test_a_write_group_has_its_group_score_margin_and_poor_fits(detail):
    (group,) = detail["write_groups"]
    assert group["name"] == "shop.orders" and group["member_count"] == 2
    assert group["group_scores"] == {"aurora_postgresql": 66.0, "dynamodb": 55.0}
    assert (group["winner"], group["runner_up"], group["margin"]) == (
        "aurora_postgresql",
        "dynamodb",
        11.0,
    )
    assert (group["placed_version"], group["placed_seq"], group["placement_member_count"]) == (
        1,
        2,
        2,
    )
    members = {m["query_id"]: m for m in group["members"]}
    assert members["w1"]["poor_fit"] and members["w1"]["intent_name"] == "Retention delete"
    assert not members["w2"]["poor_fit"]
    assert group["poor_fit_count"] == 1 and group["guidance_recorded"]
    # shop.items has one write statement: no group to place.
    assert detail["single_statement_write_group_count"] == 1


def test_without_carried_writes_a_poor_fit_is_its_own_score_below_the_floor(tmp_path):
    """A report synthesized before #1364 has no carried_writes: a member is marked from
    its own routed score on the ledger, with no guidance."""
    store = _store(tmp_path)
    report = _report()
    del report["carried_writes"]
    detail = load_routing_detail(store, REPORT_KEY, report)
    (group,) = detail["write_groups"]
    members = {m["query_id"]: m for m in group["members"]}
    assert members["w1"]["own_score"] == 60.0 and members["w2"]["own_score"] == 70.0
    assert detail["poor_fit_floor"] == 50.0
    assert not any(m["poor_fit"] for m in group["members"])
    assert not group["guidance_recorded"] and not detail["has_carried_writes"]


def test_the_renderer_formats_and_escapes(detail):
    md = detailed_report.render_detailed_engineering_report_md(_report(), detail)
    assert "# Database Modernization: Detailed Engineering Report" in md
    assert "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)." in md
    assert "### `shop.orders`" in md and "### Query `r1`" in md
    assert "### Write group `shop.orders`" in md
    assert "- Margin: Aurora PostgreSQL by 11 over DynamoDB" in md
    # The collected SQL loses its backticks inside a code span; the guidance's pipe and
    # markup are escaped in their table cell.
    assert "`SELECT * FROM orders o JOIN items i ON x | y`" in md
    assert "Use a partition \\| drop &lt;b&gt;per&lt;/b&gt; month" in md
    assert "<b>" not in md
    assert "## Records not available" not in md


def test_a_job_saved_without_routing_records_states_each_absence(tmp_path):
    store = _store(tmp_path, with_records=False)
    for version in (1, 2):
        a = store.read_json(f"{ROOT}/assignment/v{version}/assignment.json")
        a.pop("moves")
        store.write_json(f"{ROOT}/assignment/v{version}/assignment.json", a)
    report = _report()
    del report["carried_writes"]
    detail = load_routing_detail(store, REPORT_KEY, report)
    md = detailed_report.render_detailed_engineering_report_md(report, detail)
    assert "## Records not available" in md
    for absence in ("No move ledger", "No access-pattern record", "No engine analysis"):
        assert absence in md
    assert "### Query `r1`" in md  # every query is still listed, with what is known


def test_an_old_saved_report_with_no_assignment_renders_the_absence(tmp_path):
    """A report written by release 1.26 (tests/unit/report/fixtures), in a store that
    holds nothing else: the deliverable renders and says there is no routing record."""
    report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = f"{report['database_name']}/{report['job_id']}/synthesis/v2/report.json"
    store.write_json(key, report)
    rendered = deliverables.render_deliverables(
        store, report["job_id"], report["database_name"], key, assignment_version=2
    )
    assert not [e for e in rendered.errors if e.startswith("engineering-report-detailed")]
    item = next(d for d in rendered.items if d.name == "engineering-report-detailed")
    md = item.content.decode("utf-8")
    assert "## Records not available" in md
    assert "No assignment is saved next to this report" in md or "No move ledger" in md


def test_a_pre_ledger_assignment_renders(tmp_path):
    """The judged wordpress run's assignment (saved before the move ledger, with no
    version lineage fields) still renders, with the ledger's absence stated."""
    fixture = REPO / "tests" / "unit" / "ci" / "fixtures" / "judge-run3-wordpress"
    report = json.loads((fixture / "report.json").read_text())
    assignment = json.loads((fixture / "assignment.json").read_text())
    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job = report["database_name"], report["job_id"]
    version = int(assignment.get("version") or 1)
    store.write_json(f"{db}/{job}/assignment/v{version}/assignment.json", assignment)
    key = f"{db}/{job}/synthesis/v{version}/report.json"
    store.write_json(key, report)
    detail = load_routing_detail(store, key, report)
    md = detailed_report.render_detailed_engineering_report_md(report, detail)
    assert detail["query_count"] == len(assignment["query_assignments"])
    assert not detail["has_ledger"]
    assert "No move ledger" in md


def test_the_folder_lists_the_detailed_report_apart_and_does_not_publish_it(tmp_path):
    store = _store(tmp_path)
    store.write_json(REPORT_KEY, _report())
    rendered = deliverables.render_deliverables(store, JOB, DB, REPORT_KEY, assignment_version=2)
    item = next(d for d in rendered.items if d.name == "engineering-report-detailed")
    assert item.file_type == "MARKDOWN" and not item.publish
    entry = next(e for e, _ in rendered.folder_files() if e.name == item.name)
    assert entry.filename == "engineering-report-detailed.md" and entry.audit
    index = deliverables.render_folder_index(DB, JOB, 2, [e for e, _ in rendered.folder_files()])
    main, audit = index.split("## For auditing the routing")
    assert "engineering-report-detailed.md" not in main
    assert "`engineering-report-detailed.md`" in audit


def test_the_facts_need_no_record_at_all():
    """No lineage, no record, no analysis: an empty detail, every absence flagged."""
    detail = routing_detail_facts({}, [], {}, None, {})
    assert detail["queries"] == [] and detail["effective_version"] is None
    assert not (detail["has_ledger"] or detail["has_access_record"] or detail["has_analysis"])
    md = detailed_report.render_detailed_engineering_report_md({}, detail)
    assert "No assignment is saved next to this report" in md
