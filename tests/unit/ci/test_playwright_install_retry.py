"""ci/lib.sh playwright_install_with_retry (#1346): retry with backoff, then a fallback host."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
