"""The runtime-reported package version comes from installed metadata, not a
literal (#391) -- so it can never drift from the git tag hatch-vcs derived it
from at build time.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from src.shared.package_version import PACKAGE_NAME, get_package_version


def test_matches_the_installed_distribution_metadata():
    assert get_package_version() == importlib.metadata.version(PACKAGE_NAME)


def test_falls_back_when_the_distribution_is_not_installed(monkeypatch):
    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)

    assert get_package_version() == "0.0.0"


def test_fallback_matches_pyprojects_hatch_vcs_fallback_version():
    """Keep the two fallbacks (hatch-vcs's build-time one, this module's
    runtime one) from drifting apart.
    """
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())

    with pytest.MonkeyPatch.context() as mp:

        def _raise(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        mp.setattr(importlib.metadata, "version", _raise)
        assert get_package_version() == data["tool"]["hatch"]["version"]["fallback-version"]
