"""Redact credentials from request inputs stored before #723.

Before #723 the Assessments API copied ``connection.password`` into each job's
``<db>/<job>/_meta.json``, and agent answers (``<db>/<job>/<agent>/answers.json``) were
stored without any credential check. :func:`redact_stored_inputs` rewrites every such file
that still holds a credential: secret-named keys with a value are removed from
``_meta.json``, and strings that hold an unambiguous credential become ``<redacted>``
(``src.api.secret_guard``, conservative reading). Nothing else is changed: over-long
strings, deeply nested values and text that only resembles a credential (an e-mail
address after a label) stay exactly as they were.

Every file is handled on its own: a file that cannot be read, parsed, cleaned or written
(read-only, malformed, nested too deeply) is logged by path and skipped, and the run goes
on. A clean file is never rewritten, so a second run changes nothing.

The API runs it once, in a background thread after start-up, and records
:data:`MARKER` (under the artifact root) when a run finished with no file skipped, so
later starts skip it. ``uv run python scripts/redact_stored_inputs.py`` runs it by hand,
whatever the marker says.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.api.secret_guard import redact_credential_shapes, strip_secrets
from src.shared.contract_versioning import stamp_artifact_version
from src.storage.local_store import LocalArtifactStore

logger = logging.getLogger(__name__)

#: Recorded under the artifact root once a full run skipped nothing. The leading
#: underscore keeps it apart from ``.meta/<job_id>.json`` (job ids never start with one).
#: v2 (#1001) added the plain-language forms, so a v1 run does not skip them.
MARKER = ".meta/_stored-input-redaction-v2.json"

#: (glob under the artifact root, how to clean one parsed file)
_STORED_INPUTS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("*/*/_meta.json", strip_secrets),
    ("*/*/*/answers.json", redact_credential_shapes),
)


@dataclass
class RedactionRun:
    """Paths (relative to the artifact root) that were rewritten and that were skipped."""

    rewritten: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _candidates(root: Path, pattern: str) -> list[Path]:
    found = []
    for path in sorted(root.glob(pattern)):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue  # .trash/, .meta/: not request inputs
        if path.is_file():
            # A symlink that resolves outside the root is refused by the store's own
            # containment check on read and ends up in ``skipped``.
            found.append(path)
    return found


def _redact_one(store: LocalArtifactStore, key: str, clean: Callable[[Any], Any]) -> bool:
    data = store.read_json(key)
    cleaned = clean(data)
    if cleaned == data:
        return False
    # A rewrite: a contract file (answers.json) says today's version (#1018).
    store.write_json(key, stamp_artifact_version(key, cleaned))
    return True


def redact_stored_inputs(artifact_dir: str | Path) -> RedactionRun:
    """Rewrite each stored input under ``artifact_dir`` that holds a credential.

    Never raises for one file: any error reading, cleaning or writing it puts the path in
    ``skipped`` and the run continues.
    """
    run = RedactionRun()
    root = Path(artifact_dir)
    if not root.is_dir():
        return run
    store = LocalArtifactStore(base_dir=str(root))
    for pattern, clean in _STORED_INPUTS:
        for path in _candidates(root, pattern):
            key = path.relative_to(root).as_posix()
            try:
                if _redact_one(store, key, clean):
                    run.rewritten.append(key)
            except Exception as exc:  # noqa: BLE001 - per file (incl. RecursionError), never fatal
                logger.warning("redaction: skipped %s (%s)", key, type(exc).__name__)
                run.skipped.append(key)
    if run.rewritten:
        logger.warning("redaction: removed stored credentials from %d file(s)", len(run.rewritten))
    return run


def record_marker(artifact_dir: str | Path, run: RedactionRun) -> None:
    LocalArtifactStore(base_dir=str(artifact_dir)).write_json(
        MARKER,
        {
            "completed_at": datetime.now(UTC).isoformat(),
            "rewritten": len(run.rewritten),
        },
    )


def redact_stored_inputs_once(artifact_dir: str | Path) -> RedactionRun | None:
    """The start-up run: skipped when :data:`MARKER` exists; records it when nothing was
    skipped. Never raises."""
    try:
        root = Path(artifact_dir)
        if (root / MARKER).exists():
            return None
        run = redact_stored_inputs(root)
        if run.skipped:
            logger.warning(
                "redaction: %d file(s) skipped, so this runs again at the next start; fix "
                "them, then run scripts/redact_stored_inputs.py",
                len(run.skipped),
            )
        elif root.is_dir():
            record_marker(root, run)
        return run
    except Exception as exc:  # noqa: BLE001 - must never stop the API
        logger.warning("redaction: start-up run failed (%s)", type(exc).__name__)
        return None
