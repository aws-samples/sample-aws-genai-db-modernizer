# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dev listeners stay on loopback: the UI `serve` script and every compose port publish."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *patterns], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [REPO / name for name in out]


def test_the_ui_serve_script_binds_loopback() -> None:
    scripts = json.loads((REPO / "src/ui/package.json").read_text())["scripts"]
    assert "tcp://127.0.0.1:" in scripts["serve"]


def test_no_compose_file_publishes_a_bare_host_port() -> None:
    files = _tracked(
        "*docker-compose*.yml", "*docker-compose*.yaml", "*compose.yml", "*compose.yaml"
    )
    assert files, "expected at least one compose file in the repo"
    bare: list[str] = []
    for path in files:
        doc = yaml.safe_load(path.read_text()) or {}
        for name, service in (doc.get("services") or {}).items():
            for port in (service or {}).get("ports") or []:
                if isinstance(port, dict):
                    loopback = str(port.get("host_ip", "")) in ("127.0.0.1", "::1")
                else:
                    loopback = str(port).startswith(("127.0.0.1:", "[::1]:"))
                if not loopback:
                    bare.append(f"{path.relative_to(REPO)}: {name}: {port}")
    assert not bare, "publish on 127.0.0.1 (host side), not all interfaces:\n" + "\n".join(bare)
