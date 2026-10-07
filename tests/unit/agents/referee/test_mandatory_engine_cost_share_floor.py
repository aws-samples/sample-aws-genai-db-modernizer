"""A mandatory engine with a tiny workload share and a high fixed cost is not
exempt from the cost review forever (#167).

A signal override makes an engine "mandatory" through the reality check's
Pass 0-2, which protects its queries even when it carries almost none of the
workload. #326 added a dedicated justification floor for OpenSearch; this is
the same idea generalized to any other mandatory engine, judged on cost share
alone: an engine whose fixed monthly cost (``ENGINE_BASE_COST``) is high and
whose share of in-scope queries is tiny is not exempt just because a signal
made it mandatory.

This module also guards against a regression (#406): the justification
floor used to build its consolidation reason with a bare internal issue tag
baked in (e.g. ``"... justification floor (#167) ..."``), and
``reconcile_consolidations`` only rewrites a record's ``reason`` when its
``query_count``/``action``/``retained`` queries changed, so the tag could
survive verbatim into ``reality-check/output.json`` and from there into the
deliverables and the ``/modernize`` approval gate. Customer-facing text must
never carry an internal issue reference.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.reality_check import run_reality_check

REPO = Path(__file__).resolve().parents[4]

# A bare internal issue reference, e.g. "#167" or "#326". Not anchored to
# "(...)" since the bug this guards against is any customer-facing text
# carrying the tag, parenthesized or not.
_ISSUE_TAG_RE = re.compile(r"#\d+")


def _find_issue_tags(obj: Any, path: str = "$") -> list[tuple[str, str]]:
    """Every (path, value) in ``obj`` whose string value matches ``_ISSUE_TAG_RE``.

    Walks the full JSON tree (dicts, lists, strings) so a tag nested anywhere
    -- a consolidation reason, a risk description, a recommendation, a
    migration-wave rationale -- is caught, not just the one field #406 was
    filed against.
    """
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_find_issue_tags(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(_find_issue_tags(value, f"{path}[{i}]"))
    elif isinstance(obj, str) and _ISSUE_TAG_RE.search(obj):
        found.append((path, obj))
    return found


def _make_collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }


class TestMandatoryEngineCostShareFloor:
    def test_tiny_share_high_cost_mandatory_engine_is_dropped(self):
        """A hypothetical mandatory engine (not OpenSearch) serving 1/40 queries."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(39)
            ]
            + [
                {
                    "query_id": "doc1",
                    "assigned_engine": "documentdb",
                    "assignment_reason": "signal override: nested_document → documentdb",
                    "signal_override": "nested_document",
                }
            ],
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
        }
        collector = _make_collector([f"dq{i}" for i in range(39)] + ["doc1"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["doc1"] == "dynamodb"
        moved = next(c for c in result["consolidations"] if c["from_engine"] == "documentdb")
        # The reason must explain the move but never leak the internal issue
        # tag the floor is implemented under (#406): accepting either made the
        # old assertion blind to exactly the regression it should catch.
        assert "justification floor" in moved["reason"]
        assert "#167" not in moved["reason"]
        assert not _find_issue_tags(moved["reason"])


def _collection_path(sample: str, tmp_path: Path) -> Path:
    zip_path = REPO / "docs" / "examples" / sample / f"{sample}.zip"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp_path / "input" / sample)
    return tmp_path / "input" / sample / f"{sample}-collection.json"


def _run_deterministic_pipeline(
    sample: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, str]:
    """Run collect through synthesis on ``sample`` with no model calls.

    Mirrors the real path an actual job takes: ``run_assessment.main()``
    drives ``phase_reality_check`` (which calls ``run_reality_check_handler``,
    in turn ``run_reality_check_deterministic`` then ``write_reality_check_result``
    -- the two functions #406 named) and, with ``--llm-mode none``, continues
    through synthesis instead of stopping (schema design is the only phase
    ``none`` mode skips).

    Returns (job_dir, db, job_id).
    """
    import sys

    from scripts import run_assessment

    collection = _collection_path(sample, tmp_path)
    artifacts = tmp_path / "artifacts" / sample
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            str(collection),
            "--llm-mode",
            "none",
            "--all",
            "-y",
            "--artifact-root",
            str(artifacts),
        ],
    )
    run_assessment.main()

    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    job_id = state["job_id"]
    db = state["database_name"]
    return artifacts / db / job_id, db, job_id


# Fields known to be internal-only (never shown to a customer) that this
# sweep intentionally does not need to check for an issue tag, because they
# cannot carry customer-facing prose at all. None exist today in
# reality-check/output.json or report.json -- every string field in both
# contracts (RealityCheckOutputContract, SynthesisOutputContract) is
# documented as, or reachable from, customer-facing text (reasons, risks,
# recommendations, rationale, summaries). This set is here so a future
# internal-only field can be added deliberately, with a comment saying why,
# rather than by silently weakening the sweep below.
INTERNAL_ONLY_KEYS: frozenset[str] = frozenset()


class TestNoIssueTagsReachCustomerFacingArtifacts:
    """Regression test for #406: an internal issue reference (``#167``,
    ``#326``, ...) must never reach ``reality-check/output.json`` or
    ``report.json`` -- the artifacts deliverables and the ``/modernize``
    approval gate read from. ``scripts/run_assessment.py``'s
    ``_scrub_issue_refs`` (added for #329) still scrubs the approval gate's
    own stdout defensively, but that is belt and braces: the fix is at the
    source, in how ``src/agents/referee/reality_check.py`` builds the reason
    text in the first place.
    """

    @pytest.mark.parametrize("sample", ["wordpress", "discourse"])
    def test_no_bare_issue_tag_in_reality_check_or_synthesis_output(
        self, sample: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_dir, _db, _job_id = _run_deterministic_pipeline(sample, tmp_path, monkeypatch)

        reality_check_path = job_dir / "reality-check" / "output.json"
        assert reality_check_path.is_file(), f"{reality_check_path} missing"
        reality_check = json.loads(reality_check_path.read_text())

        (report_path,) = job_dir.glob("synthesis/v*/report.json")
        report = json.loads(report_path.read_text())

        for artifact_path, artifact in (
            (reality_check_path, reality_check),
            (report_path, report),
        ):
            tags = [
                (path, value)
                for path, value in _find_issue_tags(artifact)
                if path.rsplit(".", 1)[-1] not in INTERNAL_ONLY_KEYS
            ]
            assert tags == [], f"{artifact_path}: internal issue tag(s) found: {tags}"
