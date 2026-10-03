"""What the rubric judge is shown (ci/llm/judge.py + ci/llm/judge_facts.py):
the structured facts block, full deliverable text, the overall prompt
ceiling with per-section truncation markers, and the size record in
judge.json. No model is called here."""

from __future__ import annotations

import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest

from ci.llm import judge, judge_facts

FIXTURE = Path(__file__).parent / "fixtures" / "judge-run3-wordpress"
RUN3_DB, RUN3_JOB = "wordpress", "cf163e54"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _report(**extra) -> dict:
    report = {
        "database_name": "shop",
        "ranking": [
            {"target": "dynamodb", "assigned_queries": 3, "workload_percent": 75.0},
            {"target": "aurora_mysql", "assigned_queries": 1, "workload_percent": 25.0},
        ],
        "table_mappings": [
            {"source_table": "shop.orders", "recommended_database": "dynamodb"},
            {"source_table": "shop.items", "recommended_database": "aurora_mysql"},
        ],
        "query_groups": [
            {
                "group_name": "Order reads",
                "engines": ["dynamodb"],
                "access_patterns": [{"pattern_id": "DDB-AP-1", "engine": "dynamodb"}],
                "source_queries": [
                    {
                        "query_id": "q1",
                        "tables_accessed": ["shop.orders", "shop.items"],
                        "linked_patterns": ["DDB-AP-1"],
                    }
                ],
            }
        ],
        "tco_analysis": {
            "projected_monthly_cost": 300.0,
            "cost_breakdown": [
                {"database": "dynamodb", "monthly_cost_usd": 100.0},
                {"database": "aurora_mysql", "monthly_cost_usd": 200.0},
            ],
        },
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "severity": "HIGH",
                    "description": "[dynamodb] " + "x" * 400,
                    "mitigation": "Pre-compute.",
                    "affected_tables": ["shop.items"],
                    "query_ids": ["q1", "q9"],
                }
            ],
        },
        "reality_check": {
            "before_distribution": {"dynamodb": 2, "aurora_mysql": 1, "opensearch": 1},
            "after_distribution": {"dynamodb": 3, "aurora_mysql": 1},
            "consolidations": [
                {
                    "from_engine": "opensearch",
                    "to_engine": "dynamodb",
                    "action": "full",
                    "query_count": 1,
                    "reason": "no unique value",
                }
            ],
        },
    }
    report.update(extra)
    return report


def test_tables_served_prefers_effective_architecture() -> None:
    llm_input = {
        "effective_architecture": {
            "engines": [{"engine": "dynamodb", "tables": ["orders", "items"]}],
            "eliminated_engines": [{"engine": "opensearch", "absorbed_by": "dynamodb"}],
        }
    }
    facts = judge_facts.build_facts(_report(), llm_input)
    dynamodb = facts["engines"][0]
    assert dynamodb["tables_served"] == ["items", "orders"]
    # primary_tables is table_mappings' one-engine-per-table view, kept separate
    assert dynamodb["primary_tables"] == ["orders"]
    assert "effective_architecture" in facts["source"]["tables_served"]


def test_tables_served_falls_back_to_assignment_then_query_groups() -> None:
    assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["shop.orders"]},
            {"query_id": "q9", "assigned_engine": "aurora_mysql", "source_tables": ["shop.items"]},
            {
                "query_id": "q5",
                "assigned_engine": "aurora_mysql",
                "source_tables": ["shop.orders"],
                "in_scope": False,
            },
        ]
    }
    from_assignment = judge_facts.build_facts(_report(), None, assignment)
    served = {e["engine"]: e["tables_served"] for e in from_assignment["engines"]}
    assert served == {"dynamodb": ["orders"], "aurora_mysql": ["items"]}
    # risk RISK-001's queries: q1 on dynamodb, q9 on aurora_mysql
    assert from_assignment["risks"][0]["queries_assigned_to"] == {"dynamodb": 1, "aurora_mysql": 1}

    from_groups = judge_facts.build_facts(_report())
    served = {e["engine"]: e["tables_served"] for e in from_groups["engines"]}
    assert served["dynamodb"] == ["items", "orders"]
    assert "partial" in from_groups["source"]["tables_served"]
    assert from_groups["risks"][0]["queries_assigned_to"] == {
        "dynamodb": 1,
        "not_in_assignment_data": 1,
    }


def test_facts_core_fields() -> None:
    facts = judge_facts.build_facts(_report())
    assert facts["totals"]["overall_risk"] == "MEDIUM"
    assert facts["totals"]["projected_monthly_cost_usd"] == 300.0
    assert [e["monthly_cost_usd"] for e in facts["engines"]] == [100.0, 200.0]
    assert facts["eliminated_engines"] == [{"engine": "opensearch", "absorbed_by": "dynamodb"}]
    risk = facts["risks"][0]
    assert risk["engine"] == "dynamodb"
    assert risk["affected_tables"] == ["items"]
    assert "[truncated: 300 of" in risk["description"]
    assert facts["migration_waves"] == judge_facts.MIGRATION_WAVES_ABSENT
    assert "Migration Sequencing" in facts["migration_waves"]


