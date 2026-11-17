#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Add a bundled sample's synthetic routine set to its extracted collection (#1954, #2385).

Usage:
    uv run python ci/llm/sample_routines.py <collection.json> <sample>

``docs/examples/<sample>/<sample>-routines.json`` holds the routine sections the collection
script writes (``procedures``, ``routine_parameters``, ``triggers``, ``events`` and
``metadata.routine_capture``), kept apart from the sample's zip. Each list section is
appended to the collection's own and each ``metadata`` key is set, in place, so the file
reaches the routine phase the way any collection does. The deterministic e2e pipeline
(``tests/e2e/pipeline.py``) and the headless e2e-llm run (``ci/e2e-llm.sh``) both use it,
so a headless ``/modernize`` run has routines to check and one to repair (Phase 6f). A
sample with no routine set is left as it is. Prints one JSON line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def sample_routines(name: str, repo: Path = REPO_ROOT) -> Path | None:
    """The sample's routine set, or None when it has none."""
    path = repo / "docs" / "examples" / name / f"{name}-routines.json"
    return path if path.exists() else None


def add_sample_routines(collection: Path, name: str, repo: Path = REPO_ROOT) -> int:
    """Add ``name``'s routine set to ``collection`` in place; the routines added."""
    routines = sample_routines(name, repo)
    if routines is None:
        return 0
    from src.tools.database.collection_text import extract_json_object

    raw = json.loads(extract_json_object(collection.read_text(encoding="utf-8")))
    added = json.loads(routines.read_text(encoding="utf-8"))
    for key, value in added.items():
        if key == "metadata":
            raw.setdefault("metadata", {}).update(value)
        else:
            raw[key] = [*(raw.get(key) or []), *value]
    collection.write_text(json.dumps(raw), encoding="utf-8")
    return sum(len(added.get(k) or []) for k in ("procedures", "triggers", "events"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ci/llm/sample_routines.py <collection.json> <sample>", file=sys.stderr)
        return 2
    collection, name = Path(argv[0]), argv[1]
    if not collection.is_file():
        print(f"sample_routines: no collection at {collection}", file=sys.stderr)
        return 2
    print(json.dumps({"sample": name, "routines_added": add_sample_routines(collection, name)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
