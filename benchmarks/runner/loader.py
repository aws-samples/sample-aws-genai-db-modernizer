"""Discover and load benchmark cases from a stage directory + manifest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Case:
    case_id: str
    path: Path
    intent: str
    tags: list[str]
    reviewed: bool
    collection: dict
    triage: dict
    analysis: dict  # engine -> analysis dict
    expected: dict  # query_id -> {acceptable, ideal?, rationale?}
    # A bundled sample (docs/examples/<sample>/<sample>.zip) the case runs from
    # instead of its own collection/triage/analysis files (final_assignment, #1074).
    sample: str | None = None


def load_cases(stage_dir: Path, tags: list[str] | None = None) -> list[Case]:
    """Load all cases listed in stage_dir/index.json, optionally filtered by tag."""
    manifest = json.loads((stage_dir / "index.json").read_text())
    cases: list[Case] = []
    for entry in manifest.get("cases", []):
        if tags and not (set(tags) & set(entry.get("tags", []))):
            continue
        cdir = stage_dir / entry["path"]
        sample = entry.get("sample")
        # A sample case carries only its key: the pipeline makes the rest.
        collection = {} if sample else json.loads((cdir / "collection.json").read_text())
        triage = {} if sample else json.loads((cdir / "triage.json").read_text())
        analysis: dict = {}
        adir = cdir / "analysis"
        if adir.is_dir() and not sample:
            for f in sorted(adir.glob("*.json")):
                analysis[f.stem] = json.loads(f.read_text())
        key = json.loads((cdir / "expected.json").read_text())
        cases.append(
            Case(
                case_id=entry["id"],
                path=cdir,
                intent=entry.get("intent", ""),
                tags=entry.get("tags", []),
                reviewed=bool(key.get("reviewed", False)),
                collection=collection,
                triage=triage,
                analysis=analysis,
                expected=key.get("expected", {}),
                sample=sample,
            )
        )
    return cases
