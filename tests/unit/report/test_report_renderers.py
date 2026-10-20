"""Unit tests for the customer-facing report renderers (Increment 2).

The renderers (``render_engineering_report_md`` and the PDF report's sections)
and their helpers are pure functions of the synthesis ``report.json``. They are
exercised against a committed fixture — the real report from the deployed
``v2-e2e-09`` run — so the assertions check genuine reconciliation
($814.12 / 100% workload / 90 tables that migrate) and the executive/engineering
content split, not a hand-built stub.

The ATX orchestrator's publish wiring (``_publish_synthesis_deliverables`` /
``run_synthesis_via_a2a``) is exercised separately in
``tests/unit/atx_orchestrator/test_synthesis_deliverables.py`` — this module
covers only the renderers themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.report import architecture, engineering_report, renderers
from src.shared import report_facts

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


# =============================================================================
# The five-engine picture, as the engineering report and the PDF report show it


class TestFiveEngineReconciliation:
    def test_five_engine_reconciliation(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        for eng in ("Aurora PostgreSQL", "ElastiCache", "DocumentDB", "DynamoDB", "OpenSearch"):
            assert f"| {eng} |" in m
        migrated = report_facts.compute_report_facts(report)["tables_migrate"]
        assert f"| {migrated} tables migrate |" in m

    def test_both_reports_state_the_reconciled_total(self, report: dict) -> None:
        from tests.executive_pdf import engineering_text, executive_text

        assert "| Total | | $814.12 |" in engineering_text(report)
        assert "Total $814.12" in executive_text(report)

    def test_architecture_engines_all_five_with_roles(self, report: dict) -> None:
        engines = architecture._architecture_engines(report)
        assert len(engines) == 5
        total_workload = sum(e.get("workload", 0) for e in engines)
        assert 99.0 <= total_workload <= 101.0
        total_cost = sum(e.get("cost", 0) for e in engines)
        assert abs(total_cost - 814.12) < 1.0
        roles = {e["role"] for e in engines}
        # at least the three role kinds present: retained, migration target, cache
        assert any("etain" in r for r in roles)
        assert any("igration" in r for r in roles)
        assert any("ache" in r for r in roles)

    def test_risk_posture_counts_and_strategies(self, report: dict) -> None:
        from tests.executive_pdf import engineering_text

        text = engineering_text(report)
        assert "Open risks: 22" in text
        assert "Mitigation strategies" in text


# =============================================================================
# Engineering Report (Markdown, build team)


class TestEngineeringReport:
    def test_renders_markdown(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert m.startswith("# Database Modernization")
        assert len(m) > 5000

    def test_risk_register_present_and_filtered(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "## Risk register (22)" in m
        # the six malformed empty risks ([engine] unknown: with no body) are dropped.
        # (Note "unknown:" can still appear as a sub-type label on a KEPT risk that
        # has a real body after it, so we check the empty IDs, not the literal.)
        for rid in ("RISK-007", "RISK-008", "RISK-012", "RISK-013", "RISK-014", "RISK-015"):
            assert rid not in m

    def test_tradeoffs_by_engine(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "## Migration trade-offs (44)" in m
        assert "### DocumentDB" in m

    def test_has_mermaid_fences(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "```mermaid" in m

    def test_unsupported_patterns_render_as_readable_markdown(self, report: dict) -> None:
        """Issue #204: the engineering report used to print ``- {dict repr}`` for
        each unsupported pattern. The fixture covers all four schema-design
        contract shapes (dynamodb/opensearch key ids as ``query_ids``,
        documentdb/elasticache as ``source_query_ids``; dynamodb has no
        ``reason``, opensearch has both ``reason`` and ``recommendation``)."""
        m = renderers.render_engineering_report_md(report)
        assert "Unsupported patterns" in m
        # No Python dict/list repr in the unsupported-patterns bullet list itself
        # (bounded to the next bold heading / section break, since an unrelated
        # field a few lines later -- migration_notes -- is out of scope for #204).
        start = m.index("Unsupported patterns")
        rest = m[start:]
        end = min((i for i in (rest.find("\n\n**"), rest.find("\n\n##")) if i != -1), default=-1)
        section = rest if end == -1 else rest[: end + 1]
        for marker in ("{'", '{"', "': ", "[]", "query_ids", "source_query_ids"):
            assert marker not in section, f"found raw repr marker {marker!r} in:\n{section}"

    def test_unsupported_pattern_md_handles_all_schema_shapes(self) -> None:
        dynamodb_style = {
            "query_ids": ["254f282ce4da5837c67df5f5d405e1ad3c405cee2c14420378bd8dfd0d86787"],
            "pattern_type": "aggregation",
            "recommendation": "COUNT(*) on wp_postmeta: run a Query with Select=COUNT.",
        }
        line = engineering_report._unsupported_pattern_md(dynamodb_style)
        assert "{" not in line and "[" not in line and "'" not in line
        assert "aggregation" in line
        assert "254f282c" in line
        # Parens/asterisks/underscores are backslash-escaped by md_text (#211),
        # so check the content survived rather than the exact punctuation.
        assert "COUNT" in line and "wp" in line and "postmeta" in line

        elasticache_style = {
            "source_query_ids": ["59163c184972d1ec4ad95106a5fc20c95d19dd06d071f956bf50e0c51ce12bd"],
            "reason": "LEFT JOIN with multiple LIKE predicates cannot be a key lookup.",
            "workaround": "Maintain a secondary index set.",
        }
        line = engineering_report._unsupported_pattern_md(elasticache_style)
        assert "{" not in line and "[" not in line and "'" not in line
        assert "LEFT JOIN" in line
        assert "Maintain a secondary index set" in line

        opensearch_style = {
            "query_ids": ["abc12345"],
            "source_query": "SELECT * FROM t",
            "reason": "full text search",
            "recommendation": "use match query",
        }
        line = engineering_report._unsupported_pattern_md(opensearch_style)
        assert "{" not in line and "[" not in line and "'" not in line
        assert "full text search" in line
        assert "use match query" in line

    def test_unsupported_pattern_md_many_ids_truncated(self) -> None:
        many = {"query_ids": [f"id{i}" * 4 for i in range(10)], "reason": "too many to list"}
        line = engineering_report._unsupported_pattern_md(many)
        assert "+7 more" in line

    def test_unsupported_pattern_md_documentdb_shape(self) -> None:
        """documentdb: source_query_ids + reason, workaround optional (often None)."""
        documentdb_style = {
            "source_query_ids": ["abc123"],
            "reason": "CTE queries with window functions are not supported.",
            "workaround": None,
        }
        line = engineering_report._unsupported_pattern_md(documentdb_style)
        assert "{" not in line and "[" not in line and "'" not in line
        assert "CTE queries with window functions" in line
        # #1170: no pattern_type, so no generic "unsupported pattern" label; the
        # entry names its query instead.
        assert "unsupported pattern" not in line
        assert line.startswith("Query `abc123`: ")

    def test_unsupported_pattern_md_string_query_ids_is_one_id(self) -> None:
        """A bare string under query_ids is one malformed id, not a list to
        explode into characters (minor #204 follow-up)."""
        line = engineering_report._unsupported_pattern_md({"query_ids": "abc12345", "reason": "x"})
        assert "`abc12345`" in line
        # exploding into characters would produce many single-char code spans
        assert "`a`" not in line

    def test_unsupported_pattern_md_drops_falsy_ids(self) -> None:
        line = engineering_report._unsupported_pattern_md(
            {"query_ids": ["real-id", "", None], "reason": "x"}
        )
        assert "`real-id`" in line
        assert line.count("`") == 2

    def test_unsupported_pattern_md_strips_asterisks_from_pattern_type(self) -> None:
        line = engineering_report._unsupported_pattern_md(
            {"pattern_type": "*aggregation*", "reason": "x"}
        )
        # Exactly the Markdown-bold wrapper around the stripped label -- not the
        # original value's own asterisks plus the wrapper's.
        assert line.count("*") == 4
        assert "**aggregation**" in line

    def test_migration_notes_render_as_readable_markdown(self, report: dict) -> None:
        """``migration_notes`` is a list of dicts; it used to be interpolated with
        ``escaping.md_text(notes)``, printing the Python list/dict repr directly
        (e.g. ``[{'object_name': 'category_permission_trigger', ...}]``)."""
        m = renderers.render_engineering_report_md(report)
        assert "Migration notes" in m
        start = m.index("Migration notes")
        rest = m[start:]
        end = min((i for i in (rest.find("\n\n**"), rest.find("\n\n##")) if i != -1), default=-1)
        section = rest if end == -1 else rest[: end + 1]
        for marker in ("{'", '{"', "object_name", "application_logic_required"):
            assert marker not in section, f"found raw repr marker {marker!r} in:\n{section}"

    def test_migration_note_md_renders_one_bullet(self) -> None:
        note = {
            "object_name": "category_permission_trigger",
            "object_type": "trigger",
            "source_table": "discourse.categories",
            "application_logic_required": "Implement a Change Stream listener.",
        }
        line = engineering_report._migration_note_md(note)
        assert "{" not in line and "'" not in line
        assert "trigger" in line
        # Underscores are backslash-escaped by md_text (#211).
        assert "category" in line and "permission" in line and "trigger" in line
        assert "Implement a Change Stream listener" in line

    def test_migration_note_md_tolerant_of_missing_keys(self) -> None:
        line = engineering_report._migration_note_md({})
        assert "{" not in line and "'" not in line
        line = engineering_report._migration_note_md({"object_name": "x"})
        assert "{" not in line and "'" not in line
        assert "x" in line

    def test_migration_notes_as_a_bare_string_renders_one_bullet_not_one_per_char(
        self, report: dict
    ) -> None:
        """migration_notes is contractually a list of dicts, but a malformed
        report could carry a bare string; iterating it would silently produce
        one bullet per character instead of raising or rendering sanely."""
        rep = json.loads(json.dumps(report))
        rep["schema_designs"]["documentdb"]["migration_notes"] = "a free-text note"
        m = renderers.render_engineering_report_md(rep)
        assert "- a free-text note" in m
        assert "- a\n" not in m


# =============================================================================
# Empty-risk filter helpers


class TestRiskFilter:
    def test_filters_six_empty_risks(self, report: dict) -> None:
        risks = report["risk_assessment"]["risks"]
        kept = [r for r in risks if report_facts.risk_has_content(r.get("description"))]
        assert len(risks) == 28
        assert len(kept) == 22

    def test_engine_and_body_parse(self) -> None:
        eng, body = report_facts.risk_engine_and_body("[documentdb] Queries joining 3+ tables")
        assert eng == "documentdb"
        assert body == "Queries joining 3+ tables"

    def test_has_content_predicate(self) -> None:
        assert report_facts.risk_has_content("[elasticache] unknown: ") is False
        assert report_facts.risk_has_content("[documentdb] a real risk") is True
        assert report_facts.risk_has_content("") is False


# =============================================================================
# Engine roles — an engine that won no queries is not "Retained"


@pytest.fixture
def zero_workload_report(report: dict) -> dict:
    """The real report, reshaped so DocumentDB is the engine that won nothing.

    Reproduces the observed defect: triage selected DocumentDB, analysis scored it 56%,
    then the assignment routed every query elsewhere, so schema design was skipped.
    """
    rep: dict = json.loads(json.dumps(report))
    pct = {
        "aurora_postgresql": 49.3,
        "elasticache": 29.4,
        "dynamodb": 15.1,
        "opensearch": 6.1,
        "documentdb": 0.0,
    }
    for r in rep["ranking"]:
        r["workload_percent"] = pct.get(r["target"])
    rep["recommended_architecture"]["databases"] = [
        d for d in rep["recommended_architecture"]["databases"] if d["service"] != "documentdb"
    ]
    rep.setdefault("schema_designs", {})["documentdb"] = {"status": "skipped"}
    return rep


class TestEngineRole:
    def test_zero_workload_is_evaluated_not_retained(self) -> None:
        """The defect: a skipped design used to imply "Retained" regardless of workload."""
        assert (
            report_facts.engine_role(
                "documentdb", set(), {"documentdb": {"status": "skipped"}}, 0.0
            )
            == "Evaluated"
        )
        assert report_facts.engine_role("documentdb", set(), {}, None) == "Evaluated"

    def test_workload_carrying_engine_with_no_design_is_still_retained(self) -> None:
        """Guards against over-correcting: the source relational core keeps Retained."""
        role = report_facts.engine_role(
            "aurora_postgresql", set(), {"aurora_postgresql": {"status": "not_available"}}, 49.3
        )
        assert role == "Retained"

    def test_cache_and_migration_target_keep_their_role_at_zero_workload(self) -> None:
        """Branch-order guard: the workload test must not outrank these two."""
        assert report_facts.engine_role("elasticache", set(), {}, 0.0) == "Cache layer"
        assert report_facts.engine_role("dynamodb", {"dynamodb"}, {}, 0.0) == "Migration target"

    def test_scope_reads_no_queries_assigned(self, zero_workload_report: dict) -> None:
        rows = {e["engine"]: e for e in architecture._architecture_engines(zero_workload_report)}
        assert rows["documentdb"]["role"] == "Evaluated"
        assert rows["documentdb"]["scope"] == "no queries assigned"
        # the engines that do the work are unaffected
        assert rows["aurora_postgresql"]["role"] == "Retained"
        assert rows["elasticache"]["role"] == "Cache layer"

    def test_evaluated_engine_excluded_from_retained_narrative(
        self, zero_workload_report: dict
    ) -> None:
        """The HTML claimed DocumentDB was "retained as the relational core"."""
        engines = architecture._architecture_engines(zero_workload_report)
        assert [e["engine"] for e in engines if e["role"] == "Retained"] == ["aurora_postgresql"]

    def test_evaluated_engine_excluded_from_wave_one(self, zero_workload_report: dict) -> None:
        """Wave 1 is built from ``no_move``, and its confidence is a ``min()`` over members.

        An engine the assignment routed nothing to must not be named in the wave nor be
        eligible to set its confidence floor. On the observed ``discourse`` report the
        printed figure does not move (ElastiCache independently sits at the same 56%), so
        this asserts membership rather than the number — the number is only distorted when
        the Evaluated engine happens to be the sole minimum, and asserting it here would
        encode a coincidence of that one report.
        """
        engines = architecture._architecture_engines(zero_workload_report)
        no_move = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in no_move] == ["aurora_postgresql", "elasticache"]
        assert "documentdb" not in {e["engine"] for e in no_move}

    def test_footer_names_and_percentage_agree(self, zero_workload_report: dict) -> None:
        """Tested positively on role, so an Evaluated engine is not named as "keeping" workload."""
        engines = architecture._architecture_engines(zero_workload_report)
        kept = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in kept] == ["aurora_postgresql", "elasticache"]
        assert abs(sum(e["workload"] for e in kept) - 78.7) < 0.05
