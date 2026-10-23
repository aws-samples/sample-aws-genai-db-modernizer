"""No deliverable states a saving for an engine the source does not run (#949).

wordpress (MySQL), discourse (PostgreSQL) and trailgear (SQL Server) each run one
relational source and nothing else. Their plans analyse candidate engines and drop
some (DocumentDB on wordpress and trailgear, OpenSearch on discourse); before #949
the decision report read "Removes $307.03/mo by retiring DocumentDB" on wordpress and
listed $349.95/mo for DocumentDB on trailgear. A dropped candidate is kept in
``report.json`` as ``considered_not_recommended``, with no dollar figure.

The synthetic case: the same wordpress collection, recording that the application
already runs a DocumentDB cluster (``metadata.engines_in_use``). The routing is the
same, and retiring that cluster is now a real saving every deliverable states.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult, run_pipeline_from_file, unzip_sample

pytestmark = pytest.mark.deterministic

# "Removes $X/mo", "Already removed ... $X/mo", "Retired by this plan: $X/mo": any
# retirement saving a deliverable can state.
_SAVING = re.compile(r"Removes \$[\d,.]+/mo|removed from this comparison|Retired by this plan")


def _report(run: PipelineResult) -> dict[str, Any]:
    path = sorted((run.job_dir() / "synthesis").glob("v*/report.json"))[-1]
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _texts(run: PipelineResult) -> dict[str, str]:
    return {
        "engineering report": md_plain(run.deliverable("engineering-report.md").read_text()),
        "pdf": "\n".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))),
    }


def test_no_saving_for_an_engine_the_source_does_not_run(run: PipelineResult) -> None:
    if run.external:
        pytest.skip("checks the bundled samples, which record no engine in use")
    report = _report(run)
    tco = report["tco_analysis"]
    assert not tco.get("retired_engine_savings")
    assert tco.get("eliminated_engine_costs") is None  # no longer written (#949)
    rc = json.loads((run.job_dir() / "reality-check" / "output.json").read_text())
    assert all(c["saved_cost_estimate"] == 0 for c in rc["consolidations"])
    assert not any(c.get("removal_saving_claimed") for c in rc["consolidations"])
    for where, text in _texts(run).items():
        hits = _SAVING.findall(text)
        assert not hits, (run.db, where, hits)
    dropped = {e["engine"] for e in report["considered_not_recommended"]}
    # #728: since the per-query switch wordpress first-assigns one query to OpenSearch
    # (a WooCommerce report grouped by date, the aggregation class's time bucket) and the
    # reality check absorbs it, so OpenSearch is considered and dropped there.
    expected = {
        "wordpress": {"documentdb", "opensearch"},
        "discourse": {"opensearch"},
        "trailgear": {"documentdb"},
    }
    assert dropped == expected[run.db]
    assert all(set(e) == {"engine", "absorbed_by"} for e in report["considered_not_recommended"])


@pytest.fixture(scope="module")
def in_use_run(e2e_root: Path) -> PipelineResult:
    """wordpress, with a DocumentDB cluster the application already runs."""
    raw = unzip_sample("wordpress", e2e_root / "input-949")
    text = raw.read_text()
    marker = '"metadata": {'
    assert text.count(marker) == 1
    edited = raw.with_name("wordpress-in-use-collection.json")
    edited.write_text(text.replace(marker, marker + '"engines_in_use": ["documentdb"], '))
    return run_pipeline_from_file(edited, "wordpress", e2e_root / "artifacts-949", "in-use")


def test_retiring_an_engine_the_source_runs_is_still_a_saving(
    in_use_run: PipelineResult, e2e_root: Path
) -> None:
    collector = json.loads((in_use_run.job_dir() / "collector" / "output.json").read_text())
    assert collector["metadata"]["engines_in_use"] == ["documentdb"]
    report = _report(in_use_run)
    # #1196: DocumentDB's own estimate at the db.r6g.large list price ($0.26315/hr, was
    # $0.348: 367.39 - 0.08485 x 730 = 305.45), with its I/O billed over the same 730-hour
    # month as its instance instead of 720 hours (+$1.58 of I/O): 307.03.
    assert report["tco_analysis"]["retired_engine_savings"] == [
        {"database": "documentdb", "monthly_cost_usd": 307.03}
    ]
    # #728: OpenSearch (one first-assigned time-bucket report) is the one candidate the
    # source does not run, so it is dropped with no saving; DocumentDB's is the saving.
    assert report["considered_not_recommended"] == [
        {"engine": "opensearch", "absorbed_by": "aurora_mysql"}
    ]
    rc = json.loads((in_use_run.job_dir() / "reality-check" / "output.json").read_text())
    assert [c["saved_cost_estimate"] for c in rc["consolidations"]] == [307.03, 0.0, 0.0]
    texts = _texts(in_use_run)
    for where in ("pdf", "engineering report"):
        assert "Retired by this plan: DocumentDB (runs today), $307.03/mo" in texts[where], where
    assert "Removes $307.03/mo by retiring DocumentDB" in texts["engineering report"]


def test_recording_an_engine_in_use_changes_no_routing(
    in_use_run: PipelineResult, e2e_root: Path
) -> None:
    """engines_in_use only decides what is a saving; every query lands where it did."""
    from tests.e2e.conftest import _run_pipeline_cached

    # The session's own wordpress run (the ``run`` fixture's cache entry).
    plain = _run_pipeline_cached("wordpress", e2e_root / "artifacts", job_id="e2e-word")

    def engines(run: PipelineResult) -> dict[str, str]:
        job = run.job_dir()
        latest = sorted(
            (job / "assignment").glob("v*/assignment.json"), key=lambda p: int(p.parent.name[1:])
        )[-1]
        return {
            qa["query_id"]: qa["assigned_engine"]
            for qa in json.loads(latest.read_text())["query_assignments"]
        }

    assert engines(in_use_run) == engines(plain)


@pytest.fixture(scope="module")
def both_in_use_run(e2e_root: Path) -> PipelineResult:
    """wordpress, declaring DocumentDB and OpenSearch as already running (#1076)."""
    raw = unzip_sample("wordpress", e2e_root / "input-1076")
    text = raw.read_text()
    marker = '"metadata": {'
    assert text.count(marker) == 1
    edited = raw.with_name("wordpress-both-in-use-collection.json")
    edited.write_text(
        text.replace(marker, marker + '"engines_in_use": ["documentdb", "opensearch"], ')
    )
    return run_pipeline_from_file(edited, "wordpress", e2e_root / "artifacts-1076", "both-in-use")


def test_a_declared_and_retired_engine_has_its_own_statement_everywhere(
    both_in_use_run: PipelineResult,
) -> None:
    report = _report(both_in_use_run)
    retired = {r["engine"]: r for r in report["facts"]["retired_in_use_engines"]}
    assert set(retired) == {"documentdb", "opensearch"}
    assert retired["documentdb"]["monthly_cost_usd"] == 307.03
    assert retired["opensearch"]["monthly_cost_usd"] is None
    assert report["considered_not_recommended"] == []
    summary = report["summary"]
    # #1403: the executive summary states the saving in whole dollars; the PDF and the
    # engineering report keep the cents.
    assert report["facts"]["executive_brief"]["retired_saving_whole_usd"] == {"documentdb": 307}
    assert (
        "Retired by this plan: DocumentDB (runs today), $307/mo; OpenSearch (runs today)" in summary
    )
    assert "Other targets evaluated" not in summary
    texts = _texts(both_in_use_run)
    for where in ("pdf", "engineering report"):
        text = texts[where]
        assert "Retired by this plan: DocumentDB (runs today), $307.03/mo" in text, where
        assert (
            "Retired by this plan: OpenSearch (runs today), no saving counted (no queries justified it before the move)"
            in text
        ), where
        assert "DocumentDB provides no unique capabilities" in text, where
        assert "Other targets evaluated" not in text, where
