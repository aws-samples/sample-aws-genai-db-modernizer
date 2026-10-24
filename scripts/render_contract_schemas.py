#!/usr/bin/env python3
"""Render the JSON Schemas of the public ``assess()`` request and result (#1183).

The Pydantic models in ``src/assessment/models.py`` are the source. This script writes
their JSON Schemas into ``docs/contracts/schemas/``, one file per model and contract
MAJOR version, so a caller in another language (or one validating payloads outside
Python) has a published schema, and a change to the public surface shows up in review:

- ``assess_request.v<MAJOR>.schema.json``: ``AssessmentRequest``, the shape a caller sends
  (the model's validation schema);
- ``assess_result.v<MAJOR>.schema.json``: ``AssessmentResult``, the shape ``assess()``
  returns (the model's serialization schema, the JSON ``model_dump_json`` writes).

The files are generated baselines: never edit them by hand. ``--check`` (run in
``ci/lint.sh``) fails when a committed file differs from a fresh render, so a model
change without a regeneration is caught.

Usage:
    uv run python scripts/render_contract_schemas.py --write    # write both files
    uv run python scripts/render_contract_schemas.py --check    # exit 1 if either is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.assessment.models import (  # noqa: E402
    CONTRACT_VERSION,
    AssessmentRequest,
    AssessmentResult,
)

SCHEMA_DIR = REPO_ROOT / "docs" / "contracts" / "schemas"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
#: Where each file is published: its raw URL on this repository's main branch, so a
#: validator can resolve and cache it by ``$id``.
PUBLISHED = (
    "https://raw.githubusercontent.com/aws-samples/sample-aws-genai-db-modernizer/main/"
    "docs/contracts/schemas/"
)
MAJOR = CONTRACT_VERSION.split(".")[0]


def schema_path(name: str) -> Path:
    """``docs/contracts/schemas/<name>.v<MAJOR>.schema.json``."""
    return SCHEMA_DIR / f"{name}.v{MAJOR}.schema.json"


def _schema(model: Any, mode: str, title: str) -> dict[str, Any]:
    body = model.model_json_schema(mode=mode)
    return {
        "$schema": DRAFT,
        "$id": PUBLISHED + schema_path(title).name,
        "title": body.pop("title", title),
        "x-contract-version": CONTRACT_VERSION,
        **body,
    }


def render() -> list[tuple[Path, str]]:
    """Each schema file and its exact text (stable key order, two-space indent)."""
    outputs = []
    for name, model, mode in (
        ("assess_request", AssessmentRequest, "validation"),
        ("assess_result", AssessmentResult, "serialization"),
    ):
        text = json.dumps(_schema(model, mode, name), indent=2, ensure_ascii=False) + "\n"
        outputs.append((schema_path(name), text))
    return outputs


def _shown(path: Path) -> str:
    """``path`` relative to the repository when it is inside it."""
    return path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write both schema files")
    action.add_argument("--check", action="store_true", help="exit 1 if a file is stale")
    args = parser.parse_args(argv)
    outputs = render()
    if args.check:
        stale = [
            _shown(path)
            for path, expected in outputs
            if (path.read_text(encoding="utf-8") if path.exists() else "") != expected
        ]
        if stale:
            print(
                f"{', '.join(stale)} out of date. "
                "Run: uv run python scripts/render_contract_schemas.py --write"
            )
            return 1
        print(json.dumps({"render_contract_schemas": "up to date"}))
        return 0
    for path, expected in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
    print(json.dumps({"wrote": [_shown(path) for path, _ in outputs]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
