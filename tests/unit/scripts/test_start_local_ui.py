"""start_local_ui orchestrates the local API + UI servers for `/modernize --mode ui|both`.

Everything here is stubbed -- no real `uvicorn`, `npm`, or `serve` process is
ever started, and no real HTTP request leaves the process -- so the suite
stays fast and side-effect free. `check_port_available` is exercised for
real against a bare in-process socket, since that is the one primitive
cheap enough to test honestly.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from scripts import start_local_ui


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, port_available: bool = True) -> None:
    monkeypatch.setattr(start_local_ui, "check_port_available", lambda port: port_available)
    monkeypatch.setattr(start_local_ui, "build_ui", lambda serve_bin: None)


def test_ready_when_both_servers_come_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(111))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(222))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: True)

    written: dict[str, Any] = {}
    monkeypatch.setattr(
        start_local_ui,
        "write_pids",
        lambda root, pids: written.update(pids),
    )

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 0
    assert result == {
        "status": "ready",
        "api": start_local_ui.API_URL,
        "ui": start_local_ui.UI_URL,
        "pids": {"api": 111, "serve": 222},
    }
    assert written == {"api": 111, "serve": 222}


def test_timeout_kills_the_children_it_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(111))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(222))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: False)
    monkeypatch.setattr(start_local_ui, "write_pids", lambda root, pids: None)

    killed_with: dict[str, Any] = {}
    monkeypatch.setattr(
        start_local_ui,
        "kill_pids",
        lambda pids: killed_with.update(pids) or dict(pids),
    )
    cleared = []
    monkeypatch.setattr(start_local_ui, "clear_pids", lambda root: cleared.append(root))

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=0.01)

    assert code == 1
    assert result["status"] == "error"
    assert "timeout" in result["reason"]
    assert killed_with == {"api": 111, "serve": 222}
    assert cleared == [tmp_path]


def test_port_in_use_fails_fast_without_building_or_starting_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch, port_available=False)

    build_calls = []
    monkeypatch.setattr(start_local_ui, "build_ui", lambda serve_bin: build_calls.append(1))
    start_calls = []
    monkeypatch.setattr(
        start_local_ui, "start_api", lambda root, log_dir: start_calls.append("api")
    )
    monkeypatch.setattr(
        start_local_ui, "start_serve", lambda serve_bin, log_dir: start_calls.append("serve")
    )

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 1
    assert result["status"] == "error"
    assert "already in use" in result["reason"]
    assert build_calls == []
    assert start_calls == []


def test_npm_e401_surfaces_as_exit_code_2_with_actionable_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(start_local_ui, "check_port_available", lambda port: True)

    def _raise_auth_error(serve_bin: Path) -> None:
        raise start_local_ui.NpmAuthError("npm ERR! code E401")

    monkeypatch.setattr(start_local_ui, "build_ui", _raise_auth_error)

    result, code = start_local_ui.run_start(tmp_path, rebuild=True, timeout=30)

    assert code == 2
    assert result == {
        "status": "error",
        "reason": "npm registry auth (E401) — refresh the npm token",
    }


def test_run_npm_raises_npm_auth_error_on_e401_in_combined_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "npm ERR! code E401\nnpm ERR! Unable to authenticate\n"

    monkeypatch.setattr(start_local_ui.subprocess, "run", lambda *a, **k: FakeCompletedProcess())

    with pytest.raises(start_local_ui.NpmAuthError):
        start_local_ui._run_npm(["npm", "ci"], cwd=tmp_path, env={})


def test_run_npm_raises_build_error_on_other_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeCompletedProcess:
        returncode = 1
        stdout = "something else went wrong"
        stderr = ""

    monkeypatch.setattr(start_local_ui.subprocess, "run", lambda *a, **k: FakeCompletedProcess())

    with pytest.raises(start_local_ui.BuildError):
        start_local_ui._run_npm(["npm", "run", "build"], cwd=tmp_path, env={})


def test_run_npm_succeeds_silently_on_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeCompletedProcess:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(start_local_ui.subprocess, "run", lambda *a, **k: FakeCompletedProcess())

    start_local_ui._run_npm(["npm", "run", "build"], cwd=tmp_path, env={})  # must not raise


def test_stop_kills_pids_from_file_and_removes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_ui_dir = tmp_path / ".local-ui"
    local_ui_dir.mkdir()
    (local_ui_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))

    killed = []
    monkeypatch.setattr(
        start_local_ui.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    result, code = start_local_ui.run_stop(tmp_path)

    assert code == 0
    assert result == {"status": "stopped", "pids": {"api": 111, "serve": 222}}
    assert set(killed) == {
        (111, start_local_ui.signal.SIGTERM),
        (222, start_local_ui.signal.SIGTERM),
    }
    assert not (local_ui_dir / "pids.json").exists()


def test_stop_with_no_prior_run_is_a_no_op(tmp_path: Path) -> None:
    result, code = start_local_ui.run_stop(tmp_path)

    assert code == 0
    assert result == {"status": "stopped", "pids": {}}


def test_kill_pids_skips_processes_that_are_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def _killpg(pid: int, sig: int) -> None:
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(start_local_ui.os, "killpg", _killpg)

    killed = start_local_ui.kill_pids({"api": 111, "serve": 222})

    assert killed == {"api": 111}


def test_check_port_available_detects_a_real_listening_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert start_local_ui.check_port_available(port) is False
    finally:
        sock.close()

    assert start_local_ui.check_port_available(port) is True


def test_wait_for_ready_true_when_all_endpoints_respond(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_local_ui, "_check_http_ok", lambda url: True)

    assert start_local_ui.wait_for_ready(timeout=5) is True


def test_wait_for_ready_false_when_an_endpoint_never_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start_local_ui, "_check_http_ok", lambda url: False)
    monkeypatch.setattr(start_local_ui, "POLL_INTERVAL_SECONDS", 0.01)

    assert start_local_ui.wait_for_ready(timeout=0.02) is False


def test_parse_args_defaults_and_stop_flag() -> None:
    args = start_local_ui.parse_args([])
    assert args.artifact_root == "./artifacts"
    assert args.rebuild is False
    assert args.timeout == 180
    assert args.stop is False

    stop_args = start_local_ui.parse_args(["--stop"])
    assert stop_args.stop is True


def test_run_dispatches_to_stop_when_flag_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = start_local_ui.parse_args(["--artifact-root", str(tmp_path), "--stop"])

    result, code = start_local_ui.run(args)

    assert code == 0
    assert result == {"status": "stopped", "pids": {}}


def test_run_refuses_artifact_root_outside_repo_under_ci_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    args = start_local_ui.parse_args(["--artifact-root", str(tmp_path), "--stop"])

    result, code = start_local_ui.run(args)

    assert code == 1
    assert result["status"] == "error"
    assert "--artifact-root" in result["reason"]
