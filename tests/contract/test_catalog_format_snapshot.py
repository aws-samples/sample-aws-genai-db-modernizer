"""The rule catalog's TOML format stays backwards compatible within 0.x (#1647).

``tests/contract/catalog_format_snapshot.json`` records, per file kind under
``src/catalog/``, its files and its key paths with their value types and whether every
file has them. This fails when a change removes, renames or retypes a key, drops a
required key from any file (or array element), makes an optional key required, or removes
a file or a whole file kind (rule ids are stable): callers read these files,
and any of those breaks a caller's reader. Adding keys, files or file kinds passes. The
values (scores, penalties, prices, thresholds) are tuning and are not compared, and maps
keyed by data (instance classes, models: ``DATA_KEYED``) are compared by value schema.

A deliberate removal does not edit the snapshot to make this pass. It needs a documented
deprecation first (the key or file stays readable for at least one release and the release
notes name it), then a maintainer-approved snapshot update, recorded by the release step
from the tag itself:
``uv run python scripts/catalog_format_snapshot.py --write-released <tag>``.
The snapshot is never edited by hand (``scripts/hooks/block_baseline_edit.py``).
"""

from __future__ import annotations

import json
from typing import Any, cast

from scripts.catalog_format_snapshot import (
    SNAPSHOT_PATH,
    WRITE_RELEASED_COMMAND,
    build_format,
    evaluate_format,
)


def _recorded() -> dict[str, Any]:
    assert SNAPSHOT_PATH.exists(), f"missing; record it with {WRITE_RELEASED_COMMAND}"
    return cast(dict[str, Any], json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))


def test_the_catalog_keeps_the_recorded_format() -> None:
    problems = evaluate_format(_recorded()["kinds"], build_format())
    assert not problems, (
        "The rule catalog's TOML format changed in a way that breaks callers' readers:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\nKeep the key or file readable. A deliberate removal needs a documented "
        "deprecation and a maintainer-approved snapshot update at the release: "
        + WRITE_RELEASED_COMMAND
    )


def test_the_snapshot_covers_every_file_kind_and_names_its_release() -> None:
    recorded = _recorded()
    assert recorded["released"] is None or recorded["released"].startswith("v")
    kinds = recorded["kinds"]
    for name in ("engines", "patterns", "gaps", "aurora_versions", "write_intents"):
        assert kinds[name]["files"] and kinds[name]["keys"], name
    for entry in kinds.values():
        assert entry["location"].startswith("src/catalog/")
