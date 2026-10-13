"""Regression fixture for the #572/#587 real-model reviews.

Round 2: a real-model run (schema design present, which the deterministic
``--llm-mode none`` samples never exercise) flagged 70+ findings in
``report.json``, 20 in the engineering report and 16 in the analysis report,
all false positives -- key templates (``wp_options:{option_name}``) and the
engine id ElastiCache's alternative entry carries are legitimate
customer-facing identifiers, not internal wording. That fix was to the guard
(``ID_FIELD_PATHS``, the snake_case detector), never the deliverables.

Round 3: the SAME real-model validation then found a genuine leak the guard
was right to flag -- the interactive analysis report's "Aurora Design"
section printed the raw ``Optimization.category`` code directly
(``"read_replica: Aurora cluster"``, ``"io_optimized: Aurora MySQL
cluster"``). That fix was to the deliverable (``src/shared/
optimization_labels.py``, ``ExportReport.js``), not the guard -- see that
module's docstring. The analysis-report fixture below is extended with this
section so the deterministic suite covers it too.

Round 4: a THIRD real-model run (schema design again) flagged
``$.query_groups[35].access_patterns[0].description`` -- ordinary model-
written prose, "ordered by meta_id: one Query per comment_id", a column
name followed by a colon. The snake_case detector's old rule (any
snake_case-plus-colon shape followed by prose) can never be stable against
free text a model writes: a real run will keep producing new column/field
names this way. The fix is in the guard itself
(``src.tools.validation.customer_text_guard``'s ``KNOWN_REASON_CODES``/
``REASON_CODE_FIELDS``/``REASON_CODE_FIELD_PATHS`` -- see that module's
docstring): the shape alone is only trusted inside a field this pipeline's
own code writes deterministically; everywhere else the token must also be
one of a known list of internal codes imported from where each is actually
defined (tie kinds, gate kinds, blocking rules, exclusion rule ids), never
retyped here. ``query_groups`` (plus its planted ``meta_id`` sentence) is
added to the fixture below so the deterministic suite covers this real
shape too.

Source: ``~/.cache/modernizer-ops/evidence/pr587/test-results/llm-both-wordpress/
deliverables/wordpress/`` (the public wordpress sample; a `chat` llm_mode run
with schema design). ``schema_design_report.json`` is ``report.json``'s
``table_mappings`` and ``schema_designs`` (dynamodb, elasticache, aurora_mysql),
trimmed to a handful of entries each -- the sections the reviews named.
``schema_designs.aurora_mysql.optimizations`` keeps all four of that run's
real entries (``index`` x2, ``read_replica``, ``other``) plus one synthetic
``io_optimized`` and one synthetic ``partitioning`` entry, clearly marked, so
every category the Aurora contracts allow is covered, not just the ones one
particular run happened to produce.
``schema_design_engineering_report_excerpt.md`` is the "Migration map" table
and the "### ElastiCache" target-schema table from the rendered engineering
report (both show backtick-quoted key templates). ``schema_design_analysis_report_
excerpt.txt`` is the ElastiCache access-pattern table from the analysis
report's Playwright-rendered ``inner_text("body")`` (plain table-cell text,
no backticks) -- the exact shape ``tests/e2e/test_report_html.py`` scans --
plus the Aurora Design section's optimizations list, re-rendered through the
FIXED ``ExportReport.js`` (jsdom ``textContent``, reformatted onto separate
lines for readability; the real run's own saved HTML still has the round-3
bug baked in, since it was generated before the fix, so it cannot be reused
as-is for this part)."""

from __future__ import annotations

import json
from pathlib import Path

from src.tools.validation.customer_text_guard import scan_json, scan_markdown, scan_text

FIXTURES = Path(__file__).parent / "fixtures"


def _report() -> dict:
    return json.loads((FIXTURES / "schema_design_report.json").read_text())  # type: ignore[no-any-return]


def _engineering_excerpt() -> str:
    return (FIXTURES / "schema_design_engineering_report_excerpt.md").read_text()


def _analysis_excerpt() -> str:
    return (FIXTURES / "schema_design_analysis_report_excerpt.txt").read_text()