def test_migration_waves_used_when_report_has_them() -> None:
    waves = [{"wave": 1, "engines": ["elasticache"]}]
    assert judge_facts.build_facts(_report(migration_waves=waves))["migration_waves"] == waves


# ---------------------------------------------------------------------------
# Deliverable text
# ---------------------------------------------------------------------------


def test_html_headings_are_marked() -> None:
    text = judge.html_to_text("<h1>Report</h1><h2 class=x>Risk posture</h2><h3>3</h3><p>p</p>")
    assert text.splitlines() == ["# Report", "## Risk posture", "3", "p"]


def test_strip_mermaid_removes_only_mermaid_fences() -> None:
    md = "# A\n```mermaid\ngraph TD\n  a-->b\n```\ntext\n```sql\nSELECT 1\n```\n"
    out = judge.strip_mermaid(md)
    assert "graph TD" not in out
    assert "[mermaid diagram removed]" in out
    assert "SELECT 1" in out and "text" in out


def test_format_pdf_pages_marks_pages_and_drops_footer() -> None:
    pages = [
        (
            "Risk Profile",
            "©\n2026\n,\nAmazon\nWeb\nServices,\nInc.\nor\nits\naffiliates.\nAll\nrights\n"
            "reserved.\nAmazon\nConfidential\nand\nTrademark.\nRisk\nProfile\n11 risks. Two HIGH.",
        ),
        (None, "plain"),
    ]
    out = judge.format_pdf_pages(pages).splitlines()
    assert out == ["[page 1: Risk Profile]", "11 risks.", "Two HIGH.", "[page 2]", "plain"]


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncate_sections_keeps_headings_and_marks_cuts() -> None:
    text = "# Doc\n## Small\none\n## Big\n" + "\n".join(f"row {i}" for i in range(100))
    same, info = judge.truncate_sections(text, len(text))
    assert same == text and info["truncated"] is False

    out, info = judge.truncate_sections(text, 200)
    assert len(out) <= 200
    assert "## Small" in out and "## Big" in out and "one" in out
    assert '[section "Big":' in out and "of 100 items shown]" in out
    assert info["truncated"] is True
    assert [c["section"] for c in info["sections_truncated"]] == ["Big"]


def test_truncate_json_never_cuts_engines_or_cost_lines() -> None:
    facts = judge_facts.build_facts(_report())
    facts["engines"][0]["tables_served"] = [f"t{i}" for i in range(200)]
    out, info = judge.truncate_json(facts, len(judge.dump_facts(facts)) // 2)
    assert info["truncated"] is True
    assert "[list truncated:" in out
    assert {c["section"] for c in info["sections_truncated"]} >= {"engines[].tables_served"}
    assert '"aurora_mysql"' in out  # second engine kept
    assert '"monthly_cost_usd": 200.0' in out


def test_allocate_budgets_is_max_min_fair() -> None:
    assert judge.allocate_budgets({"a": 10, "b": 20}, 100) == {"a": 10, "b": 20}
    assert judge.allocate_budgets({"a": 10, "b": 100, "c": 100}, 110) == {
        "a": 10,
        "b": 50,
        "c": 50,
    }


# ---------------------------------------------------------------------------
# Prompt + judge.json
# ---------------------------------------------------------------------------


def _stage_run3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | None]:
    """Lay the run-3 evidence out as an artifact root. The PDF text comes from
    pdf-pages.json (the real deck's extracted pages): the 1.2 MB PDF itself
    isn't checked in."""
    synthesis = tmp_path / RUN3_DB / RUN3_JOB / "synthesis" / "v2"
    synthesis.mkdir(parents=True)
    for name in ("report.json", "llm_input.json"):
        shutil.copy(FIXTURE / name, synthesis / name)
    for path in FIXTURE.glob("wordpress_*"):
        shutil.copy(path, synthesis / path.name)
    (synthesis / "summary-executive-report.pdf").write_bytes(b"%PDF-1.4 stub")
    pages = json.loads((FIXTURE / "pdf-pages.json").read_text())
    monkeypatch.setitem(sys.modules, "pypdf", types.ModuleType("pypdf"))
    monkeypatch.setattr(
        judge, "extract_pdf_pages", lambda path, pypdf: [(p["title"], p["text"]) for p in pages]
    )
    return judge.locate_deliverables(tmp_path, RUN3_DB, RUN3_JOB)


