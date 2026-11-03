#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ask a job's context graph a catalog question from the command line (#307).

The questions are the entries of the graph query catalog (``src/graph/catalog.py``):
fixed, read-only, bounded Cypher. This script never takes Cypher.

Usage:
    uv run python scripts/graph_query.py list
    uv run python scripts/graph_query.py params <id> [--job-id <job>]
    uv run python scripts/graph_query.py run <id> --job-id <job> [--param name=value ...]
        [--db <db>] [--artifact-root <dir>] [--format json|md]

Each command prints one compact JSON line (``--format md`` prints Markdown);
errors, usage errors included, print ``{"status": "error", "message": ...}``
and exit 1 (``--help`` prints help and exits 0). ``--clean`` removes the job's
CLI copy of the graph (``graph/cli/``) after answering. ``--db`` is
found from the job id when only one database folder holds that job.

The job's graph is opened read-only, with the read timeout and the entry's
row bound. When the API's graph (``<db>/<job>/graph/context.lbug``) is current
it is read as it is; otherwise the script builds its own copy
(``graph/cli/context.lbug``) under a file lock and reads that, so it never
writes the file the API serves (``src/graph/job_graph.py``). The copy is
removed when a later run finds the API's graph current again. On Windows the
script always uses its own copy (reading the API's file while the API replaces
it needs POSIX rename semantics).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._sandbox import sandbox_violation  # noqa: E402
from src.graph import catalog, job_graph  # noqa: E402
from src.report.escaping import md_cell  # noqa: E402
from src.shared.database_name import validate_database_name  # noqa: E402
from src.shared.job_id import validate_job_id  # noqa: E402
from src.storage.local_store import LocalArtifactStore  # noqa: E402


class UsageError(ValueError):
    """A bad command line: printed as the JSON error."""


class _JsonArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors become the JSON error line (exit 1)."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_JsonArgumentParser)

    def job_args(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument("--job-id", required=required)
        p.add_argument("--db", default=None, help="database folder (default: found from the job)")
        p.add_argument(
            "--artifact-root",
            default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
            help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
        )
        p.add_argument("--format", choices=("json", "md"), default="json")
        p.add_argument(
            "--clean",
            action="store_true",
            help="afterwards, remove this job's CLI copy of the graph (graph/cli/)",
        )

    job_args(sub.add_parser("list", help="every catalog entry"), required=False)
    params = sub.add_parser("params", help="an entry's parameters (and their choices for a job)")
    params.add_argument("entry")
    job_args(params, required=False)
    run = sub.add_parser("run", help="run one entry on a job's graph")
    run.add_argument("entry")
    run.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    job_args(run, required=True)
    return parser


def _entry(entry_id: str) -> catalog.CatalogEntry:
    try:
        return catalog.get_entry(entry_id)
    except KeyError:
        raise UsageError("unknown catalog entry; run 'graph_query.py list' for the ids") from None


def _params(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise UsageError("--param must be NAME=VALUE")
        out[name] = value
    return out


def _resolve_db(root: str, db: str | None, job_id: str) -> str:
    if db is not None:
        db = validate_database_name(db)
        if not (Path(root) / db / job_id).is_dir():
            raise UsageError("no job with this id in that database folder")
        return db
    found = sorted(
        d.name
        for d in Path(root).iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / job_id).is_dir()
    ) if Path(root).is_dir() else []  # fmt: skip
    if not found:
        raise UsageError("no job with this id under the artifact root")
    if len(found) > 1:
        raise UsageError("several database folders hold this job id; pass --db")
    return validate_database_name(found[0])


def _entry_summary(entry: catalog.CatalogEntry) -> dict:
    return {
        "id": entry.id,
        "ref": entry.ref,
        "title": entry.title,
        "question": entry.question,
        "params": [p.name for p in entry.params],
        "needs": [n.id for n in entry.needs],
        "max_rows": entry.max_rows,
    }


def cmd_list(args: argparse.Namespace) -> dict:
    return {"status": "ok", "entries": [_entry_summary(e) for e in catalog.entries()]}


def cmd_params(args: argparse.Namespace) -> dict:
    entry = _entry(args.entry)
    params: list[dict[str, object]] = [
        {
            "name": p.name,
            "kind": p.kind.value,
            "description": p.description,
            "example": p.example,
        }
        for p in entry.params
    ]
    out: dict = {"status": "ok", "entry": entry.id, "params": params}
    if args.job_id is None:
        return out
    job_id = validate_job_id(args.job_id)
    db = _resolve_db(args.artifact_root, args.db, job_id)
    store = LocalArtifactStore(base_dir=args.artifact_root)
    with job_graph.open_job_graph(store, args.artifact_root, db, job_id) as (graph, info):
        for item, param in zip(params, entry.params, strict=True):
            choices = catalog.param_choices(graph, param)
            item["choices"] = list(choices)
            item["truncated"] = choices.truncated
    out.update(db=db, job_id=job_id, graph=info)
    return out


def cmd_run(args: argparse.Namespace) -> dict:
    entry = _entry(args.entry)
    params = _params(args.param)
    catalog.check_params(entry, params)
    job_id = validate_job_id(args.job_id)
    db = _resolve_db(args.artifact_root, args.db, job_id)
    store = LocalArtifactStore(base_dir=args.artifact_root)
    with job_graph.open_job_graph(store, args.artifact_root, db, job_id) as (graph, info):
        result = catalog.run_entry(graph, entry, params)
        unmet = catalog.unmet_needs(graph, entry) if not result.rows else []
    out: dict = {
        "status": "ok",
        "entry": entry.id,
        "db": db,
        "job_id": job_id,
        "params": params,
        "columns": list(result.rows[0]) if result.rows else [],
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "max_rows": entry.max_rows,
        "graph": info,
    }
    if not result.rows:
        out["unmet_needs"] = [catalog.need_summary(n) for n in unmet]
        out["explanation"] = catalog.explain_empty(unmet)
    return out


def _cell(value: object) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v is not None)
    return md_cell("" if value is None else value)


