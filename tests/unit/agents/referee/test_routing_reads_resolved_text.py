# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every routing decision reads a statement with its views resolved (#1875).

A statement on a view has a ``view_resolved_text`` (the direct join, the view's body as
a derived table, or a write onto the base table), and ``view_lineage.access_text`` is
the one way a routing step reads a statement's text: the hard exclusions, the
capability gates, triage, the analyses, the cache overlay and the reality-check floors
all see the same statement. This guard scans every module of the ruleset
(``src.assessment.ruleset.ruleset_files``, what ``assess()`` hashes because it can
change a statement's facts, scores or engine) for a direct read of a statement's
``query_text`` key, and fails on one that is not listed below with why it may.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.assessment import ruleset

_READ_RE = re.compile(r"""get\(\s*["']query_text["']|\[\s*["']query_text["']\s*\]""")

#: Modules that collect, redact or build the texts: they write ``query_text`` itself.
ALLOWED_MODULES: dict[str, str] = {
    "agents/collector/": "the collectors read the source's own statement text",
    "tools/database/": "the collection parsers read and redact the collected text",
    "tools/aws/": "Performance Insights rows are collected text",
    "tools/sql/redact.py": "redacts query_text itself",
    "tools/sql/view_lineage.py": "builds the resolved text, and access_text reads both",
    "agents/referee/synthesis_report.py": "the report quotes and counts statements as collected",
    "agents/referee/synthesis_handler.py": "the report quotes statements as collected",
    "tools/analysis/relational_gaps.py": (
        "the gap check reads every statement and every view definition, each on its own"
    ),
}

#: Single reads that are not a routing fact, by module and the text of the read.
ALLOWED_READS: dict[tuple[str, str], str] = {
    (
        "agents/referee/access_pattern.py",
        'qid = str(query.get("query_id") or query.get("query_text") or "")',
    ): "an identity key for a statement with no id, not a fact",
    (
        "tools/sql/query_facts.py",
        'str(pattern.get("query_text") or ""), engine, pattern.get("parsed_facts")',
    ): "the collector's stored readings are of query_text; access_text reads the rest fresh",
    (
        "agents/referee/reality_check_request.py",
        'text = q.get("query_text", "") or ""',
    ): "the reviewer model is shown the statement as the application sends it",
    (
        "agents/schema_design/group_input.py",
        'text = slim.get("query_text") or ""',
    ): "the designer's input, which lists the views the statement reads beside it",
}


#: Beyond the ruleset's import closure, the routing passes the pipeline runs after it
#: (feasibility review, post-schema cost) live here too.
ROUTING_DIRS = ("agents/referee", "tools/analysis")


def _paths() -> list[Path]:
    paths = {Path(p) for p in ruleset.ruleset_files()}
    for folder in ROUTING_DIRS:
        paths |= set((ruleset._SRC / folder).glob("*.py"))
    return sorted(paths)


def _reads() -> list[tuple[str, str]]:
    out = []
    for path in _paths():
        if path.suffix != ".py":
            continue
        rel = Path(path).relative_to(ruleset._SRC).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if _READ_RE.search(line):
                out.append((rel, line.strip()))
    return out


def _allowed(rel: str, line: str) -> bool:
    if any(rel.startswith(prefix) for prefix in ALLOWED_MODULES):
        return True
    if "query_text_preview" in line:  # a display excerpt in a decision trace
        return True
    return (rel, line) in ALLOWED_READS


def test_no_routing_module_reads_query_text_where_a_resolved_text_exists() -> None:
    bad = [f"{rel}: {line}" for rel, line in _reads() if not _allowed(rel, line)]
    assert not bad, (
        "A routing-path module reads query_text directly: read "
        "src.tools.sql.view_lineage.access_text(query) instead (#1875), or list the "
        "read in ALLOWED_READS with why it is no routing fact.\n" + "\n".join(bad)
    )


@pytest.mark.passes_on_main  # it keeps the list honest; the guard above is the check
def test_every_listed_read_still_exists() -> None:
    reads = set(_reads())
    stale = [key for key in ALLOWED_READS if key not in reads]
    assert not stale, f"Listed reads that are gone: {stale}"
