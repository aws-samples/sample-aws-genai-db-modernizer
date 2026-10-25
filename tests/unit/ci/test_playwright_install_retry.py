"""ci/lib.sh playwright_install_with_retry (#1346): retry with backoff, then a fallback host."""

from __future__ import annotations

import math
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# A fake `uv` that records PLAYWRIGHT_DOWNLOAD_HOST per call and fails the first
# FAKE_FAILS calls with the CDN's certificate error.
FAKE_UV = """#!/usr/bin/env bash
n=$(( $(cat "$FAKE_DIR/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/count"
echo "${PLAYWRIGHT_DOWNLOAD_HOST:-default}" >> "$FAKE_DIR/hosts"
echo "$*" >> "$FAKE_DIR/args"
if [ "$n" -le "$FAKE_FAILS" ]; then echo "ERR_TLS_CERT_ALTNAME_INVALID" >&2; exit 1; fi
"""


def _run(tmp_path: Path, fails: int, **env: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    base = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "FAKE_DIR": str(tmp_path),
        "FAKE_FAILS": str(fails),
        "E2E_INSTALL_BACKOFF": "0",
        "HOME": str(tmp_path),
    }
    base.update(env)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; playwright_install_with_retry "$@"',
            "x",
            str(REPO / "ci/lib.sh"),
            "--with-deps",
            "chromium",
        ],
        env=base,
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    hosts = (tmp_path / "hosts").read_text().split() if (tmp_path / "hosts").exists() else []
    return proc, hosts


def test_success_first_try_does_not_retry(tmp_path):
    proc, hosts = _run(tmp_path, fails=0)
    assert proc.returncode == 0
    assert hosts == ["default"]
    assert (tmp_path / "args").read_text().strip() == "run playwright install --with-deps chromium"


def test_retries_then_succeeds_on_default_host(tmp_path):
    proc, hosts = _run(tmp_path, fails=1)
    assert proc.returncode == 0
    assert hosts == ["default", "default"]


def test_last_attempt_uses_the_fallback_host(tmp_path):
    proc, hosts = _run(tmp_path, fails=2)
    assert proc.returncode == 0
    assert hosts[:2] == ["default", "default"]
    assert hosts[2].startswith("https://playwright.download.prss.microsoft.com/")


def test_fails_after_three_attempts(tmp_path):
    proc, hosts = _run(tmp_path, fails=99)
    assert proc.returncode != 0
    assert len(hosts) == 3
    assert "failed after 3 attempts" in proc.stderr


def test_fallback_host_and_attempts_are_configurable(tmp_path):
    proc, hosts = _run(
        tmp_path,
        fails=99,
        E2E_INSTALL_ATTEMPTS="2",
        PLAYWRIGHT_FALLBACK_HOST="https://mirror.example",
    )
    assert proc.returncode != 0
    assert hosts == ["default", "https://mirror.example"]


def test_explicit_download_host_is_respected_and_tls_is_never_relaxed(tmp_path):
    proc, hosts = _run(tmp_path, fails=99, PLAYWRIGHT_DOWNLOAD_HOST="https://custom.example")
    assert hosts == ["https://custom.example"] * 3
    text = (REPO / "ci/lib.sh").read_text() + (REPO / "ci/e2e.sh").read_text()
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in text
    assert "strict-ssl" not in text


# A fake `uv` that hangs on its first FAKE_HANGS calls (like an apt-get update that
# never returns, #1360) and succeeds afterwards.
HANGING_UV = """#!/usr/bin/env bash
n=$(( $(cat "$FAKE_DIR/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/count"
if [ "$n" -le "$FAKE_HANGS" ]; then
  # Like node spawning sudo apt-get: a child that outlives a kill of its parent.
  if [ -n "$FAKE_IGNORE_TERM" ]; then (trap '' TERM; exec sleep 60) & else sleep 60 & fi
  echo $! >> "$FAKE_DIR/children"
  wait
fi
"""


# A hung attempt is stopped after a limit measured in whole seconds, and that limit
# also covers the attempt's own process start. A fixed 1s limit stopped attempts
# before the fake `uv` had counted itself on a loaded machine (#1529), so the limit is
# a multiple of how long a trivial attempt took in this process, never below 1s.
HANG_LIMIT_RATIO = 4


def _scaled_limit(reference_seconds: float) -> int:
    return max(1, math.ceil(HANG_LIMIT_RATIO * reference_seconds))


def _hang_limit(tmp_path: Path) -> int:
    """The attempt limit for this machine's current load: one successful no-op run, timed."""
    reference = tmp_path / "reference"
    reference.mkdir()
    started = time.monotonic()
    proc, _ = _run(reference, fails=0)
    assert proc.returncode == 0, proc.stderr
    return _scaled_limit(time.monotonic() - started)


def test_the_hang_limit_grows_with_the_measured_start_time():
    assert _scaled_limit(0.05) == 1
    assert _scaled_limit(0.5) == 2
    assert _scaled_limit(2.0) == 8


def _run_hanging(tmp_path: Path, hangs: int, **env: str):
    limit = _hang_limit(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(HANGING_UV)
    uv.chmod(0o755)
    base = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "FAKE_DIR": str(tmp_path),
        "FAKE_HANGS": str(hangs),
        "E2E_INSTALL_BACKOFF": "0",
        "E2E_INSTALL_TIMEOUT": str(limit),
        "HOME": str(tmp_path),
    }
    base.update(env)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; playwright_install_with_retry "$@"',
            "x",
            str(REPO / "ci/lib.sh"),
            "chromium",
        ],
        env=base,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=30 + 4 * limit,
    )
    return proc, int((tmp_path / "count").read_text())


