#!/usr/bin/env python3
"""Generate OpenAPI spec from the live FastAPI app.

Dumps the auto-generated OpenAPI JSON to docs/architecture/openapi.json.
Run this whenever routes or models change to keep the spec in sync.

Usage:
    uv run python scripts/generate_api_spec.py

CI check (fails if spec is stale):
    uv run python scripts/generate_api_spec.py --check
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on the path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Stub out environment variables that main.py reads at import time
os.environ.setdefault("STATE_MACHINE_ARN", "")
os.environ.setdefault("S3_BUCKET", "")
os.environ.setdefault("LOG_GROUP", "")
os.environ.setdefault("PROJECT_NAME", "modernizer")
os.environ.setdefault("ENVIRONMENT", "dev")

from src.api.main import app  # noqa: E402

OUTPUT_PATH = ROOT / "docs" / "02-architecture" / "openapi.json"


def generate() -> "dict[str, Any]":
    """Return the OpenAPI spec dict from the FastAPI app."""
    return app.openapi()  # type: ignore[no-any-return]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenAPI spec from FastAPI app")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: fail if the committed spec is out of date (for CI)",
    )
    args = parser.parse_args()

    spec = generate()
    new_content = json.dumps(spec, indent=2) + "\n"

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"ERROR: {OUTPUT_PATH} does not exist. Run without --check to generate it.")
            sys.exit(1)
        existing = OUTPUT_PATH.read_text()
        if existing != new_content:
            print(
                f"ERROR: {OUTPUT_PATH} is out of date.\n"
                "Run `uv run python scripts/generate_api_spec.py` and commit the result."
            )
            sys.exit(1)
        print(f"OK: {OUTPUT_PATH} is up to date.")
        return

    OUTPUT_PATH.write_text(new_content)

    paths = spec.get("paths", {})
    print(f"Generated {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"  {len(paths)} paths")
    for path in sorted(paths):
        methods = ", ".join(m.upper() for m in paths[path] if m != "parameters")
        print(f"  {methods:30s} {path}")


if __name__ == "__main__":
    main()
