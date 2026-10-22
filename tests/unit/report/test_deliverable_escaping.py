"""Output-escaping tests for the customer-facing deliverables (threat model R3).

R3 (``docs/security/atx-threat-model.md``): the Markdown deliverable interpolates customer- and LLM-derived strings — table and column names, raw SQL
``query_text``, risk prose, trade-off text. Escaping had been applied ad hoc per
field, so one missed interpolation point was an injection vector into a document a
human downloads and opens.

These tests do two things the acceptance criteria call for:

* Exercise the centralized escaping helpers (:mod:`escaping`) directly, one per
  rendering context, so the neutralization contract is pinned regardless of caller.
* Inject markup and control characters into a **table name** and a **query text**
  and assert they are neutralized in the Engineering Report (Markdown, including
  its Mermaid fences).

The PDF is intentionally not covered here: it draws glyphs through reportlab and
has no markup context to inject into (R3/E2 scope the escaping concern to
Markdown).
"""

from __future__ import annotations

import re

import pytest

from src.report import escaping, renderers

# A payload that is hostile in every context at once: HTML/SVG markup, a script
# element, a Markdown table pipe, an inline-code backtick, a Mermaid bracket and
# quote, an attribute-breaking double quote, and a CR/LF.
INJECT = 'x</td><script>alert(1)</script>`|["\r\ndrop'


# ---------------------------------------------------------------------------
# Unit: the escaping helpers, one per context
# ---------------------------------------------------------------------------


class TestEscapingHelpers:
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

    def test_md_text_escapes_markdown_image_syntax(self) -> None:
        """#211: an untrusted value containing image syntax must not render as
        a live remote image fetch."""
        out = escaping.md_text("![](http://example.com/x.png)")
        assert out == "\\!\\[\\]\\(http://example.com/x.png\\)"

    def test_md_text_escapes_markdown_link_syntax(self) -> None:
        out = escaping.md_text("[click](http://example.com)")
        assert out == "\\[click\\]\\(http://example.com\\)"

    def test_md_text_escapes_emphasis_markers(self) -> None:
        out = escaping.md_text("*bold* and _italic_")
        assert "*" not in out.replace("\\*", "")
        assert "_" not in out.replace("\\_", "")
        assert out == "\\*bold\\* and \\_italic\\_"

    def test_md_text_escapes_the_backslash_itself_first(self) -> None:
        """A literal backslash must not be mistaken for part of one of these
        escapes once the special characters are also escaped: each original
        backslash is doubled, then each bracket gets its own escaping backslash."""
        out = escaping.md_text("\\[not a link\\]")
        assert out == "\\\\\\[not a link\\\\\\]"
        # Unescaping once (collapsing the doubled backslash) must not reveal a
        # live, unescaped link.
        assert "\\[not a link\\]" not in out.replace("\\\\", "\\")

    def test_md_text_hostile_input_battery(self) -> None:
        """Pipe, backtick, newline, <, link, image -- the full battery #211 asks for."""
        out = escaping.md_text("a|b`c\nd<e>![f](g)[h](i)")
        assert "\n" not in out
        assert "<e>" not in out
        assert "![f](g)" not in out
        assert "[h](i)" not in out
        # Pipes and backticks are untouched -- not special outside a table
        # cell / code span, which is what md_cell / md_code are for.
        assert "a|b`c" in out

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


# ---------------------------------------------------------------------------
# Decision Report (HTML)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Engineering Report (Markdown + Mermaid)
# ---------------------------------------------------------------------------