@pytest.mark.passes_on_main  # the flaky test itself: it fails on main only under load
def test_a_hung_attempt_is_stopped_and_retried(tmp_path):
    proc, calls = _run_hanging(tmp_path, hangs=1)
    assert proc.returncode == 0, proc.stderr
    assert calls == 2
    assert re.search(r"still running after \d+s", proc.stderr)


def test_every_attempt_hanging_fails_instead_of_blocking(tmp_path):
    proc, calls = _run_hanging(tmp_path, hangs=99)
    assert proc.returncode != 0
    assert calls == 3
    assert "failed after 3 attempts" in proc.stderr


def _uv_calls_for_shard(tmp_path: Path, shard: str) -> list[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{tmp_path}/calls"\n')
    uv.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "E2E_SHARD": shard,
        "E2E_OUTPUT": str(tmp_path / "out"),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [str(REPO / "ci/e2e.sh")], env=env, capture_output=True, text=True, cwd=REPO
    )
    assert proc.returncode == 0, proc.stderr
    return (tmp_path / "calls").read_text().splitlines()


@pytest.mark.parametrize("shard", ["hashseed", "determinism"])
def test_the_determinism_shards_install_no_browser(tmp_path, shard):
    """Neither determinism suite opens a browser page (#1360), so neither installs one."""
    calls = _uv_calls_for_shard(tmp_path, shard)
    assert not any("playwright install" in c for c in calls)
    assert any(c.startswith("run pytest") for c in calls)


@pytest.mark.passes_on_main
def test_a_browser_shard_still_installs_its_browsers(tmp_path):
    calls = _uv_calls_for_shard(tmp_path, "wordpress")
    assert "run playwright install chromium webkit" in calls


def test_a_hung_attempt_takes_its_child_processes_with_it(tmp_path):
    """The stuck apt-get under playwright holds the dpkg lock, so retries need it gone (#1363)."""
    import os
    import time

    proc, _ = _run_hanging(tmp_path, hangs=1)
    assert proc.returncode == 0, proc.stderr
    (child,) = (tmp_path / "children").read_text().split()
    time.sleep(0.5)
    try:
        os.kill(int(child), 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "the hung attempt's child process is still running"


def test_a_command_ignoring_term_is_killed_after_the_grace_period(tmp_path):
    """The KILL pass must be reachable: a child ignoring TERM is killed once its parent is gone."""
    import os
    import signal

    proc, calls = _run_hanging(tmp_path, hangs=1, FAKE_IGNORE_TERM="1")
    try:
        assert proc.returncode == 0, proc.stderr
        assert calls == 2
    finally:
        for pid in (tmp_path / "children").read_text().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_e2e_ci(tmp_path: Path, launch_ok: bool, shard: str = "wordpress") -> list[str]:
    """Run ci/e2e.sh with CI=true and a fake uv whose browser-launch check passes or fails."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/calls"\n'
        # `uv run python -c <launch script> chromium webkit` is the launch check.
        f'case "$*" in "run python -c"*) exit {0 if launch_ok else 1};; esac\n'
    )
    uv.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CI": "true",
        "E2E_SHARD": shard,
        "E2E_OUTPUT": str(tmp_path / "out"),
        "E2E_INSTALL_BACKOFF": "0",
        "APT_MIRRORS_FILE": str(tmp_path / "apt-mirrors.txt"),
        "APT_CONF_FILE": str(tmp_path / "apt-conf"),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [str(REPO / "ci/e2e.sh")], env=env, capture_output=True, text=True, cwd=REPO
    )
    assert proc.returncode == 0, proc.stderr
    return [
        c.split(" -c ")[0] if " -c " in c else c
        for c in (tmp_path / "calls").read_text().splitlines()
    ]


def test_ci_with_system_libraries_present_makes_no_apt_call(tmp_path):
    calls = _run_e2e_ci(tmp_path, launch_ok=True)
    assert "run playwright install chromium webkit" in calls
    assert not any("install-deps" in c or "--with-deps" in c for c in calls)


def test_ci_installs_system_libraries_only_when_a_browser_cannot_launch(tmp_path):
    calls = _run_e2e_ci(tmp_path, launch_ok=False)
    assert "run playwright install-deps chromium webkit" in calls
    assert not any("--with-deps" in c for c in calls)


def test_the_determinism_shards_run_no_launch_check_either(tmp_path):
    calls = _run_e2e_ci(tmp_path, launch_ok=False, shard="determinism")
    assert not any("playwright" in c or "run python -c" in c for c in calls)


def test_the_apt_fallback_uses_the_archive_mirror_from_the_second_attempt(tmp_path):
    """A runner whose mirror stalls gets apt timeouts, then archive.ubuntu.com (#1360)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n")
    uv = bindir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$(cat "{mirrors}" | head -1 | cut -f1) $*" >> "{tmp_path}/calls"\n'
        'case "$*" in "run python -c"*) exit 1;; esac\n'
        'case "$*" in "run playwright install-deps"*)\n'
        f'  echo x >> "{tmp_path}/deps"; [ "$(wc -l < "{tmp_path}/deps")" -ge 2 ] || exit 1;; esac\n'
    )
    uv.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "E2E_INSTALL_BACKOFF": "0",
        "APT_MIRRORS_FILE": str(mirrors),
        "APT_CONF_FILE": str(tmp_path / "apt-conf"),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; playwright_ensure_deps chromium webkit',
            "x",
            str(REPO / "ci/lib.sh"),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr
    deps = [c for c in (tmp_path / "calls").read_text().splitlines() if "install-deps" in c]
    assert deps[0].startswith("http://azure.archive.ubuntu.com/")
    assert deps[1].startswith("http://archive.ubuntu.com/")
    assert 'Acquire::http::Timeout "30"' in (tmp_path / "apt-conf").read_text()
