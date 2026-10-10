"""#478: Aurora's queries live in ``query_groups`` like any other
engine's (``build_query_groups``'s relational branch), since Aurora's schema
design has no ``access_patterns`` of its own (#157 adds real ones later).

Covers the two Engineering Report consequences:
- "Target schemas by engine" says "N queries" for a relational engine, with
  its display name in the heading, instead of "0 access patterns" with the
  raw contract key.
- A new "Queries on Aurora by table" subsection, scoped to each Aurora
  engine's own entries within a group even when the group is shared with
  another engine (e.g. the cache layer reading the same source table), and
  never ranking the two collector/utility "blame" groups as a busiest table.
"""

from __future__ import annotations

from typing import Any

from src.report import renderers


def _ap(engine: str, pattern_id: str, query_ids: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "engine": engine,
        "pattern_id": pattern_id,
        "query_ids": query_ids,
        "in_scope": True,
        **extra,
    }


def _report() -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "schema_designs": {
            "aurora_mysql": {
                "status": "completed",
                "tables": [
                    {"table_name": "wp_posts", "source_tables": ["wp.wp_posts"]},
                    {"table_name": "wp_postmeta", "source_tables": ["wp.wp_postmeta"]},
                ],
                "access_pattern_count": 0,
            },
            "elasticache": {
                "status": "completed",
                "tables": [{"table_name": "post:{id}", "source_tables": ["wp.wp_posts"]}],
                "access_pattern_count": 1,
            },
        },
        "query_groups": [
            {
                "group_name": "wp.wp_posts",
                "engines": ["aurora_mysql", "elasticache"],
                "access_patterns": [
                    _ap("aurora_mysql", "relational-aurora_mysql-1", ["q1"], design_rps=5.0),
                    _ap("elasticache", "EC-AP-1", ["q1"], design_rps=12.0),
                ],
            },
            {
                "group_name": "wp.wp_postmeta",
                "engines": ["aurora_mysql"],
                "access_patterns": [
                    _ap(
                        "aurora_mysql",
                        "relational-aurora_mysql-2",
                        ["q2"],
                        design_rps=1.0,
                        description="relational core",
                    )
                ],
            },
            {
                "group_name": "Table not identified by the collector",
                "engines": ["aurora_mysql"],
                "access_patterns": [
                    _ap(
                        "aurora_mysql",
                        "relational-aurora_mysql-3",
                        ["q3"],
                        design_rps=999.0,
                    )
                ],
            },
            {
                "group_name": "Utility and session statements",
                "engines": ["aurora_mysql"],
                "access_patterns": [
                    _ap(
                        "aurora_mysql",
                        "relational-aurora_mysql-4",
                        ["q4"],
                        design_rps=998.0,
                    )
                ],
            },
        ],
    }


