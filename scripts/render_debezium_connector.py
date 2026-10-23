#!/usr/bin/env python3
"""Render a Debezium connector stack for Amazon MSK Connect (#1414, ADR-033).

Reads a connector input (the ``job_id``, ``wave``, ``source``, ``cdc`` and ``hub``
sections of a wave's data movement plan, see
``src/tools/data_movement/debezium_connector.py``) and writes the CloudFormation
template of the connector stack. No AWS call is made; deploying it is a separate,
manual step (``infrastructure/cloudformation/data-movement/README.md``).

Usage:
    uv run python scripts/render_debezium_connector.py --input in.json --output stack.yaml
    uv run python scripts/render_debezium_connector.py --examples          # rewrite the examples
    uv run python scripts/render_debezium_connector.py --examples --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import ValidationError  # noqa: E402

from src.tools.data_movement.debezium_connector import (  # noqa: E402
    DebeziumConnectorInput,
    dump_template,
    render_connector_template,
    replica_identity_prerequisites,
)

EXAMPLES_DIR = REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "examples"
INPUT_SUFFIX = ".input.json"


def _load(input_path: Path) -> DebeziumConnectorInput:
    return DebeziumConnectorInput.model_validate(json.loads(input_path.read_text()))


def render_file(input_path: Path) -> str:
    return dump_template(render_connector_template(_load(input_path)))


def example_pairs(examples_dir: Path = EXAMPLES_DIR) -> list[tuple[Path, Path]]:
    """Each example input and the template rendered from it."""
    return [
        (p, p.with_name(p.name[: -len(INPUT_SUFFIX)] + ".yaml"))
        for p in sorted(examples_dir.glob(f"*{INPUT_SUFFIX}"))
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, help="connector input JSON")
    parser.add_argument("--output", type=Path, help="template to write (stdout if omitted)")
    parser.add_argument("--examples", action="store_true", help="render the checked-in examples")
    parser.add_argument("--check", action="store_true", help="with --examples: only compare")
    args = parser.parse_args(argv)

    if args.examples:
        stale = []
        for input_path, output_path in example_pairs():
            rendered = render_file(input_path)
            current = output_path.read_text() if output_path.exists() else None
            if rendered == current:
                continue
            if args.check:
                stale.append(output_path.relative_to(REPO_ROOT).as_posix())
            else:
                output_path.write_text(rendered)
        if stale:
            print(json.dumps({"status": "stale", "files": stale}))
            return 1
        print(json.dumps({"status": "ok"}))
        return 0

    if args.input is None:
        parser.error("--input is required (or use --examples)")
    try:
        spec = _load(args.input)
    except (ValidationError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    rendered = dump_template(render_connector_template(spec))
    if args.output:
        args.output.write_text(rendered)
        status: dict[str, object] = {"status": "ok", "output": str(args.output)}
        identity = replica_identity_prerequisites(spec)
        if identity:
            # Set before deploy, or UPDATE/DELETE on these tables fail.
            status["replica_identity_before_deploy"] = [
                {"table": name, "replica_identity": mode} for name, mode in identity
            ]
        print(json.dumps(status))
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
