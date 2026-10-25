"""The webkit system packages are cached, and e2e-llm uses the shared install helpers (#1375)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SHA_PIN = re.compile(r"^[\w./-]+@[0-9a-f]{40}( #.*)?$")


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["e2e-shard"]["steps"]
    return steps


def test_the_apt_packages_are_cached_for_the_shards_that_install_webkit():
    steps = _steps()
    (cache,) = [s for s in steps if s.get("id") == "apt-cache"]
    assert cache["uses"].startswith("actions/cache@")
    assert cache["with"]["path"] == "~/apt-archives"
    key = cache["with"]["key"]
    assert "steps.image.outputs.name" in key and "steps.playwright.outputs.version" in key
    for shard in ("ui", "hashseed", "determinism", "replay"):
        assert f"matrix.shard != '{shard}'" in cache["if"]
    names = [s.get("name", "") for s in steps]
    assert names.index("Seed the apt package cache") < names.index("End-to-end")
    assert names.index("End-to-end") < names.index("Keep the downloaded apt packages for the cache")


def test_the_apt_cache_is_copied_into_and_out_of_the_root_owned_apt_directory():
    by_name = {s.get("name"): s for s in _steps()}
    seed = by_name["Seed the apt package cache"]
    assert seed["if"] == "steps.apt-cache.outputs.cache-hit == 'true'"
    assert "/var/cache/apt/archives" in seed["run"]
    keep = by_name["Keep the downloaded apt packages for the cache"]
    assert "cache-hit != 'true'" in keep["if"] and "always()" in keep["if"]
    assert "/var/cache/apt/archives/*.deb" in keep["run"]


@pytest.mark.passes_on_main  # guards the existing pins against the new steps
def test_every_action_in_the_workflow_is_pinned_by_sha():
    for step in _steps():
        if "uses" in step:
            assert SHA_PIN.match(step["uses"]), step["uses"]


@pytest.mark.passes_on_main  # guards the existing permissions against the new steps
def test_the_workflow_keeps_least_privilege_and_adds_no_secrets():
    text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", "")


def test_e2e_llm_installs_browsers_with_the_shared_helpers_not_with_deps():
    script = (REPO / "ci/e2e-llm.sh").read_text(encoding="utf-8")
    assert "--with-deps" not in script
    assert "playwright_install_with_retry chromium webkit" in script
    assert "playwright_ensure_deps chromium webkit" in script
