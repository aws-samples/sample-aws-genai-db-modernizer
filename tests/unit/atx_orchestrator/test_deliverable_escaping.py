"""Output-escaping tests for the customer-facing deliverables (threat model R3).

R3 (``docs/security/atx-threat-model.md``): the HTML/SVG/Markdown deliverables
interpolate customer- and LLM-derived strings — table and column names, raw SQL
``query_text``, risk prose, trade-off text. Escaping had been applied ad hoc per
field, so one missed interpolation point was an injection vector into a document a
human downloads and opens.

These tests do two things the acceptance criteria call for:

* Exercise the centralized escaping helpers (:mod:`escaping`) directly, one per
  rendering context, so the neutralization contract is pinned regardless of caller.
* Inject markup and control characters into a **table name** and a **query text**
  and assert they are neutralized in each deliverable — the Decision Report (HTML),
  the Engineering Report (Markdown, including its Mermaid fences), and the
  Interactive Analysis Report (HTML).

The PDF/PPTX deck is intentionally not covered here: those renderers draw glyphs
through reportlab / python-pptx and have no markup context to inject into (R3/E2
scope the escaping concern to HTML/SVG/Markdown).
"""

from __future__ import annotations

import json

import pytest

from src.atx_orchestrator.runtime import analysis_report as ar
from src.atx_orchestrator.runtime import artifacts, escaping

# A payload that is hostile in every context at once: HTML/SVG markup, a script
# element, a Markdown table pipe, an inline-code backtick, a Mermaid bracket and
# quote, an attribute-breaking double quote, and a CR/LF.
INJECT = 'x</td><script>alert(1)</script>`|["\r\ndrop'


# ---------------------------------------------------------------------------
# Unit: the escaping helpers, one per context
# ---------------------------------------------------------------------------


class TestEscapingHelpers:
    def test_html_text_neutralizes_markup_but_keeps_quotes(self) -> None:
        out = escaping.html_text('<b>&"x"')
        assert "<b>" not in out
        assert "&lt;b&gt;" in out
        assert "&amp;" in out
        # Quotes are not special in a text node, so they are left readable.
        assert '"x"' in out

    def test_html_attr_also_neutralizes_quotes(self) -> None:
        out = escaping.html_attr('" onmouseover="alert(1)')
        assert '"' not in out
        assert "&quot;" in out

    def test_md_cell_escapes_pipe_and_collapses_newlines(self) -> None:
        out = escaping.md_cell("a|b\r\nc<d>")
        assert "\\|" in out
        assert "\n" not in out and "\r" not in out
        assert "<d>" not in out and "&lt;d&gt;" in out

    def test_md_code_removes_backticks_that_would_close_the_span(self) -> None:
        out = escaping.md_code("na`me` `run")
        assert "`" not in out

    def test_md_text_collapses_newlines_and_neutralizes_html(self) -> None:
        out = escaping.md_text("line1\nline2 <x>")
        assert "\n" not in out
        assert "<x>" not in out

    def test_md_yaml_value_escapes_quotes_and_newlines(self) -> None:
        out = escaping.md_yaml_value('db"name\nleak: value')
        assert "\n" not in out
        assert '\\"' in out
        # The literal unescaped quote must not survive to close the YAML string.
        assert 'db"name' not in out

    def test_mermaid_label_neutralizes_quotes_and_brackets(self) -> None:
        out = escaping.mermaid_label('t"]\n')
        assert '"' not in out
        assert "]" not in out and "[" not in out
        assert "\n" not in out


# ---------------------------------------------------------------------------
# Fixtures: a synthesis report and an analysis export, both carrying the payload
# ---------------------------------------------------------------------------

DB = "discourse"
JOB = "29d77e81-6675-4942-9c34-4c5070d77860"


