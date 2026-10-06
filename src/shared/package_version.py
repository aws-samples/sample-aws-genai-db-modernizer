"""Resolve the installed package version, with a safe fallback (#391).

The version is derived from the git release tag by ``hatch-vcs``
(``[tool.hatch.version]`` in ``pyproject.toml``) at build time, never
hand-edited. At runtime, code that needs to report the version reads it back
from the installed distribution's metadata through ``importlib.metadata``,
so a literal string here can't drift from the tag that produced the build --
the drift this module exists to prevent (``pyproject.toml`` said ``0.1.0b1``
while the latest tag was already ``v0.1.0-beta.3``).

Falls back to ``"0.0.0"`` -- the same value as ``pyproject.toml``'s
``[tool.hatch.version].fallback-version`` -- when the distribution's metadata
can't be found at all, e.g. running from a source tree that was never
installed via ``pip``/``uv``.
"""

from __future__ import annotations

import importlib.metadata

PACKAGE_NAME = "database-modernizer"
_FALLBACK_VERSION = "0.0.0"


def get_package_version() -> str:
    """Return the installed package version, or the fallback if unresolvable."""
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _FALLBACK_VERSION
