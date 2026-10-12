"""INFO-level logs must never carry customer-derived payload content (#143).

``extract_text`` and ``parse_invocation`` previously logged the raw A2A
message / invocation text at INFO, which can be a customer's SQL text, schema
names, or other collection content riding inside the invocation payload. The
same was true of the subagent wrapper's post-work "COMPLETED" log, which
dumped the entire ``work_fn`` summary dict. All three are now DEBUG; INFO
keeps only identifiers and counts.

These tests assert the negative at INFO (no sentinel leaks) and the positive
at DEBUG (the diagnostic detail is still available for troubleshooting, just
not at a level production logging ships by default).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from src.atx_orchestrator.subagents.base import (
    extract_text,
    make_subagent_factory,
    parse_invocation,
)

# A sentinel standing in for customer-derived content (SQL text, schema names,
# upload contents, LLM input/output) that must never reach INFO.
SENTINEL = "SENTINEL_CUSTOMER_PAYLOAD_8f3c2a"


_FORMATTER = logging.Formatter()


def _format(record: logging.LogRecord) -> str:
    """Full record text, including a traceback -- ``logger.exception(...)``
    attaches the raised exception's message there, not in ``getMessage()``."""
    return _FORMATTER.format(record)


def _info_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(_format(r) for r in caplog.records if r.levelno >= logging.INFO)


def _debug_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(_format(r) for r in caplog.records)


class TestExtractTextLogHygiene:
    def test_raw_string_message_not_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            extract_text(f"some non-json customer text containing {SENTINEL}")
        assert SENTINEL not in _info_text(caplog)
        # the detail is still available for troubleshooting at DEBUG
        assert SENTINEL in _debug_text(caplog)

    def test_json_list_parts_message_not_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = json.dumps([{"text": f"customer SQL: SELECT {SENTINEL}", "type": "text"}])
        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            extract_text(payload)
        assert SENTINEL not in _info_text(caplog)
        assert SENTINEL in _debug_text(caplog)

    def test_dict_message_not_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        message = {"parts": [{"text": f"schema dump {SENTINEL}", "kind": "text"}]}
        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            extract_text(message)
        assert SENTINEL not in _info_text(caplog)
        assert SENTINEL in _debug_text(caplog)

    def test_fallback_message_type_not_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither a str nor a dict: the final ``str(msg)`` fallback branch."""

        class _Weird:
            def __repr__(self) -> str:
                return f"<Weird containing {SENTINEL}>"

        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            result = extract_text(_Weird())
        assert SENTINEL in result  # the caller still gets the text
        assert SENTINEL not in _info_text(caplog)
        assert SENTINEL in _debug_text(caplog)


class TestParseInvocationLogHygiene:
    def test_invocation_text_not_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        text = json.dumps(
            {
                "job_id": "job-1",
                "database_name": "demo",
                "input_key": "demo/job-1/uploads/collector-output.json",
                "note": SENTINEL,
            }
        )
        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            parse_invocation(text)
        assert SENTINEL not in _info_text(caplog)
        assert SENTINEL in _debug_text(caplog)


class TestMissingKeysFailurePathLogHygiene:
    """Critical (review round 1): the missing-key ``ValueError`` must name only
    the missing keys, never any value -- including free-form fields riding
    along in the invocation (e.g. a ``note``). ``process_message_async``'s
    broad failure handler logs that exception at ERROR via
    ``logger.exception("Subagent FAILED")``, which also attaches it to the
    traceback text, so a leaked value would reach the logs at ERROR too --
    "INFO or above", not just INFO.
    """

    @pytest.mark.asyncio
    async def test_missing_job_id_with_sentinel_note_not_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def work_fn(params: dict) -> dict:  # pragma: no cover - must not run
            raise AssertionError("work_fn must not run when a required key is missing")

        with patch(
            "agent_builder_sdk.base_subagent.base_subagent.AsyncBaseSubagent.__init__",
            return_value=None,
        ):
            agent = make_subagent_factory("Test prompt", work_fn)(None)
        # job_id is empty (missing); a free-form `note` field carries the sentinel.
        text = json.dumps({"job_id": "", "database_name": "demo", "note": SENTINEL})

        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            with pytest.raises(ValueError, match="job_id"):
                await agent.process_message_async(text)

        assert SENTINEL not in _info_text(caplog)
        # parse_invocation's own DEBUG diagnostic (unrelated to this ValueError)
        # still carries the raw text -- that is expected; only INFO-and-above
        # (the ERROR from logger.exception("Subagent FAILED")) must be clean.
        assert SENTINEL in _debug_text(caplog)

    @pytest.mark.asyncio
    async def test_missing_job_id_message_names_only_job_id(self) -> None:
        """job_id missing, database_name present and valid.

        ``database_name=""`` would be rejected earlier by
        ``_validate_database_name`` inside ``parse_invocation`` itself (a
        pre-existing, already-safe path), never reaching
        ``process_message_async``'s own missing-key check. A valid
        ``database_name`` is required to actually exercise the branch the
        critical fix changed. Against the pre-fix code (``f"Parsed: {params}"``)
        this fails: ``params`` would include ``note=SENTINEL`` since nothing
        here is rejected by the validator, so the old message would contain it.
        """

        def work_fn(params: dict) -> dict:  # pragma: no cover - must not run
            raise AssertionError("work_fn must not run when a required key is missing")

        with patch(
            "agent_builder_sdk.base_subagent.base_subagent.AsyncBaseSubagent.__init__",
            return_value=None,
        ):
            agent = make_subagent_factory("Test prompt", work_fn)(None)
        text = json.dumps({"job_id": "", "database_name": "demo", "note": SENTINEL})

        with pytest.raises(ValueError) as exc_info:
            await agent.process_message_async(text)

        message = str(exc_info.value)
        assert SENTINEL not in message
        missing_clause = message.split("Received keys")[0]
        assert "job_id" in missing_clause
        assert "database_name" not in missing_clause  # only the missing key is named


class TestSubagentCompletedLogHygiene:
    @pytest.mark.asyncio
    async def test_work_fn_summary_not_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A representative end-to-end path: parse -> work_fn -> COMPLETED log.

        The summary a work_fn returns can carry customer-derived strings (e.g. a
        future engine echoing a table or query identifier verbatim). The INFO log
        for a completed task must not include it.
        """

        def work_fn(params: dict) -> dict:
            return {
                "job_id": params["job_id"],
                "database_name": params["database_name"],
                "leaked_if_logged_at_info": SENTINEL,
            }

        with patch(
            "agent_builder_sdk.base_subagent.base_subagent.AsyncBaseSubagent.__init__",
            return_value=None,
        ):
            agent = make_subagent_factory("Test prompt", work_fn)(None)
        text = json.dumps({"job_id": "job-1", "database_name": "demo", "input_key": "k"})

        with caplog.at_level(logging.DEBUG, logger="src.atx_orchestrator.subagents.base"):
            await agent.process_message_async(text)

        assert SENTINEL not in _info_text(caplog)
        assert SENTINEL in _debug_text(caplog)
