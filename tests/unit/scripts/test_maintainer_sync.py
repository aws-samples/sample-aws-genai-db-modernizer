"""The maintainer sync must compare file contents, not size and mtime (#344)."""

import os
import re
import shutil
import subprocess  # nosec B404 -- runs the system rsync binary with fixed argv
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintainer-sync.sh"


def _rsync_args_block() -> str:
    text = SCRIPT.read_text()
    match = re.search(r"RSYNC_ARGS=\((.*?)\n\)", text, re.S)
    assert match, "RSYNC_ARGS array not found in maintainer-sync.sh"
    return match.group(1)


def test_sync_compares_file_contents(tmp_path):
    assert "--checksum" in _rsync_args_block().split()

    # The suite runs with --fail-on-skip, and not every CI image ships rsync,
    # so the behaviour check runs only where rsync exists instead of skipping.
    if shutil.which("rsync") is None:
        return
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "f.txt").write_text('version = "1.5"\n')
    (dst / "f.txt").write_text('version = "1.4"\n')
    stamp = (src / "f.txt").stat().st_mtime
    os.utime(dst / "f.txt", (stamp, stamp))
    args = [
        a
        for a in _rsync_args_block().split()
        if a.startswith("-") and not a.startswith("--exclude")
    ]
    subprocess.run(  # nosec B603 B607 -- fixed argv, rsync resolved from PATH like the script it tests
        ["rsync", *args, f"{src}/", f"{dst}/"], check=True, capture_output=True
    )
    assert (dst / "f.txt").read_text() == 'version = "1.5"\n'
