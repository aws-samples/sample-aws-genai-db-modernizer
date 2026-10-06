"""The package version is derived from the git tag, never hand-edited (#391).

Two things are proved here:

- ``pyproject.toml`` declares ``version`` dynamic, with ``hatch-vcs`` wired
  up and a safe ``fallback-version`` for a checkout with no ``.git`` at all
  (e.g. a source archive).
- The actual tag -> PEP 440 mapping that ``hatchling``/``hatch-vcs`` perform,
  proved by running the real ``uv build`` against throwaway git repos (not
  just the ``setuptools_scm`` library hatch-vcs delegates to) and reading the
  version off the produced wheel's filename. ``v0.1.0-beta.3`` -> ``0.1.0b3``
  and ``v0.2.0-beta.1`` -> ``0.2.0b1`` in particular, since those are real
  tags this project has used or plans to use.

A fourth shape is also covered: ``scripts/maintainer-sync.sh``'s target repo.
That script requires the target to already be a git repository
(``scripts/maintainer-sync.sh:29``) and excludes ``.git`` from the rsync, so
the mirror keeps its *own* history -- never the source's ``v*`` tags. That
is not the same as "no ``.git`` at all": the mirror still has a ``.git``, so
it resolves to a plain commit-count dev version (``0.0.1.devN+g<sha>``) that
changes on every sync commit, rather than the ``0.0.0`` ``fallback-version``.
Deliberately not "fixed" -- a dev version with no matching tag is already
obviously not a release.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 -- fixed argv against throwaway tmp_path git repos
import tomllib
from pathlib import Path

import pytest
from packaging.utils import parse_wheel_filename
from packaging.version import Version

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# A minimal stand-in for this project's pyproject.toml -- just the bits that
# drive the tag -> version mapping (dynamic version, hatch-vcs, fallback) --
# so each build in this file is fast and self-contained rather than building
# the whole real project repeatedly.
_MINIMAL_PYPROJECT = """\
[project]
name = "database-modernizer"
dynamic = ["version"]
description = "throwaway fixture for tests/unit/test_version_from_tag.py"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "vcs"
fallback-version = "0.0.0"

[tool.hatch.build.targets.wheel]
packages = ["src"]
"""


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def test_project_version_is_dynamic():
    data = _load_pyproject()
    assert data["project"].get("dynamic") == ["version"]
    assert "version" not in data["project"]


def test_hatch_vcs_is_a_build_requirement():
    data = _load_pyproject()
    assert "hatch-vcs" in data["build-system"]["requires"]


def test_hatch_version_source_is_vcs_with_a_fallback():
    data = _load_pyproject()
    hatch_version = data["tool"]["hatch"]["version"]
    assert hatch_version["source"] == "vcs"
    # Must parse as a valid PEP 440 version itself, so a checkout with no git
    # metadata still produces an installable package.
    Version(hatch_version["fallback-version"])


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed argv, git resolved from PATH
        ["git", *args], cwd=cwd, check=True, capture_output=True
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A throwaway git repo with the minimal hatch-vcs-driven project layout."""
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("# throwaway fixture\n")
    (repo / "pyproject.toml").write_text(_MINIMAL_PYPROJECT)
    src = repo / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    return repo


def _commit(repo: Path, message: str) -> None:
    (repo / "CHANGE.txt").write_text(message)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


