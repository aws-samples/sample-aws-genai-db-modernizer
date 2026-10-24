"""Unit tests for the orchestrator publish wiring (Increment 2).

``_publish_synthesis_deliverables`` (in ``tools``) is tested with a fake store
and a patched ``publish`` to confirm: the three deliverables (the engineering
report, the assessment data and the executive report) are written to S3 as the job's
``deliverables/`` folder under their stable names, never next to report.json
(#987); all three are published as CUSTOMER_OUTPUT each carrying an explicit
download filename; and the whole step is non-fatal when the report cannot be read.

``_FakeStore`` implements ``write_bytes`` deliberately. While it did not, the
PDF block raised ``AttributeError`` straight into its own ``except`` and that
deliverable was never exercised at all.

Exercised against the same committed fixture used by the renderer tests (the real
report from the deployed ``v2-e2e-09`` run) — see
``tests/unit/report/test_report_renderers.py`` for the pure-renderer coverage.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.atx_orchestrator.tools import run_synthesis_via_a2a
from src.report import deliverables as report_deliverables
from src.report.deliverables import DeliverableSet

FIXTURE = Path(__file__).resolve().parents[1] / "report" / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


class _FakeStore:
    def __init__(self, data: dict) -> None:
        self.data: dict[str, dict] = dict(data)
        self.text_writes: dict[str, tuple[str, str]] = {}
        self.byte_writes: dict[str, bytes] = {}

    def read_json(self, path: str) -> dict:
        return self.data[path]

    def exists(self, path: str) -> bool:
        return path in self.data

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self.text_writes[path] = (content, content_type)

    # The executive summary writes binary. Without this the whole block raises
    # AttributeError into its own ``except`` and the fifth deliverable silently
    # never renders -- which is exactly how it went untested before.
    def write_bytes(self, path: str, content: bytes, content_type: str = "") -> None:
        self.byte_writes[path] = content


class _JsonOnlyStore(_FakeStore):
    """Models the ATX artifact store: JSON-only, raises on text/bytes writes.

    Used to prove that when the durable "system of record" copy cannot be written
    (ATX backend), the synthesis publish path still reaches artifacts.publish with
    every deliverable — the customer must still get their downloadable reports.
    """

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        raise NotImplementedError("ATX artifact store is JSON-only")

    def write_bytes(self, path: str, content: bytes, content_type: str = "") -> None:
        raise NotImplementedError("ATX artifact store is JSON-only")


class TestSynthesisDeliverables:
    KEY = "discourse/job-x/synthesis/v1/report.json"

    def test_writes_the_deliverables_folder_and_publishes_three(self, report: dict) -> None:
        payload = {"response": {"report_artifact": self.KEY, "engines_ranked": 5}}
        store = _FakeStore({self.KEY: report})
        captured: dict = {}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.artifacts.publish",
                side_effect=lambda items: captured.update({"items": items}) or {},
            ),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")

        # payload flows through unchanged
        assert json.loads(out)["response"]["report_artifact"] == self.KEY

        # provenance stems are date-stamped; derive the day rather than freezing time
        day = datetime.now(UTC).strftime("%Y%m%d")

        # The durable copies are the job's deliverables folder, under the stable
        # names a local run uses; nothing lands next to report.json (#987).
        assert sorted(store.text_writes) == [
            "discourse/job-x/deliverables/assessment-data.json",
            "discourse/job-x/deliverables/engineering-report-detailed.md",
            "discourse/job-x/deliverables/engineering-report.md",
        ]
        assert sorted(store.byte_writes) == ["discourse/job-x/deliverables/executive-report.pdf"]
        assert (
            store.byte_writes["discourse/job-x/deliverables/executive-report.pdf"][:5] == b"%PDF-"
        )
        assert not [k for k in [*store.text_writes, *store.byte_writes] if "/synthesis/" in k]

        # three published, in order, all CUSTOMER_OUTPUT
        items = captured["items"]
        assert [it[1] for it in items] == ["MARKDOWN", "JSON", "PDF"]
        assert [it[2] for it in items] == [
            "Engineering Report: discourse",
            "Assessment Data (raw): discourse",
            "Executive Report: discourse (v1)",
        ]
        assert {it[3] for it in items} == {"CUSTOMER_OUTPUT"}

        # Every item carries an explicit download filename (5th element). Without it
        # publish() cannot set fileMetadata.path and the customer's download is named
        # after the artifact UUID -- a regression no other assertion here would catch.
        assert all(len(it) == 5 for it in items)
        assert [it[4] for it in items] == [
            f"discourse_engineering-report_job-x_{day}.md",
            f"discourse_assessment-data_job-x_{day}.json",
            "executive-report.pdf",
        ]

    def test_publishes_all_three_on_json_only_store(self, report: dict) -> None:
        """On the JSON-only ATX backend the durable store copies raise
        NotImplementedError, but publish() must still deliver all three --
        publish is the actual customer-facing delivery, the store copy is only a
        system-of-record duplicate that backend has no place for."""
        payload = {"response": {"report_artifact": self.KEY, "engines_ranked": 5}}
        store = _JsonOnlyStore({self.KEY: report})
        captured: dict = {}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.artifacts.publish",
                side_effect=lambda items: captured.update({"items": items}) or {},
            ),
        ):
            run_synthesis_via_a2a("job-x", "discourse")

        # No durable copies landed (the store rejected them) ...
        assert store.text_writes == {}
        assert store.byte_writes == {}
        # ... but all three deliverables were still published to the customer.
        items = captured["items"]
        assert [it[1] for it in items] == ["MARKDOWN", "JSON", "PDF"]
        assert {it[3] for it in items} == {"CUSTOMER_OUTPUT"}

    def test_non_fatal_when_report_unreadable(self) -> None:
        payload = {"response": {"report_artifact": "missing/key.json"}}
        store = _FakeStore({})  # read_json raises KeyError
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")
        assert json.loads(out)["response"]["report_artifact"] == "missing/key.json"

    def test_non_fatal_when_no_report_artifact(self) -> None:
        payload = {"response": {"engines_ranked": 5}}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store"),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")
        # No report_artifact -> deliverables publishing is skipped; the payload
        # flows through unchanged. (A store is constructed once for version
        # resolution, which is best-effort and never fatal.)
        assert json.loads(out) == payload

    def test_pins_the_assignment_version(self, report: dict) -> None:
        """render_deliverables is called with the assignment_version resolved from
        the payload, and with no graph: no remaining deliverable reads it (#987)."""
        payload = {"response": {"report_artifact": self.KEY, "engines_ranked": 5}}
        store = _FakeStore({self.KEY: report})
        mock_render = Mock(return_value=DeliverableSet())
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.report.deliverables.render_deliverables", mock_render),
            patch("src.atx_orchestrator.runtime.artifacts.publish", return_value={}),
        ):
            run_synthesis_via_a2a("job-x", "discourse")

        assert mock_render.called
        _, kwargs = mock_render.call_args
        assert "graph_fetcher" not in kwargs
        assert kwargs["assignment_version"] == 1

    def test_logs_failed_artifact_names_without_a_count(
        self, report: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When render_deliverables reports failures, the ATX-level summary
        names them so a round that shipped fewer deliverables is visible from
        this call site (#146) -- as a plain list, not an "N of M" count that
        would need the reader to know how many deliverables are even possible."""
        payload = {"response": {"report_artifact": self.KEY, "engines_ranked": 5}}
        store = _FakeStore({self.KEY: report})
        failed_set = DeliverableSet(
            errors=[
                "executive-report: RuntimeError: boom",
                "engineering-report: ValueError: bad report",
            ]
        )
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.report.deliverables.render_deliverables", return_value=failed_set),
            patch("src.atx_orchestrator.runtime.artifacts.publish", return_value={}),
            caplog.at_level(logging.WARNING, logger="src.atx_orchestrator.tools"),
        ):
            run_synthesis_via_a2a("job-x", "discourse")

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "executive-report" in m and "engineering-report" in m and "failed to render" in m
            for m in messages
        )
        assert not any(re.search(r"\b\d+ of \d+\b", m) for m in messages)

    def test_re_entry_regenerates_distinct_deliverables_per_version(self, report: dict) -> None:
        """A re-entry round (new assignment version, ADR-029) must render and
        publish a fresh executive summary reflecting the new round's content,
        not reuse or silently skip the prior round's file (#146). v2 gets a
        changed report (a different overall risk level, which the exec summary
        deck renders), so this also proves the PDF bytes actually differ, not
        just the path and filename they land at."""
        key_v1 = self.KEY
        key_v2 = "discourse/job-x/synthesis/v2/report.json"
        report_v2 = copy.deepcopy(report)
        report_v2["risk_assessment"]["overall_risk_level"] = "CRITICAL"
        store = _FakeStore({key_v1: report, key_v2: report_v2})
        publishes: list = []
        render_calls: list = []
        real_render = report_deliverables.render_deliverables

        def _record_publish(items: list) -> dict:
            publishes.append(items)
            return {}

        def _spy_render(*args, **kwargs):
            render_calls.append(kwargs)
            return real_render(*args, **kwargs)

        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.report.deliverables.render_deliverables", side_effect=_spy_render),
            patch(
                "src.atx_orchestrator.runtime.artifacts.publish",
                side_effect=_record_publish,
            ),
        ):
            for key, version in ((key_v1, 1), (key_v2, 2)):
                payload = {
                    "response": {
                        "report_artifact": key,
                        "assignment_version": version,
                        "engines_ranked": 5,
                    }
                }
                with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload):
                    run_synthesis_via_a2a("job-x", "discourse")

        # render_deliverables is called with the version each round actually
        # reported, not a value stuck at the first round's.
        assert [c["assignment_version"] for c in render_calls] == [1, 2]

        # Each re-entry round published its own full set of three deliverables --
        # the second round is not skipped because the first already ran.
        assert len(publishes) == 2
        for items in publishes:
            assert [it[1] for it in items] == ["MARKDOWN", "JSON", "PDF"]

        # The two rounds' published PDFs differ in content, not just in label.
        pdf_v1, pdf_v2 = (items[-1][0] for items in publishes)
        assert pdf_v1 != pdf_v2

        # The durable copy is the deliverables folder, which holds the latest round:
        # v2 overwrote v1 there, and no copy was left under either synthesis version.
        assert sorted(k for k in store.byte_writes) == [
            "discourse/job-x/deliverables/executive-report.pdf"
        ]
        assert store.byte_writes["discourse/job-x/deliverables/executive-report.pdf"] == pdf_v2
