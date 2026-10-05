"""The maintainer sync must compare file contents, not size and mtime (#344)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintainer-sync.sh"


def _rsync_args_block() -> str:
    text = SCRIPT.read_text()
    match = re.search(r"RSYNC_ARGS=\((.*?)\n\)", text, re.S)
    assert match, "RSYNC_ARGS array not found in maintainer-sync.sh"
    return match.group(1)


def test_sync_compares_file_contents():
    assert "--checksum" in _rsync_args_block().split()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_checksum_copies_a_same_size_edit_with_the_same_mtime(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "f.txt").write_text('version = "1.5"\n')
    (dst / "f.txt").write_text('version = "1.4"\n')
    stamp = (src / "f.txt").stat().st_mtime
    os.utime(dst / "f.txt", (stamp, stamp))
    args = [a.strip() for a in _rsync_args_block().split() if a.strip().startswith("-")]
    args = [a for a in args if not a.startswith("--exclude")]
    subprocess.run(["rsync", *args, f"{src}/", f"{dst}/"], check=True, capture_output=True)
    assert (dst / "f.txt").read_text() == 'version = "1.5"\n'