def _build_wheel_version(repo: Path) -> Version:
    """Run the real ``uv build`` and read the version off the wheel it produces.

    ``--offline``: hatchling/hatch-vcs are already resolved into uv's cache by
    this repo's own ``uv sync``/``uv build`` (see ci/test.sh's prerequisites,
    ci/README.md) -- the no-network rule for unit tests holds because nothing
    here needs a fresh index lookup.
    """
    dist = repo / "dist"
    result = subprocess.run(  # nosec B603 B607 -- fixed argv, uv resolved from PATH
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(dist)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    _name, version, _build_tag, _tags = parse_wheel_filename(wheels[0].name)
    return version


# The suite runs with --fail-on-skip; `uv` not being on PATH would be
# surprising (this whole project's tooling is built on it), but mirror the
# early-return pattern tests/unit/scripts/test_maintainer_sync.py uses for
# rsync rather than skipping outright.
def _uv_missing() -> bool:
    return shutil.which("uv") is None


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.1.0-beta.1", "0.1.0b1"),
        ("v0.1.0-beta.3", "0.1.0b3"),
        # The tag the next release is planned to use (#391).
        ("v0.2.0-beta.1", "0.2.0b1"),
        ("v1.0.0", "1.0.0"),
    ],
)
def test_tag_to_version_mapping_at_the_tagged_commit(tmp_path, tag, expected):
    if _uv_missing():
        return
    repo = _init_repo(tmp_path)
    _commit(repo, "init")
    _git("tag", tag, cwd=repo)

    assert _build_wheel_version(repo) == Version(expected)


def test_untagged_commit_after_a_beta_tag_guesses_the_next_prerelease(tmp_path):
    if _uv_missing():
        return
    repo = _init_repo(tmp_path)
    _commit(repo, "init")
    _git("tag", "v0.2.0-beta.1", cwd=repo)
    _commit(repo, "second")

    version = _build_wheel_version(repo)

    # guess-next-dev (hatch-vcs's/setuptools_scm's default scheme) bumps the
    # prerelease number for the next dev build rather than repeating 0.2.0b1.
    assert str(version).startswith("0.2.0b2.dev1+g")


def test_no_git_metadata_falls_back_to_the_fallback_version(tmp_path):
    """A directory with no ``.git`` at all -- e.g. a source archive export."""
    if _uv_missing():
        return
    repo = tmp_path / "no_git"
    repo.mkdir()
    (repo / "README.md").write_text("# throwaway fixture\n")
    (repo / "pyproject.toml").write_text(_MINIMAL_PYPROJECT)
    src = repo / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")

    assert _build_wheel_version(repo) == Version("0.0.0")


def test_sync_script_mirror_resolves_to_a_dev_version_not_the_fallback(tmp_path):
    """Reproduce ``scripts/maintainer-sync.sh``'s target repo shape.

    The script requires ``$TARGET_REPO_PATH/.git`` to already exist
    (``scripts/maintainer-sync.sh:29``) and excludes ``.git`` from the rsync
    (``RSYNC_ARGS`` includes ``--exclude=".git/"``), so the mirror keeps its
    own, tagless git history rather than the source's ``v*`` tags -- a
    different shape than "no ``.git`` at all". It must still build (not
    error), and should resolve to an obviously-not-a-release dev version
    rather than silently reusing the ``0.0.0`` fallback.
    """
    if _uv_missing() or shutil.which("rsync") is None:
        return

    source = _init_repo(tmp_path, name="source")
    _commit(source, "init")
    _git("tag", "v0.2.0-beta.1", cwd=source)  # the mirror must not see this

    # The target is "already a git repo" with its own, unrelated history --
    # maintainer-sync.sh refuses to run otherwise.
    target = tmp_path / "target"
    target.mkdir()
    _git("init", "-q", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)
    (target / "SEED.txt").write_text("pre-existing target history\n")
    _git("add", "-A", cwd=target)
    _git("commit", "-q", "-m", "chore: initial target state", cwd=target)

    subprocess.run(  # nosec B603 B607 -- fixed argv, rsync resolved from PATH
        [
            "rsync",
            "-a",
            "--checksum",
            "--delete",
            "--exclude=.git/",
            "--exclude=.git",
            f"{source}/",
            f"{target}/",
        ],
        check=True,
        capture_output=True,
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-q", "-m", "chore: sync changes from source branch main", cwd=target)

    version = _build_wheel_version(target)

    assert re.fullmatch(r"0\.0\.1\.dev\d+\+g[0-9a-f]+", str(version)), version