class TestTargetSchemasHeading:
    def test_relational_engine_heading_says_queries_with_display_name(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        assert "### Aurora MySQL (2 target objects, 4 queries)" in md
        assert "aurora\\_mysql" not in md

    def test_non_relational_engine_heading_is_unchanged(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        assert "### elasticache (1 target object, 1 access pattern)" in md


class TestQueriesOnAuroraByTable:
    def test_subsection_exists_scoped_to_the_engine(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        assert "## Queries on Aurora by table" in md
        section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
        assert "### Aurora MySQL (4 queries)" in section
        # The shared group's elasticache entry does not inflate Aurora's count.
        assert "| wp.wp_posts | 1 |" in section

    def test_blame_groups_appear_but_never_as_busiest(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("| ")][1:]  # drop header
        # Despite having the highest calls/s, the two blame rows sort last.
        assert "Table not identified by the collector" in rows[-2]
        assert "Utility and session statements" in rows[-1]
        assert "wp.wp_posts" in rows[0] or "wp.wp_postmeta" in rows[0]

    def test_absent_without_any_relational_engine(self) -> None:
        report = _report()
        report["query_groups"] = [
            g for g in report["query_groups"] if "aurora_mysql" not in (g.get("engines") or [])
        ]
        md = renderers.render_engineering_report_md(report)
        assert "## Queries on Aurora by table" not in md

    def test_reason_text_for_a_none_entry_in_reasons_is_empty_not_the_word_none(self) -> None:
        """#478: matches the JS ``reasonTextFor``'s
        ``reasons[reasonIndex] || ''`` -- a ``None`` entry (shouldn't happen,
        but never worth shipping) resolves to an empty string."""
        group = {"reasons": [None, "a real reason"]}
        assert renderers._reason_text_for(group, {"reason_index": 0}) == ""
        assert renderers._reason_text_for(group, {"reason_index": 1}) == "a real reason"

    def test_long_joined_reasons_are_cut_on_a_word_boundary(self) -> None:
        """#478: the joined "Reason" cell used to hard-cut at
        200 characters, sometimes mid-word; it must back up to a word
        boundary like every other clipped cell in the deliverables."""
        report = _report()
        long_reason = (
            "this reason sentence repeats itself many times over so that the joined "
            "description text comfortably exceeds the two hundred character budget "
            "available for a single table row in the Queries on Aurora by table section"
        )
        report["query_groups"][1]["access_patterns"][0]["description"] = long_reason
        md = renderers.render_engineering_report_md(report)
        section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
        row = next(line for line in section.splitlines() if "wp.wp_postmeta" in line)
        assert "…" in row
        # No partial word right before the ellipsis.
        clipped = row.split("|")[-2].strip()
        assert clipped.endswith("…")
        before_ellipsis = clipped[: -len("…")].rstrip()
        assert long_reason.startswith(before_ellipsis)
        next_char_index = len(before_ellipsis)
        assert next_char_index == len(long_reason) or long_reason[next_char_index] == " "


class TestGenericQueryGroupsTable:
    """#478: the generic '## Query groups' table must not
    duplicate what the dedicated Aurora subsection already shows in full,
    and must name engines by their display name, not the raw contract key."""

    def test_a_pure_aurora_group_is_not_listed_in_the_generic_table(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        section = md.split("## Query groups", 1)[1].split("\n## ", 1)[0]
        # wp.wp_postmeta's group is aurora_mysql-only -- fully covered by
        # the dedicated subsection, so it must not also appear here.
        assert "wp.wp_postmeta" not in section

    def test_a_group_shared_with_another_engine_still_appears(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        section = md.split("## Query groups", 1)[1].split("\n## ", 1)[0]
        # wp.wp_posts is shared with elasticache -- still relevant from the
        # non-relational side, so it stays in the generic table too.
        assert "wp.wp_posts" in section

    def test_engines_column_uses_display_names(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        section = md.split("## Query groups", 1)[1].split("\n## ", 1)[0]
        assert "Aurora MySQL" in section
        assert "aurora_mysql" not in section

    def test_header_count_matches_the_rows_actually_listed(self) -> None:
        md = renderers.render_engineering_report_md(_report())
        section = md.split("## Query groups (", 1)[1]
        count = int(section.split(")", 1)[0])
        rows = [
            line
            for line in section.split("\n## ", 1)[0].splitlines()
            if line.startswith("| ") and not line.startswith("| Group")
        ]
        # The separator row ("|---|---|...") also starts with "| "; drop it.
        rows = [r for r in rows if not r.replace("|", "").replace("-", "").strip() == ""]
        assert count == len(rows)

    def test_heading_points_at_the_omitted_aurora_groups_below(self) -> None:
        """#478: the three pure-Aurora groups excluded from this
        table (wp.wp_postmeta plus the two blame groups) are not missing data
        -- the heading says where they are, since "Queries on Aurora by table"
        renders after this section, not before it."""
        md = renderers.render_engineering_report_md(_report())
        assert "## Query groups (1) (plus 3 Aurora groups below)" in md
        assert md.index("## Query groups (") < md.index("## Queries on Aurora by table")

    def test_heading_has_no_pointer_when_nothing_was_omitted(self) -> None:
        report = _report()
        report["query_groups"] = [
            g
            for g in report["query_groups"]
            if g["group_name"] != "wp.wp_postmeta"
            and g["group_name"]
            not in ("Table not identified by the collector", "Utility and session statements")
        ]
        md = renderers.render_engineering_report_md(report)
        assert "## Query groups (1)" in md
        assert "Aurora group" not in md