def to_markdown(command: str, out: dict) -> str:
    """The same result as Markdown, for a person reading the terminal."""
    if out["status"] != "ok":
        return f"Error: {out['message']}\n"
    if command == "list":
        lines = ["| Ref | Id | Question | Parameters |", "|---|---|---|---|"]
        lines += [
            f"| {e['ref']} | `{e['id']}` | {_cell(e['question'])} | {_cell(e['params'])} |"
            for e in out["entries"]
        ]
        return "\n".join(lines) + "\n"
    entry = catalog.get_entry(out["entry"])
    lines = [f"### {entry.ref}. {entry.title}", "", entry.question, ""]
    if command == "params":
        for p in out["params"]:
            line = f"- `{p['name']}` ({p['kind']}): {p['description']} Example `{p['example']}`."
            if "choices" in p:
                more = " (more not shown)" if p["truncated"] else ""
                line += f" Choices: {_cell(p['choices'][:20])}{more}"
            lines.append(line)
        return "\n".join(lines) + "\n"
    if not out["rows"]:
        return "\n".join(lines + [out["explanation"]]) + "\n"
    columns = out["columns"]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "---|" * len(columns))
    lines += ["| " + " | ".join(_cell(r.get(c)) for c in columns) + " |" for r in out["rows"]]
    if out["truncated"]:
        lines += ["", f"Only the first {out['max_rows']:,} rows are shown."]
    return "\n".join(lines) + "\n"


COMMANDS = {"list": cmd_list, "params": cmd_params, "run": cmd_run}


def _clean(args: argparse.Namespace, out: dict) -> None:
    """``--clean``: remove the job's CLI copy once the command has answered."""
    if not getattr(args, "clean", False) or not out.get("job_id"):
        return
    job_graph.remove_cli_dir(args.artifact_root, out["db"], out["job_id"])
    out["cleaned"] = True


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except UsageError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, separators=(",", ":")))
        return 1
    fmt = getattr(args, "format", "json")
    violation = sandbox_violation(args)
    if violation:
        out: dict = {"status": "error", "message": violation}
    else:
        try:
            out = COMMANDS[args.command](args)
            _clean(args, out)
        except (UsageError, ValueError) as exc:
            out = {"status": "error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - this script must always print JSON
            out = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    if fmt == "md":
        sys.stdout.write(to_markdown(args.command, out))
    else:
        print(json.dumps(out, default=str, separators=(",", ":")))
    return 0 if out["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
