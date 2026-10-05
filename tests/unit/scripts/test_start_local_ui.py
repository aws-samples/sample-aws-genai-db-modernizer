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


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state" / ".local-ui"
    monkeypatch.setattr(start_local_ui, "STATE_DIR", state_dir)
    return state_dir


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, port_available: bool = True) -> None:
    monkeypatch.setattr(start_local_ui, "check_port_available", lambda port: port_available)
    monkeypatch.setattr(start_local_ui, "build_ui", lambda serve_bin: None)


def _all_processes_match(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = {111: "python -m uvicorn src.api.main:app", 222: "node .bin/serve -s build"}
    monkeypatch.setattr(start_local_ui, "process_command", lambda pid: commands.get(pid))


def test_ready_when_both_servers_come_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(111))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(222))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: True)

    written: dict[str, Any] = {}
    monkeypatch.setattr(start_local_ui, "write_pids", lambda pids: written.update(pids))

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 0
    assert result == {
        "status": "ready",
        "api": start_local_ui.API_URL,
        "ui": start_local_ui.UI_URL,
        "pids": {"api": 111, "serve": 222},
    }
    assert written == {"api": 111, "serve": 222}


def test_ready_reuses_already_running_servers_from_a_previous_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_state_dir: Path
) -> None:
    # Issue #346: /modernize never stops these servers, so the next default
    # run must recognize its own still-healthy servers and reuse them rather
    # than failing with "port already in use" or restarting them.
    _isolated_state_dir.mkdir(parents=True)
    (_isolated_state_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))
    _all_processes_match(monkeypatch)
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: True)

    def _must_not_be_called(name: str) -> Any:
        def _fail(*a: Any, **k: Any) -> None:
            pytest.fail(f"{name} should not be called")

        return _fail

    for name in ("check_port_available", "build_ui", "start_api", "start_serve", "write_pids"):
        monkeypatch.setattr(start_local_ui, name, _must_not_be_called(name))

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 0
    assert result == {
        "status": "ready",
        "api": start_local_ui.API_URL,
        "ui": start_local_ui.UI_URL,
        "pids": {"api": 111, "serve": 222},
    }


def test_rebuild_flag_skips_reuse_and_starts_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_state_dir: Path
) -> None:
    _isolated_state_dir.mkdir(parents=True)
    (_isolated_state_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))
    _all_processes_match(monkeypatch)
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(333))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(444))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: True)
    monkeypatch.setattr(start_local_ui, "write_pids", lambda pids: None)

    result, code = start_local_ui.run_start(tmp_path, rebuild=True, timeout=30)

    assert code == 0
    assert result["pids"] == {"api": 333, "serve": 444}


def test_recorded_pid_belonging_to_something_else_is_not_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_state_dir: Path
) -> None:
    _isolated_state_dir.mkdir(parents=True)
    (_isolated_state_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))
    monkeypatch.setattr(start_local_ui, "process_command", lambda pid: "/usr/sbin/sshd -D")
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(333))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(444))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: True)
    monkeypatch.setattr(start_local_ui, "write_pids", lambda pids: None)

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 0
    assert result["pids"] == {"api": 333, "serve": 444}


def test_recorded_servers_alive_but_unhealthy_do_not_short_circuit_the_port_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_state_dir: Path
) -> None:
    _isolated_state_dir.mkdir(parents=True)
    (_isolated_state_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))
    _all_processes_match(monkeypatch)
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: False)
    _patch_common(monkeypatch, port_available=False)

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=30)

    assert code == 1
    assert "already in use" in result["reason"]


