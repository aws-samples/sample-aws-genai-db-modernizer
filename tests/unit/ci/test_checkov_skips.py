# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Checkov finding in the shipped templates is fixed or skipped with a reason (#1724).

A skip lives next to the resource it covers, names the check and says why the
check does not apply. The list below is the reviewed set: a new skip, or a
global ``--skip-check`` in CI that would hide a finding everywhere, fails here
until someone decides it is justified.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cfnlint.decode import decode_str

REPO_ROOT = Path(__file__).resolve().parents[3]

PLUGIN_BUCKET_STACK = (
    REPO_ROOT
    / "infrastructure"
    / "cloudformation"
    / "data-movement"
    / "debezium-plugin-bucket.yaml"
)

TEMPLATES = [
    REPO_ROOT / "infrastructure" / "cloudformation" / "automation.yaml",
    REPO_ROOT / "infrastructure" / "cloudformation" / "bedrock-guardrail.yaml",
    PLUGIN_BUCKET_STACK,
    REPO_ROOT / "pipeline" / "iam-roles.yaml",
    # The rendered data-movement examples CI scans too.
    *sorted(
        (REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "examples").glob(
            "*.yaml"
        )
    ),
]
#: CI still scans the example CI runner template, which is being removed
#: (#1723); its skips are not part of the reviewed set.

DOCKERFILES = [
    REPO_ROOT / "infrastructure" / "docker" / "agent-load-test" / "Dockerfile",
    REPO_ROOT / "src" / "atx_orchestrator" / "Dockerfile.atx",
]

#: (file, resource, check id): the reviewed skips.
EXPECTED_SKIPS = {
    ("automation.yaml", "AutomationInstance", "CKV_AWS_46"),
    ("debezium-plugin-bucket.yaml", "AccessLogBucket", "CKV_AWS_18"),
    ("iam-roles.yaml", "AgentCoreExecutionRole", "CKV_AWS_111"),
    ("Dockerfile", "-", "CKV_DOCKER_2"),
}

#: A reason must say more than the check's name does.
MIN_REASON_WORDS = 8

_INLINE = re.compile(r"#\s*checkov:skip=(CKV2?_[A-Z0-9_]+)\s*:?(.*)")


def _template_skips(path: Path) -> list[tuple[str, str, str]]:
    template, matches = decode_str(path.read_text(encoding="utf-8"))
    assert not matches, matches
    assert template is not None
    out: list[tuple[str, str, str]] = []
    for name, resource in template.get("Resources", {}).items():
        metadata: dict[str, Any] = resource.get("Metadata") or {}
        for skip in (metadata.get("checkov") or {}).get("skip", []):
            out.append((name, skip["id"], skip.get("comment", "")))
    # Inline comments directly under a resource (pipeline/iam-roles.yaml style).
    resource_name = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = re.match(r"^  ([A-Za-z0-9]+):\s*$", line)
        if header:
            resource_name = header.group(1)
        inline = _INLINE.search(line)
        if inline and resource_name:
            out.append((resource_name, inline.group(1), inline.group(2).strip()))
    return out


def _all_skips() -> list[tuple[str, str, str, str]]:
    out = [(p.name, *skip) for p in TEMPLATES for skip in _template_skips(p)]
    for path in DOCKERFILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            inline = _INLINE.search(line)
            if inline:
                out.append((path.name, "-", inline.group(1), inline.group(2).strip()))
    return out


def test_the_skips_are_the_reviewed_set():
    found = {(f, r, c) for f, r, c, _ in _all_skips()}
    assert found == EXPECTED_SKIPS


@pytest.mark.parametrize("skip", _all_skips(), ids=lambda s: f"{s[0]}:{s[1]}:{s[2]}")
def test_every_skip_states_why(skip):
    _, _, _, reason = skip
    assert len(reason.split()) >= MIN_REASON_WORDS, reason


def test_dockerfile_skip_is_a_batch_job():
    """CKV_DOCKER_2 is skipped only because the container runs once and exits."""
    text = DOCKERFILES[0].read_text(encoding="utf-8")
    assert "HEALTHCHECK" not in re.sub(r"#.*", "", text)
    (reason,) = [r for f, _, c, r in _all_skips() if c == "CKV_DOCKER_2"]
    assert "exits" in reason


@pytest.mark.passes_on_main
def test_the_plugin_bucket_logs_to_the_skipped_bucket():
    """CKV_AWS_18 is fixed on the plugin bucket; the skip is on its log target only."""
    path = PLUGIN_BUCKET_STACK
    template, _ = decode_str(path.read_text(encoding="utf-8"))
    assert template is not None
    logging = template["Resources"]["PluginBucket"]["Properties"]["LoggingConfiguration"]
    assert logging["DestinationBucketName"] == {"Ref": "AccessLogBucket"}


def _ci_checkov_commands() -> list[str]:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    commands = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "checkov " in run:
                commands.append(run)
    assert commands
    return commands


@pytest.mark.parametrize("check", ["CKV_AWS_46", "CKV_AWS_111", "CKV_AWS_109", "CKV_AWS_18"])
def test_ci_does_not_skip_a_reviewed_check_globally(check):
    for command in _ci_checkov_commands():
        skipped = re.findall(r"--skip-check\s+(\S+)", command)
        assert all(check not in s.split(",") for s in skipped), command


def test_pre_commit_checkov_skips_the_quarantined_snapshot():
    """The checkov hook's own entry scans ``.``; contrib/ keeps its own findings (quarantine, #1412)."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [h for repo in config["repos"] for h in repo["hooks"] if h["id"] == "checkov"]
    assert hooks
    for hook in hooks:
        args = hook["args"]
        assert "(^|/)contrib/" in [
            args[i + 1] for i, a in enumerate(args) if a == "--skip-path"
        ], hook


def test_every_file_ci_scans_is_reviewed_here():
    """A file CI hands to checkov with ``-f`` must be in this test's lists."""
    scanned = {
        REPO_ROOT / path
        for command in _ci_checkov_commands()
        for path in re.findall(r"-f\s+(\S+)", command)
    }
    assert scanned
    assert scanned <= set(TEMPLATES) | set(DOCKERFILES), sorted(
        str(p) for p in scanned - set(TEMPLATES) - set(DOCKERFILES)
    )
