# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_assessment.py`` records the session running the job in the cursor (#2215): the
dispatch hooks write a job's ledger only from that session."""

from __future__ import annotations

import inspect

from scripts import run_assessment


def test_the_session_field_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-run-2215")
    assert run_assessment._session_field() == {"session_id": "session-run-2215"}


def test_no_or_a_bad_session_adds_no_field(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert run_assessment._session_field() == {}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not a session/../x")
    assert run_assessment._session_field() == {}


def test_a_new_cursor_and_a_resumed_one_both_record_it() -> None:
    source = inspect.getsource(run_assessment._run)
    assert source.count("_session_field()") == 2  # the new cursor, and the resume's update
    assert "state.update(session)" in source
