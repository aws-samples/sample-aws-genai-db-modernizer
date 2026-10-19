"""The engineering report's per-query appendix groups near-identical entries (#1108).

Runs on the cached discourse job (the sample the rubric judge flagged at
scale): every listed query with a full trace is in exactly one group, the
appendix prints one entry per group, and every query's short id appears in it
once. The entry counts are printed so a run shows the before and after.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

APPENDIX = "## Appendix: per-query routing decisions"


def _appendix(run: PipelineResult) -> str:
    md = run.deliverable("engineering-report.md").read_text()
    start = md.index(APPENDIX)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


@pytest.mark.deterministic
def test_discourse_appendix_groups_without_losing_a_query(
    discourse_run: PipelineResult, capsys: pytest.CaptureFixture[str]
) -> None:
    facts = json.loads(discourse_run.report_json().read_text())["facts"]["query_trace"]
    in_full = [r["query_id"] for r in facts["listed_queries"] if r["in_full"]]
    groups = facts["trace_groups"]
    grouped = [q for g in groups for q in g["query_ids"]]
    assert sorted(grouped) == sorted(in_full)
    assert len(set(grouped)) == len(grouped)
    assert facts["trace_group_count"] == len(groups)

    appendix = _appendix(discourse_run)
    entries = len(re.findall(r"^### ", appendix, flags=re.MULTILINE))
    assert entries == len(groups)
    assert entries < len(in_full), "discourse has near-identical entries to group"
    for g in groups:
        for qid, short in zip(g["query_ids"], g["short_ids"], strict=True):
            token = f"`{short}`" if g["query_count"] > 1 else f"`{qid}`"
            assert appendix.count(token) == 1, qid
    with capsys.disabled():
        print(
            f"\n#1108 discourse appendix: {len(in_full)} listed queries with a full trace: "
            f"{len(in_full)} entries before, {entries} after "
            f"(largest group {max(g['query_count'] for g in groups) if groups else 0})"
        )