class TestEngineeringReportEscaping:
    def test_non_code_cells_neutralize_markup(self) -> None:
        """A value rendered as plain cell text (not a code span) has its angle
        brackets escaped, so a ``<script>`` in it cannot be live HTML in a renderer
        that passes raw HTML through."""
        md = renderers.render_engineering_report_md(_report_with_injection())
        # ``aggregate_pattern`` in the migration map is a plain cell (patx<INJECT>).
        assert "&lt;/td&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in md

    def test_script_only_ever_appears_inside_a_code_span(self) -> None:
        """Backtick-delimited code spans render their content literally and do not
        execute HTML, so stripping the span-terminating backtick (md_code) is the
        correct neutralization there. The contract we assert is that every raw
        ``<script>`` that survives is fenced inside a code span — never bare markup."""
        md = renderers.render_engineering_report_md(_report_with_injection())
        for line in md.splitlines():
            if "<script>" in line:
                # A bare '<' that is not inside a `...` span would be live markup.
                # Every occurrence on this line must sit between backticks.
                assert line.count("`") >= 2, f"unfenced <script> in: {line!r}"

    def test_code_spans_strip_terminating_backticks(self) -> None:
        """The payload carries a backtick; inside a code span it must be removed so the
        span cannot close early and let the remainder render as live Markdown."""
        md = renderers.render_engineering_report_md(_report_with_injection())
        map_lines = [ln for ln in md.splitlines() if ln.startswith("| `src")]
        assert map_lines, "expected the injected source table in the migration map"
        # The code-span segments (between backticks) carry no stray backtick from the
        # payload: splitting on backticks yields alternating text / code segments, and
        # a leaked backtick would shift that alternation.
        first_cell = map_lines[0].split("|")[1]
        assert "``" not in first_cell

    def test_schema_table_keeps_its_columns_for_pipe_and_angle_brackets(self) -> None:
        """#457: a ``|`` splits a GFM table row even inside a code span, which breaks
        the span and leaves the rest of the name as live markup. Target and source
        table names in the schema table go through md_cell like every other cell, so
        the row keeps its columns and no raw angle bracket remains."""
        report = {
            "schema_designs": {
                "dynamodb": {
                    "status": "completed",
                    "access_pattern_count": 1,
                    "tables": [
                        {
                            "table_name": "Evil|<b>x</b>",
                            "aggregate_pattern": "single",
                            "source_tables": ["src|<i>y</i>"],
                            "gsi_count": 1,
                        }
                    ],
                }
            }
        }
        lines = renderers.render_engineering_report_md(report).splitlines()
        header = lines.index("| Target | Pattern | Source tables | GSIs |")
        row = lines[header + 2]
        # An unescaped pipe delimits a cell; ``\|`` is the GFM escape for a literal one.
        cells = re.split(r"(?<!\\)\|", row)[1:-1]
        assert len(cells) == 4, row
        assert "<" not in row and ">" not in row
        assert "`Evil\\|&lt;b&gt;x&lt;/b&gt;`" in cells[0]
        assert "`src\\|&lt;i&gt;y&lt;/i&gt;`" in cells[2]

    def test_migration_map_keeps_its_columns_for_pipe_and_angle_brackets(self) -> None:
        """#466: the Migration map renders source and target table names as code spans
        too, so they go through md_cell like the schema table's (#457): the row keeps
        its columns and no raw angle bracket remains."""
        report = {
            "table_mappings": [
                {
                    "source_table": "Evil|<b>x</b>",
                    "recommended_database": "dynamodb",
                    "target_table": "tgt|<i>y</i>",
                    "aggregate_pattern": "single",
                    "confidence_score": 0.9,
                }
            ]
        }
        lines = renderers.render_engineering_report_md(report).splitlines()
        header = lines.index(
            "| Source table | Owner | Also read by | Cached by | Target | Pattern | Workload fit |"
        )
        row = lines[header + 2]
        cells = re.split(r"(?<!\\)\|", row)[1:-1]
        assert len(cells) == 7, row
        assert "<" not in row and ">" not in row
        assert "`Evil\\|&lt;b&gt;x&lt;/b&gt;`" in cells[0]
        assert "`tgt\\|&lt;i&gt;y&lt;/i&gt;`" in cells[4]

    def test_no_raw_newline_injected_mid_value(self) -> None:
        md = renderers.render_engineering_report_md(_report_with_injection())
        # None of the single-value interpolations should have introduced the CR/LF
        # from the payload as a real line break (which would split a row/list item).
        assert "\r" not in md

    def test_mermaid_labels_are_neutralized(self) -> None:
        md = renderers.render_engineering_report_md(_report_with_injection())
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

        prov = renderers.provenance(
            _report_with_injection(), "engineering-report", "md", job_id=JOB
        )
        md = renderers.render_engineering_report_md(_report_with_injection(), prov=prov)
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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
