"""Argument containment for the scripts a headless CI `/modernize` run may call.

`ci/e2e-llm.sh` runs `claude -p` with `.claude/settings.ci.json`, whose Bash
allowlist is exactly the pipeline scripts. That makes those scripts'
arguments the remaining way for a prompt-injected or hallucinated command to
reach outside the repository (e.g. `--file /etc/passwd`, `--artifact-root /`,
`--db ../..`). When `MODERNIZER_CI_SANDBOX=1` (exported by `ci/e2e-llm.sh`),
every allowlisted script calls :func:`sandbox_violation` right after parsing
its arguments and exits with its normal JSON error if it returns a message.

Outside that environment variable this is a no-op, so local and production
use of the scripts is unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SANDBOX_ENV = "MODERNIZER_CI_SANDBOX"
REPO_ROOT = Path(__file__).resolve().parent.parent

PATH_ARGS: tuple[str, ...] = ("file", "artifact_root", "check_costs")
NAME_ARGS: tuple[str, ...] = ("db", "job_id")

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _flag(attr: str) -> str:
    return "--" + attr.replace("_", "-")


def sandbox_violation(
    args: Any,
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
    path_args: Iterable[str] = PATH_ARGS,
    name_args: Iterable[str] = NAME_ARGS,
) -> str | None:
    """Return why ``args`` escapes the sandbox, or ``None`` if it doesn't (or
    the sandbox is off). Attributes missing from ``args`` or set to ``None``
    are skipped. Paths are resolved (symlinks followed) relative to the
    current working directory, exactly as the scripts themselves open them."""
    env = os.environ if environ is None else environ
    if env.get(SANDBOX_ENV) != "1":
        return None

    root = repo_root.resolve()
    for attr in path_args:
        value = getattr(args, attr, None)
        if value is None:
            continue
        # ``--check-costs`` takes one or more paths (issue #313); a single
        # value is still checked exactly as before.
        values = value if isinstance(value, list) else [value]
        for item in values:
            try:
                resolved = Path(item).resolve()
            except (OSError, RuntimeError, ValueError):
                return (
                    f"{_flag(attr)} cannot be resolved (symlink loop, invalid or too long); "
                    f"refused under {SANDBOX_ENV}=1"
                )
            if not resolved.is_relative_to(root):
                return (
                    f"{_flag(attr)} {item!r} resolves outside the repository root ({root}); "
                    f"refused under {SANDBOX_ENV}=1"
                )

    for attr in name_args:
        value = getattr(args, attr, None)
        if value is None:
            continue
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or set(value) == {"."}:
            return (
                f"{_flag(attr)} {value!r} must match [A-Za-z0-9_.-]+ and not be '.' or '..'; "
                f"refused under {SANDBOX_ENV}=1"
            )
    return None