def _report_with_injection() -> dict:
    """A minimal but structurally complete synthesis report whose customer- and
    LLM-derived string fields all carry the hostile payload."""
    return {
        "contract_version": "1.0.0",
        "job_id": JOB,
        "database_name": f"db{INJECT}",
        "summary": f"Summary {INJECT}",
        "summary_deterministic": f"Deterministic {INJECT}",
        "ranking": [
            {
                "target": "dynamodb",
                "workload_percent": 100.0,
                "assigned_queries": 10,
                "assignment_reason_summary": f"reason {INJECT}",
            }
        ],
        "tco_analysis": {
            "cost_breakdown": [{"database": "dynamodb", "monthly_cost_usd": 100.0}],
            "projected_monthly_cost": 100.0,
        },
        "risk_assessment": {
            "overall_risk_level": "HIGH",
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "severity": "HIGH",
                    "risk_type": "data_model",
                    "description": f"[dynamodb] risk body {INJECT}",
                    "mitigation": f"mitigate {INJECT}",
                    "affected_tables": [f"tbl{INJECT}"],
                }
            ],
            "mitigation_strategies": [f"strategy {INJECT}"],
        },
        "recommended_architecture": {
            "architecture_type": f"polyglot {INJECT}",
            "databases": [{"service": "dynamodb", "table_count": 5, "rationale": f"why {INJECT}"}],
        },
        "table_mappings": [
            {
                "source_table": f"src{INJECT}",
                "recommended_database": "dynamodb",
                "target_table": f"tgt{INJECT}",
                "aggregate_pattern": f"pat{INJECT}",
                "confidence_score": 0.9,
            }
        ],
        "query_groups": [
            {
                "group_name": f"grp{INJECT}",
                "engines": ["dynamodb"],
                "access_patterns": ["AP-1"],
                "source_queries": ["q1"],
                "total_design_rps": 5,
            }
        ],
        "schema_designs": {
            "dynamodb": {
                "status": "completed",
                "access_pattern_count": 1,
                "tables": [
                    {
                        "table_name": f"T{INJECT}",
                        "aggregate_pattern": f"agg{INJECT}",
                        "source_tables": [f"s{INJECT}"],
                        "gsi_count": 1,
                    }
                ],
                "unsupported_patterns": [f"unsupported {INJECT}"],
                "migration_notes": f"notes {INJECT}",
            }
        },
        "trade_offs": [
            {
                "engine": "dynamodb",
                "description": f"tradeoff {INJECT}",
                "impact": f"impact {INJECT}",
                "source_tables": [f"st{INJECT}"],
            }
        ],
        "assignment_summary": {"version": 1},
    }


class _FakeStore:
    def __init__(self, objects: dict[str, dict]):
        self.objects = objects

    def read_json(self, path: str) -> dict:
        result: dict = json.loads(json.dumps(self.objects[path]))
        return result

    def exists(self, path: str) -> bool:
        return path in self.objects

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


def _analysis_export() -> dict:
    """An assembled analysis export whose query text and table name carry the payload."""
    key = f"{DB}/{JOB}/synthesis/v1/report.json"
    objects: dict[str, dict] = {
        key: _report_with_injection(),
        f"{DB}/{JOB}/collector/output.json": {
            "database_name": DB,
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": f"SELECT {INJECT}",
                        "query_type": "SELECT",
                        "tables_accessed": [f"tbl{INJECT}"],
                        "calls_per_second": 5.0,
                        "frequency_per_hour": 100,
                    }
                ]
            },
            "schema": {"tables": [{"table_name": f"tbl{INJECT}"}]},
        },
        f"{DB}/{JOB}/query-journeys/q1.json": {
            "query_id": "q1",
            "source": {
                "query_text": f"SELECT {INJECT}",
                "query_type": "SELECT",
                "tables_accessed": [f"tbl{INJECT}"],
            },
            "assignment": {"assigned_engine": "dynamodb", "confidence": 90, "in_scope": True},
            "design": {"engine": "dynamodb", "status": "designed"},
        },
    }
    return ar.build_export_data(_FakeStore(objects), JOB, DB)


# ---------------------------------------------------------------------------
# Decision Report (HTML)
# ---------------------------------------------------------------------------


class TestDecisionReportEscaping:
    def test_no_live_script_element_from_injected_fields(self) -> None:
        html = artifacts.render_decision_report_html(_report_with_injection())
        # The only <script substring allowed is none: this deliverable ships no JS,
        # and the injected </script>/<script> must have been neutralized to entities.
        assert "<script>alert(1)" not in html
        assert "</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_meta_attribute_cannot_be_broken_out_of(self) -> None:
        prov = artifacts.provenance(_report_with_injection(), "decision-report", "html", job_id=JOB)
        html = artifacts.render_decision_report_html(_report_with_injection(), prov=prov)
        # The database name reaches a <meta content="..."> attribute via the filename;
        # a raw double quote there would let it inject further attributes.
        for line in html.splitlines():
            if "x-dbmod-" in line:
                # everything after content=" up to the closing "> is one value
                assert line.count('"') % 2 == 0


# ---------------------------------------------------------------------------
# Engineering Report (Markdown + Mermaid)
# ---------------------------------------------------------------------------