def test_timeout_kills_the_children_it_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(start_local_ui, "start_api", lambda root, log_dir: FakeProc(111))
    monkeypatch.setattr(start_local_ui, "start_serve", lambda serve_bin, log_dir: FakeProc(222))
    monkeypatch.setattr(start_local_ui, "wait_for_ready", lambda timeout: False)
    monkeypatch.setattr(start_local_ui, "write_pids", lambda pids: None)

    killed_with: dict[str, Any] = {}
    monkeypatch.setattr(
        start_local_ui,
        "kill_pids",
        lambda pids: killed_with.update(pids) or dict(pids),
    )
    cleared = []
    monkeypatch.setattr(start_local_ui, "clear_pids", lambda: cleared.append(True))

    result, code = start_local_ui.run_start(tmp_path, rebuild=False, timeout=0.01)

    assert code == 1
    assert result["status"] == "error"
    assert "timeout" in result["reason"]
    assert killed_with == {"api": 111, "serve": 222}
    assert cleared == [True]


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
    monkeypatch: pytest.MonkeyPatch, _isolated_state_dir: Path
) -> None:
    local_ui_dir = _isolated_state_dir
    local_ui_dir.mkdir(parents=True)
    (local_ui_dir / "pids.json").write_text(json.dumps({"api": 111, "serve": 222}))
    _all_processes_match(monkeypatch)

    killed = []
    monkeypatch.setattr(
        start_local_ui.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    result, code = start_local_ui.run_stop()

    assert code == 0
    assert result == {"status": "stopped", "pids": {"api": 111, "serve": 222}}
    assert set(killed) == {
        (111, start_local_ui.signal.SIGTERM),
        (222, start_local_ui.signal.SIGTERM),
    }
    assert not (local_ui_dir / "pids.json").exists()


def test_stop_with_no_prior_run_is_a_no_op() -> None:
    result, code = start_local_ui.run_stop()

    assert code == 0
    assert result == {"status": "stopped", "pids": {}}


def test_kill_pids_skips_processes_that_are_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def _killpg(pid: int, sig: int) -> None:
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(start_local_ui.os, "killpg", _killpg)
    _all_processes_match(monkeypatch)

    killed = start_local_ui.kill_pids({"api": 111, "serve": 222})

    assert killed == {"api": 111}


def test_kill_pids_never_signals_a_reused_pid_running_something_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = {111: "/usr/sbin/sshd -D", 222: None, 333: "python -m uvicorn x"}
    monkeypatch.setattr(start_local_ui, "process_command", lambda pid: commands.get(pid))
    signalled = []
    monkeypatch.setattr(start_local_ui.os, "killpg", lambda pid, sig: signalled.append(pid))

    killed = start_local_ui.kill_pids({"api": 111, "serve": 222, "other": 333})

    assert killed == {}
    assert signalled == []


def test_process_command_reads_a_real_process_and_none_for_a_dead_pid() -> None:
    import os

    assert start_local_ui.process_command(os.getpid())
    assert start_local_ui.process_command(2**22 + 12345) is None


def test_check_port_available_detects_a_real_ipv6_listening_socket() -> None:
    # ci/test.sh runs with --fail-on-skip, so a host without IPv6 loopback
    # (some containers) passes trivially instead of skipping.
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return
    try:
        sock.bind(("::1", 0))
    except OSError:
        sock.close()
        return
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert start_local_ui.check_port_available(port) is False
    finally:
        sock.close()


def test_run_npm_turns_timeout_and_missing_binary_into_build_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _timeout(*a: Any, **k: Any) -> None:
        raise start_local_ui.subprocess.TimeoutExpired(cmd="npm", timeout=600)

    monkeypatch.setattr(start_local_ui.subprocess, "run", _timeout)
    with pytest.raises(start_local_ui.BuildError, match="timed out"):
        start_local_ui._run_npm(["npm", "ci"], cwd=tmp_path, env={})

    def _missing(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("npm")

    monkeypatch.setattr(start_local_ui.subprocess, "run", _missing)
    with pytest.raises(start_local_ui.BuildError, match="could not be started"):
        start_local_ui._run_npm(["npm", "ci"], cwd=tmp_path, env={})


def test_build_failure_from_missing_npm_is_a_json_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(start_local_ui, "check_port_available", lambda port: True)

    def _missing(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("npm")

    monkeypatch.setattr(start_local_ui.subprocess, "run", _missing)

    result, code = start_local_ui.run_start(tmp_path, rebuild=True, timeout=30)

    assert code == 1
    assert result["status"] == "error"
    assert "could not be started" in result["reason"]


def test_serve_listens_on_ipv4_loopback_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def _popen(argv: list[str], **kwargs: Any) -> FakeProc:
        seen["argv"] = argv
        return FakeProc(1)

    monkeypatch.setattr(start_local_ui.subprocess, "Popen", _popen)
    start_local_ui.start_serve(tmp_path / "serve", tmp_path)

    assert seen["argv"][seen["argv"].index("-l") + 1] == "tcp://127.0.0.1:3000"


def test_wait_for_ready_probes_the_loopback_address(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def _ok(url: str) -> bool:
        urls.append(url)
        return True

    monkeypatch.setattr(start_local_ui, "_check_http_ok", _ok)

    assert start_local_ui.wait_for_ready(timeout=5) is True
    assert urls and all(u.startswith("http://127.0.0.1:") for u in urls)


def test_pid_and_log_state_dir_is_gitignored() -> None:
    gitignore = (start_local_ui.REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".local-ui/" in gitignore


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