def test_run3_prompt_has_every_criterion_input_and_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliverables = _stage_run3(tmp_path, monkeypatch)
    assert deliverables["llm_input"] is not None
    _, _, rubric_body = judge.load_rubric(judge.DEFAULT_RUBRIC_PATH)
    prompt, stats = judge.build_prompt_with_stats(rubric_body, RUN3_DB, RUN3_JOB, deliverables)

    assert len(prompt) <= judge.PROMPT_CHAR_CEILING
    assert stats["prompt_chars"] == len(prompt)
    # grounded: architecture type and totals
    assert '"architecture_type": "HYBRID_WITH_CACHE"' in prompt
    # cost: every per-engine figure and the total
    for cost in ("98.41", "165.55", "318.8", "582.76"):
        assert cost in prompt
    # risks: the engineering report's register and the deck's Risk Profile slide
    assert "## Risk register (11)" in prompt
    assert "[page 6: Risk Profile]" in prompt
    # roadmap: the deck's Migration Sequencing slide, waves marked absent in facts
    assert "[page 7: Migration Sequencing]" in prompt
    assert "VALIDATION GATE" in prompt
    assert judge_facts.MIGRATION_WAVES_ABSENT in prompt
    # each engine's table scope: DynamoDB serves wp_postmeta and order items
    # even though table_mappings puts wp_postmeta on aurora_mysql
    block = re.search(r'<deliverable id="facts-[0-9a-f]+">\n(.*?)\n</deliverable>', prompt, re.S)
    assert block
    facts = json.loads(block.group(1))
    for engine in facts["engines"]:
        assert engine["tables_served"], engine["engine"]
    dynamodb = next(e for e in facts["engines"] if e["engine"] == "dynamodb")
    assert {"wp_postmeta", "wp_woocommerce_order_items"} <= set(dynamodb["tables_served"])
    assert '"absorbed_by": "aurora_mysql"' in prompt
    # mermaid removed, decision report and deck in full
    assert "```mermaid" not in prompt
    for key in ("facts", "decision_html", "pdf"):
        assert stats["inputs"][key]["truncated"] is False, key
    # rubric pointers
    assert "tables_served" in rubric_body and "omits cost by design" in rubric_body


def test_prompt_above_ceiling_is_cut_with_markers_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliverables = _stage_run3(tmp_path, monkeypatch)
    _, _, rubric_body = judge.load_rubric(judge.DEFAULT_RUBRIC_PATH)
    prompt, stats = judge.build_prompt_with_stats(
        rubric_body, RUN3_DB, RUN3_JOB, deliverables, ceiling=30_000
    )
    assert len(prompt) <= 30_000
    md = stats["inputs"]["engineering_md"]
    assert md["truncated"] is True and md["chars"] < md["source_chars"]
    assert '[section "Risk register (11) > dynamodb":' in prompt
    assert "## Migration trade-offs (38)" in prompt  # headings survive the cut
    assert stats["inputs"]["decision_html"]["truncated"] is False  # small inputs stay whole


def test_judge_json_records_prompt_and_input_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_run3(tmp_path, monkeypatch)
    scores = {k: 4 for k in judge.CRITERIA}
    stub = tmp_path / "fake_claude.py"
    outer = {"is_error": False, "result": json.dumps({"scores": scores, "notes": {}})}
    stub.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "if sys.argv[1:] == ['--help']:\n    sys.exit(0)\n"
        f"sys.stdin.read()\nprint({json.dumps(outer)!r})\n"
    )
    stub.chmod(0o755)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=RUN3_DB, job=RUN3_JOB, claude_bin=str(stub)
    )
    assert code == 0, result
    assert 0 < result["prompt_chars"] <= result["prompt_char_ceiling"]
    assert set(result["inputs"]) == {"facts", "decision_html", "pdf", "engineering_md"}
    for entry in result["inputs"].values():
        assert {"source_chars", "chars", "truncated", "sections_truncated"} <= set(entry)


def test_locate_deliverables_finds_matching_assignment(tmp_path: Path) -> None:
    job = tmp_path / "db" / "j"
    synthesis = job / "synthesis" / "v3"
    synthesis.mkdir(parents=True)
    (synthesis / "report.json").write_text("{}")
    (synthesis / "x_decision-report_j.html").write_text("<p>d</p>")
    (synthesis / "x_engineering-report_j.md").write_text("# e")
    (job / "assignment" / "v3").mkdir(parents=True)
    (job / "assignment" / "v3" / "assignment.json").write_text("{}")
    (job / "assignment" / "v2").mkdir(parents=True)

    found = judge.locate_deliverables(tmp_path, "db", "j")
    assert found["assignment"] == job / "assignment" / "v3" / "assignment.json"
    assert found["llm_input"] is None


def test_rubric_thresholds_unchanged() -> None:
    pass_mean, min_score, _ = judge.load_rubric(judge.DEFAULT_RUBRIC_PATH)
    assert (pass_mean, min_score) == (3.5, 2)