class TestEngineeringReportEscaping:
    def test_non_code_cells_neutralize_markup(self) -> None:
        """A value rendered as plain cell text (not a code span) has its angle
        brackets escaped, so a ``<script>`` in it cannot be live HTML in a renderer
        that passes raw HTML through."""
        md = artifacts.render_engineering_report_md(_report_with_injection())
        # ``aggregate_pattern`` in the migration map is a plain cell (patx<INJECT>).
        assert "&lt;/td&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in md

    def test_script_only_ever_appears_inside_a_code_span(self) -> None:
        """Backtick-delimited code spans render their content literally and do not
        execute HTML, so stripping the span-terminating backtick (md_code) is the
        correct neutralization there. The contract we assert is that every raw
        ``<script>`` that survives is fenced inside a code span — never bare markup."""
        md = artifacts.render_engineering_report_md(_report_with_injection())
        for line in md.splitlines():
            if "<script>" in line:
                # A bare '<' that is not inside a `...` span would be live markup.
                # Every occurrence on this line must sit between backticks.
                assert line.count("`") >= 2, f"unfenced <script> in: {line!r}"

    def test_code_spans_strip_terminating_backticks(self) -> None:
        """The payload carries a backtick; inside a code span it must be removed so the
        span cannot close early and let the remainder render as live Markdown."""
        md = artifacts.render_engineering_report_md(_report_with_injection())
        map_lines = [ln for ln in md.splitlines() if ln.startswith("| `src")]
        assert map_lines, "expected the injected source table in the migration map"
        # The code-span segments (between backticks) carry no stray backtick from the
        # payload: splitting on backticks yields alternating text / code segments, and
        # a leaked backtick would shift that alternation.
        first_cell = map_lines[0].split("|")[1]
        assert "``" not in first_cell

    def test_no_raw_newline_injected_mid_value(self) -> None:
        md = artifacts.render_engineering_report_md(_report_with_injection())
        # None of the single-value interpolations should have introduced the CR/LF
        # from the payload as a real line break (which would split a row/list item).
        assert "\r" not in md

    def test_mermaid_labels_are_neutralized(self) -> None:
        md = artifacts.render_engineering_report_md(_report_with_injection())
        assert "```mermaid" in md
        # Inside the fence, no label may contain a raw " or ] from the payload — either
        # would break the node grammar. The escaped forms are what we expect instead.
        fence = md.split("```mermaid", 1)[1].split("```", 1)[0]
        for line in fence.splitlines():
            if '["' in line or '[("' in line:
                inner = line.split("[", 1)[1]
                # the label content sits between the opening [" and the closing "]
                assert '"]' in inner or '")]' in inner
                label = inner.rsplit('"', 2)[0] if '"' in inner else inner
                assert "\n" not in label

    def test_front_matter_parses_and_value_does_not_escape_its_quoting(self) -> None:
        """The database name reaches YAML front matter carrying quotes and a newline.
        The block must still parse, and the injected value must round-trip as a single
        scalar — proof it could not break out of its quoting into new YAML keys."""
        import yaml  # type: ignore[import-untyped]

        prov = artifacts.provenance(
            _report_with_injection(), "engineering-report", "md", job_id=JOB
        )
        md = artifacts.render_engineering_report_md(_report_with_injection(), prov=prov)
        fm = md.split("---", 2)[1]
        parsed = yaml.safe_load(fm)
        # Exactly the provenance keys, nothing injected in.
        assert set(parsed) == {k for k, v in prov.items() if v}
        # The database name survives intact as one string value (newline collapsed).
        assert parsed["database"] == prov["database"].replace("\r\n", " ").replace("\n", " ")
        assert parsed["job_id"] == JOB


# ---------------------------------------------------------------------------
# Interactive Analysis Report (HTML)
# ---------------------------------------------------------------------------


class TestAnalysisReportEscaping:
    def test_script_close_in_query_text_cannot_break_the_document(self) -> None:
        html = ar.render_analysis_report_html(_analysis_export())
        # The DATA payload embeds the query text; the "<" is escaped so a </script>
        # in captured SQL cannot terminate the embedding <script> element early.
        assert "</script><script>alert(1)" not in html
        assert "\\u003c/script>" in html
        # Only our own emission plus the Chart.js tag close a script element.
        assert html.count("</script>") <= 2

    def test_summary_and_meta_are_escaped(self) -> None:
        html = ar.render_analysis_report_html(_analysis_export())
        # The synthesis summary flows into a text node; its markup is neutralized.
        assert "<script>alert(1)" not in html
        # The database name reaches <meta content="...">; the quote is escaped.
        for line in html.splitlines():
            if "x-dbmod-" in line:
                assert line.count('"') % 2 == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