class TestRealModelReportJsonIsClean:
    def test_table_mappings_and_schema_designs_have_no_findings(self) -> None:
        assert scan_json(_report()) == []

    def test_fixture_actually_exercises_the_round_2_shapes(self) -> None:
        """A guard regression (reverting the ID_FIELD_PATHS fix) must have
        something real to catch here -- this is not a vacuously empty fixture."""
        report = _report()
        alternatives = [
            alt for m in report["table_mappings"] for alt in (m.get("alternatives") or [])
        ]
        assert any(alt.get("database") == "elasticache" for alt in alternatives)
        assert any(":{" in (alt.get("target_table") or "") for alt in alternatives)
        ec_tables = report["schema_designs"]["elasticache"]["tables"]
        assert any(":{" in t["table_name"] for t in ec_tables)
        assert any(
            ":{" in tt
            for sd in report["schema_designs"].values()
            for to in sd.get("trade_offs", [])
            for tt in to.get("target_tables", [])
        )

    def test_fixture_carries_every_optimization_category_the_contracts_allow(self) -> None:
        """#587 round 3: the real leak was in the rendered text, not the raw
        JSON (``category`` alone has no colon, so the guard never flagged
        it here) -- this just confirms the fixture's own coverage claim."""
        opts = _report()["schema_designs"]["aurora_mysql"]["optimizations"]
        categories = {o["category"] for o in opts}
        assert categories == {"partitioning", "index", "read_replica", "io_optimized", "other"}

    def test_fixture_carries_the_round_4_column_name_sentence_unflagged(self) -> None:
        """#587 round 4: the real false positive -- a model-written access
        pattern description mentioning a column name (``meta_id``) before a
        colon must never be mistaken for a snake_case reason code leaking."""
        report = _report()
        description = report["query_groups"][0]["access_patterns"][0]["description"]
        assert description == "ordered by meta_id: one Query per comment_id"
        assert scan_json(report) == []

    def test_a_planted_leak_in_a_prose_field_is_still_caught(self) -> None:
        """The exclusions are by field, not by value shape -- a real leak
        planted in an actual prose field (trade_offs[].description) next to
        all these identifiers is still caught."""
        report = _report()
        report["schema_designs"]["elasticache"]["trade_offs"][0][
            "description"
        ] = "Consolidated because documentdb provides no unique value here."
        findings = scan_json(report)
        assert [f.kind for f in findings] == ["engine_id"]
        assert findings[0].text == "documentdb"


class TestRealModelEngineeringReportExcerptIsClean:
    def test_migration_map_and_elasticache_tables_have_no_findings(self) -> None:
        assert scan_markdown(_engineering_excerpt()) == []

    def test_fixture_actually_has_key_templates_in_backticks(self) -> None:
        text = _engineering_excerpt()
        assert "`wp_options:{option_name}`" in text
        assert "`wp_postmeta:{post_id}:meta`" in text

    def test_a_planted_reason_code_leak_is_still_caught(self) -> None:
        text = _engineering_excerpt() + "\n\ntie_unresolved: no routing rule decides.\n"
        findings = scan_markdown(text)
        assert [f.kind for f in findings] == ["snake_case_reason_code"]
        assert findings[0].text == "tie_unresolved:"


class TestRealModelAnalysisReportExcerptIsClean:
    def test_elasticache_access_pattern_table_has_no_findings(self) -> None:
        assert scan_text(_analysis_excerpt()) == []

    def test_fixture_actually_has_key_templates_with_no_backticks(self) -> None:
        """Unlike the Markdown report, the analysis report's rendered table
        cells carry no backticks at all -- the colon-not-followed-by-whitespace
        rule has to work without the code-span exemption's help here."""
        text = _analysis_excerpt()
        assert "wp_options:{option_name}" in text
        assert "`" not in text

    def test_fixture_shows_the_fixed_aurora_optimization_labels(self) -> None:
        """#587 round 3: the real leak -- confirms the fixture actually
        exercises the fixed shape, not a vacuously clean one."""
        text = _analysis_excerpt()
        assert "Read replica: Aurora cluster" in text
        assert "I/O-Optimized storage: Aurora MySQL cluster" in text
        assert "read_replica" not in text
        assert "io_optimized" not in text

    def test_a_planted_engine_id_leak_is_still_caught(self) -> None:
        text = _analysis_excerpt() + "\nMoved to aurora_mysql because of a tie.\n"
        findings = scan_text(text)
        assert [f.kind for f in findings] == ["engine_id"]
        assert findings[0].text == "aurora_mysql"
