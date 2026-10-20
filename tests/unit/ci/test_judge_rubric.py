"""The #952 rubric: seven criteria (cost folded into grounded, tone split into
hedging, concision and contradictions), risks graded on completeness against
the migration-risk facts, and a structured ``defects[]`` list. No model is
called here."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ci.llm import judge, judge_facts
from tests.unit.ci.test_judge import DB, JOB, _build_job_dir

RUBRIC = judge.DEFAULT_RUBRIC_PATH.read_text()
FIXTURE = Path(__file__).parent / "fixtures" / "judge-run3-wordpress"


def _sections() -> list[tuple[int, str, str]]:
    found = []
    parts = re.split(r"^## (\d+)\. (\w+)\s*$", RUBRIC, flags=re.MULTILINE)
    for i in range(1, len(parts), 3):
        found.append((int(parts[i]), parts[i + 1], parts[i + 2]))
    return found


def _section(name: str) -> str:
    return {n: body for _, n, body in _sections()}[name]


def test_rubric_has_one_numbered_section_per_criterion_in_order() -> None:
    sections = _sections()
    assert [name for _, name, _ in sections] == list(judge.CRITERIA)
    assert [n for n, _, _ in sections] == list(range(1, len(judge.CRITERIA) + 1))
    for _, name, body in sections:
        for anchor in ("**1**", "**3**", "**5**"):
            assert anchor in body, (name, anchor)


def test_saturated_and_mixed_criteria_are_gone() -> None:
    assert "cost" not in judge.CRITERIA  # folded into grounded
    assert "tone" not in judge.CRITERIA  # split in three
    assert "facts.tco" in _section("grounded")


def test_every_facts_field_the_rubric_cites_exists() -> None:
    """A rubric pointer to a facts field the harness never builds would send
    the judge looking for nothing."""
    facts = judge_facts.build_facts(_migration_report())
    cited = set(re.findall(r"facts\.([a-z_]+)", RUBRIC))
    assert {"migration_risk_facts", "routing_margins", "tco", "risks"} <= cited
    assert cited <= set(facts), cited - set(facts)
    # every field the risks section names is a migration_risk_facts key
    named = set(re.findall(r"`([a-z_]+)`", _section("risks"))) - {"migration_risk_facts"}
    assert {"kept_in_sync", "cache_reads_dropped", "unresolved_ties"} <= named
    assert named <= set(facts["migration_risk_facts"]), named - set(facts["migration_risk_facts"])


def test_rubric_defines_the_empty_register_case() -> None:
    risks = _section("risks")
    assert "**An empty register.**" in risks
    assert "scores 2" in risks


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _migration_report(**extra) -> dict:
    report = {
        "database_name": "shop",
        "ranking": [{"target": "dynamodb", "assigned_queries": 3}],
        "migration_waves": [
            {"wave": 1, "engines": ["aurora_mysql"], "table_owners": None},
            {
                "wave": 2,
                "engines": ["dynamodb"],
                "table_owners": [
                    {"table": "shop.orders", "owner": "aurora_mysql", "sync": "CDC"},
                    {"table": "shop.items", "owner": "aurora_mysql", "sync": "CDC"},
                    {"table": "shop.carts", "owner": "dynamodb"},
                ],
            },
        ],
        "query_traces": {
            "close_calls": [
                {"query_id": "q1", "engine": "aurora_mysql", "alternative": "dynamodb", "gap": 0.0},
                {"query_id": "q2", "engine": "aurora_mysql", "alternative": "dynamodb", "gap": 4.0},
                {"query_id": "q3", "engine": "dynamodb", "alternative": "aurora_mysql", "gap": 2.0},
            ],
            "unservable": [],
        },
        "cache_overlay": {"engine": "elasticache", "dropped_query_ids": ["q7", "q8"]},
        "schema_designs": {
            "aurora_mysql": {
                "app_layer_notes": [
                    {
                        "feature": "zero_dates",
                        "source_object": "DATETIME defaults",
                        "recommendation": "keep sql_mode",
                    }
                ],
                "hot_partitions_at_risk": 0,
            },
            "elasticache": {
                "unsupported_patterns": [
                    {"source_query_ids": ["q4", "q5"], "reason": "joins", "workaround": "owner db"}
                ]
            },
            "dynamodb": {"hot_partitions_at_risk": 1},
        },
        "trade_offs": [{"engine": "dynamodb"}, {"engine": "dynamodb"}, {"engine": "aurora_mysql"}],
        "assignment_summary": {"tie_unresolved_count": 1},
    }
    report.update(extra)
    return report


def test_migration_risk_facts_from_report_fields() -> None:
    facts = judge_facts.build_facts(_migration_report())
    risk = facts["migration_risk_facts"]
    assert risk["kept_in_sync"] == [
        {
            "wave": 2,
            "engines": ["dynamodb"],
            "kept_in_sync_tables": 2,
            "by_owner": {"aurora_mysql": 2},
            "sync": ["CDC"],
            "tables": ["items", "orders"],
        }
    ]
    # carts is owned by the wave's own engine: shared in the wave, not kept in sync
    assert risk["shared_in_wave"] == [
        {
            "wave": 2,
            "engines": ["dynamodb"],
            "entries": 1,
            "tables": [{"table": "carts", "owner": "dynamodb"}],
        }
    ]
    assert risk["close_call_query_count"] == 3
    assert risk["cache_reads_dropped"] == 2
    assert risk["unsupported_patterns"] == [
        {"engine": "elasticache", "queries": 2, "reason": "joins", "workaround": "owner db"}
    ]
    assert risk["app_layer_notes"][0]["feature"] == "zero_dates"
    assert risk["hot_partitions_at_risk"] == {"dynamodb": 1}
    assert risk["unservable_queries"] == 0
    assert risk["unresolved_ties"] == 1
    assert risk["design_trade_offs_by_engine"] == {"dynamodb": 2, "aurora_mysql": 1}

    margins = facts["routing_margins"]
    assert margins["close_call_query_count"] == 3
    assert margins["engine_pairs"][0] == {
        "engine": "aurora_mysql",
        "alternative": "dynamodb",
        "pair_query_count": 2,
        "min_gap": 0.0,
        "max_gap": 4.0,
    }


def test_the_facts_layer_wins_over_the_raw_traces() -> None:
    layer = {
        "close_calls": {
            "margin_points": 5.0,
            "close_call_query_count": 20,
            "settled_tie_count": 1,
            "engine_pairs": [
                {"engine": "a", "alternative": "b", "pair_query_count": 19, "query_ids": ["x"]}
            ],
        },
        "waves": [
            {
                "wave": 3,
                "engines": ["dynamodb"],
                "kept_in_sync": [{"table": "shop.posts", "owner": "aurora_mysql", "sync": "CDC"}],
            },
            {"wave": 1, "engines": ["aurora_mysql"], "kept_in_sync": []},
        ],
        "unservable": {"unservable_query_count": 4},
    }
    facts = judge_facts.build_facts(_migration_report(facts=layer))
    assert facts["routing_margins"]["close_call_query_count"] == 20
    assert "query_ids" not in facts["routing_margins"]["engine_pairs"][0]
    assert facts["migration_risk_facts"]["unservable_queries"] == 4
    # the report's own figure wins over a recount of migration_waves[].table_owners
    assert facts["migration_risk_facts"]["kept_in_sync"] == [
        {
            "wave": 3,
            "engines": ["dynamodb"],
            "kept_in_sync_tables": 1,
            "by_owner": {"aurora_mysql": 1},
            "sync": ["CDC"],
            "tables": ["posts"],
        }
    ]


def test_a_two_engine_wave_keeps_the_reports_kept_in_sync_figure() -> None:
    """#581's merged DynamoDB/DocumentDB wave: an entry owned by one wave engine
    and served by the other is shared in the wave, not kept in sync. The judge
    states the report's figure (2 here) and lists the in-wave sharing apart,
    rather than recounting all three entries as kept in sync."""
    owners = [
        {"table": "shop.orders", "owner": "aurora_postgresql", "sync": "CDC"},
        {"table": "shop.items", "owner": "aurora_postgresql", "sync": "CDC"},
        {"table": "shop.carts", "owner": "dynamodb", "sync": "CDC"},
    ]
    wave = {"wave": 3, "engines": ["dynamodb", "documentdb"], "table_owners": owners}
    layer = {
        "waves": [
            {
                "wave": 3,
                "engines": ["dynamodb", "documentdb"],
                "kept_in_sync_tables": 2,
                "kept_in_sync": owners[:2],
                "shared_in_wave": owners[2:],
            }
        ]
    }
    for report in (
        _migration_report(migration_waves=[wave], facts=layer),  # facts layer
        _migration_report(migration_waves=[wave]),  # older report: same split rule
    ):
        risk = judge_facts.build_facts(report)["migration_risk_facts"]
        assert [w["kept_in_sync_tables"] for w in risk["kept_in_sync"]] == [2]
        assert risk["kept_in_sync"][0]["by_owner"] == {"aurora_postgresql": 2}
        assert risk["shared_in_wave"][0]["tables"] == [{"table": "carts", "owner": "dynamodb"}]


def test_settled_ties_are_not_close_calls_in_the_trace_fallback() -> None:
    traces = {
        "close_calls": [
            {"engine": "a", "alternative": "b", "gap": 1.0},
            {"engine": "a", "alternative": "b", "gap": 0.0, "settled_tie": True},
        ]
    }
    facts = judge_facts.build_facts(_migration_report(query_traces=traces))
    assert facts["routing_margins"]["close_call_query_count"] == 1
    assert facts["routing_margins"]["settled_tie_count"] == 1
    assert facts["migration_risk_facts"]["close_call_query_count"] == 1


def test_tables_served_by_several_owning_engines() -> None:
    llm_input = {
        "effective_architecture": {
            "engines": [
                {"engine": "dynamodb", "tables": ["orders", "carts"]},
                {"engine": "aurora_mysql", "tables": ["orders", "items"]},
                {"engine": "elasticache", "tables": ["carts", "items"]},
            ]
        }
    }
    report = _migration_report(
        ranking=[
            {"target": "dynamodb", "assigned_queries": 3},
            {"target": "aurora_mysql", "assigned_queries": 2},
            {"target": "elasticache", "role": "cache_layer"},
        ]
    )
    shared = judge_facts.build_facts(report, llm_input)["migration_risk_facts"][
        "tables_served_by_several_engines"
    ]
    # the cache layer fronts reads and owns no copy, so carts and items are not shared
    assert shared == {
        "count": 1,
        "tables": [{"table": "orders", "engines": ["aurora_mysql", "dynamodb"]}],
    }


def test_an_older_report_without_the_fields_reads_null_not_zero() -> None:
    report = json.loads((FIXTURE / "report.json").read_text())
    facts = judge_facts.build_facts(report)
    risk = facts["migration_risk_facts"]
    assert facts["routing_margins"] is None
    assert risk["kept_in_sync"] is None
    assert risk["cache_reads_dropped"] is None
    assert risk["close_call_query_count"] is None
    # schema designs are present in this report, so those fields are counted
    assert isinstance(risk["unsupported_patterns"], list)
    assert isinstance(risk["design_trade_offs_by_engine"], dict)


def test_an_empty_report_still_builds_facts() -> None:
    facts = judge_facts.build_facts({})
    assert facts["routing_margins"] is None
    assert facts["migration_risk_facts"]["unsupported_patterns"] is None
    assert facts["migration_risk_facts"]["hot_partitions_at_risk"] is None


# ---------------------------------------------------------------------------
# Defects
# ---------------------------------------------------------------------------


def test_defects_are_validated_capped_and_never_fail_a_reply() -> None:
    raw = [
        {"criterion": "hedging", "kind": "hedge", "where": "pdf > p5", "quote": "might   be"},
        {"criterion": "hedging", "kind": "vibes", "where": "x", "quote": "y"},
        {"criterion": "tone", "kind": "hedge", "where": "x", "quote": "y"},
        "not an object",
        {"criterion": "risks", "kind": "missing_risk", "quote": "z" * 1000},
    ]
    out = judge.validate_defects(raw)
    assert out["dropped"] == 2
    assert out["items"][0] == {
        "criterion": "hedging",
        "kind": "hedge",
        "where": "pdf > p5",
        "quote": "might be",
    }
    assert out["items"][1]["kind"] == "other"
    assert out["items"][2]["where"] == ""
    assert len(out["items"][2]["quote"]) == judge.DEFECT_TEXT_CHARS + 3
    assert judge.validate_defects(None) == {"items": [], "dropped": 0}
    assert judge.validate_defects("bad") == {"items": [], "dropped": 1}
    many = [{"criterion": "hedging", "kind": "hedge", "quote": str(i)} for i in range(100)]
    capped = judge.validate_defects(many)
    assert len(capped["items"]) == judge.MAX_DEFECTS
    assert capped["dropped"] == 100 - judge.MAX_DEFECTS


def test_defects_merge_across_samples_with_a_count() -> None:
    a = {"criterion": "hedging", "kind": "hedge", "where": "w", "quote": "Not the only one."}
    b = {"criterion": "hedging", "kind": "hedge", "where": "w2", "quote": "not the ONLY one"}
    c = {"criterion": "risks", "kind": "missing_risk", "where": "facts.x", "quote": ""}
    merged = judge.merge_defects([[a, a], [b, c], [c]])
    # same count: rubric criterion order (risks is criterion 3, hedging 5)
    assert [(d["criterion"], d["samples"]) for d in merged] == [("risks", 2), ("hedging", 2)]
    assert merged[1]["quote"] == "Not the only one."  # the first spelling seen is kept
    assert judge.merge_defects([[a], [a], [a], [c]])[0]["samples"] == 3


def test_reply_without_defects_still_grades() -> None:
    reply = json.dumps({"scores": dict.fromkeys(judge.CRITERIA, 4), "notes": {}})
    scores, _notes, defects, _trailing = judge.parse_judge_reply_full(reply)
    assert scores["hedging"] == 4
    assert defects == {"items": [], "dropped": 0}


def test_prompt_asks_for_every_criterion_and_the_defects_list(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    deliverables = judge.locate_deliverables(tmp_path, DB, JOB)
    prompt = judge.build_prompt(RUBRIC.split("---", 2)[2], DB, JOB, deliverables, nonce="n0nce")
    assert judge.RESPONSE_SHAPE in prompt
    assert judge.DEFECTS_INSTRUCTION in prompt
    for kind in judge.DEFECT_KINDS:
        assert kind in prompt
    assert f"the {len(judge.CRITERIA)} criteria" in prompt
    shape = json.loads(judge.RESPONSE_SHAPE.replace(": n", ": 0"))
    assert list(shape["scores"]) == list(judge.CRITERIA)
    assert list(shape["notes"]) == list(judge.CRITERIA)


def test_risk_and_close_call_sections_are_cut_last() -> None:
    """Two sections of the same size over budget: the unprotected one is cut
    to nothing before the protected one loses a line."""
    filler = "\n".join(f"filler line {i} " + "x" * 40 for i in range(40))
    keep = "\n".join(f"risk line {i} " + "r" * 40 for i in range(40))
    for heading in (
        "## Risk posture",
        "## Resolved by the assignment (5)",
        "## Mitigation strategies",
        "## Close calls (20 queries)",
        "## Migration roadmap (3 waves)",
    ):
        text = f"## Appendix\n{filler}\n{heading}\n{keep}\n"
        out, info = judge.truncate_sections(text, len(text) * 2 // 3)
        assert [c["section"] for c in info["sections_truncated"]] == ["Appendix"], heading
        assert keep in out, heading


# --- rubric_version 3 clarifications (first e2e-llm run on #1075) ----------


def test_the_migration_map_target_engine_is_not_ownership() -> None:
    """The judge read the Migration map's "Target engine" as the owner and
    called it a contradiction of "Sources of truth and replicas"."""
    rules = RUBRIC[: RUBRIC.index("## 1. grounded")]
    assert "**Ownership.**" in rules
    ownership = rules[rules.index("**Ownership.**") :].split("\n- ", 1)[0]
    assert '"Target engine"' in ownership
    assert "recommended engine" in ownership
    assert "Sources of truth" in ownership
    assert "kept in sync from their" in ownership
    assert "Sources of truth" in _section("contradictions")
    assert "the ownership\nrule above" in _section("contradictions")


def test_a_sentence_with_a_stated_bound_is_not_a_hedge() -> None:
    hedging = _section("hedging")
    not_a_hedge = hedging[hedging.index("Not a hedge:") :]
    assert "stated bound" in not_a_hedge
    for example in ("until it\nreplicates", "until the TTL expires"):
        assert example in not_a_hedge, example
    # an unbounded softener stays a hedge
    assert '"a moment"' in hedging[: hedging.index("Not a hedge:")]


def test_the_clarified_rubric_is_version_3() -> None:
    meta = judge.rubric_identity(judge.DEFAULT_RUBRIC_PATH)
    assert meta["rubric_version"] == 3


def test_wave_figures_come_from_the_facts_layer() -> None:
    """The judge flagged "18 served, 15 move" as unsupported while
    report.json facts.waves[2] states served_tables 18, moving_tables 15."""
    layer = {
        "waves": [
            {
                "wave": 3,
                "engines": ["dynamodb"],
                "migrating": True,
                "served_tables": 18,
                "moving_tables": 15,
                "kept_in_sync_tables": 3,
                "moving_by_engine": {"dynamodb": 15},
                "served_by_engine": {"dynamodb": 18},
                "kept_in_sync": [],
            }
        ]
    }
    figures = judge_facts.build_facts(_migration_report(facts=layer))["wave_figures"]
    assert figures == [
        {
            "wave": 3,
            "engines": ["dynamodb"],
            "migrating": True,
            "served_tables": 18,
            "moving_tables": 15,
            "kept_in_sync_tables": 3,
            "moving_by_engine": {"dynamodb": 15},
            "served_by_engine": {"dynamodb": 18},
        }
    ]
    assert judge_facts.build_facts(_migration_report())["wave_figures"] is None


def test_migration_risk_facts_read_a_count_or_a_list() -> None:
    """Synthesis writes ``hot_partitions_at_risk`` as a count; a list still counts its items."""
    report = _migration_report()
    report["schema_designs"]["dynamodb"]["hot_partitions_at_risk"] = 2
    report["schema_designs"]["aurora_mysql"]["hot_partitions_at_risk"] = 0
    risk = judge_facts.build_facts(report)["migration_risk_facts"]
    assert risk["hot_partitions_at_risk"] == {"dynamodb": 2}
    report["schema_designs"]["dynamodb"]["hot_partitions_at_risk"] = [
        {"table": "a"},
        {"table": "b"},
    ]
    report["schema_designs"]["aurora_mysql"]["hot_partitions_at_risk"] = -1
    risk = judge_facts.build_facts(report)["migration_risk_facts"]
    assert risk["hot_partitions_at_risk"] == {"dynamodb": 2}
