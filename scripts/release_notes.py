#!/usr/bin/env python3
"""The routing and contract changes a release carries (#1184).

A change can move engines for the same input, or change a contract's shape, without a
release saying so. This script writes the section the release attaches, for the range
``--since`` (the previous release tag) to ``--until`` (the new tag, default ``HEAD``):

- **Routing diff**: the committed per-query routing snapshot
  (``tests/e2e/baselines/routing_snapshot.json``, ``tests/e2e/routing_snapshot.py``)
  at both refs, one row per query whose final engine, score on that engine or reason
  code moved (the reason code of the query's last Move, ``pass:reason_code`` in the
  snapshot). A query added or removed is a row too.
- **Changelog**: one line per rule change (a file under ``src/catalog/`` added, changed
  or removed, with the subjects of the commits that touched it) and one per contract
  change (a contract whose version moved, was added or was removed, with the field
  changes ``scripts/contract_schema_snapshot.py`` classifies), read from the internal
  contracts' snapshot (``tests/contract/schema_snapshot.json``) and the public
  ``assess()`` contract's released snapshot (``tests/contract/assess_released_snapshot.json``).

It reads committed files at two refs and runs no pipeline, so it needs only git and the
repository's history (the release workflow checks out with full history).

Usage:
    uv run python scripts/release_notes.py --since v0.3.0-beta.1 --until v0.4.0 \\
        --output release-changes.md
    # The release body's capped copy (engine changes first, score drift summarised):
    uv run python scripts/release_notes.py --since v0.3.0-beta.1 --body
    # The tag a release is compared with (version order, exact tag names):
    uv run python scripts/release_notes.py --previous-tag v0.4.0
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 -- runs git with fixed arguments, no shell
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packaging.version import InvalidVersion, Version  # noqa: E402

from scripts.contract_schema_snapshot import (  # noqa: E402
    _format_changes,
    diff_fields,
    overall_bump,
)
from src.report.escaping import md_cell  # noqa: E402

ROUTING_SNAPSHOT = "tests/e2e/baselines/routing_snapshot.json"
CONTRACT_SNAPSHOT = "tests/contract/schema_snapshot.json"
PUBLIC_CONTRACT_SNAPSHOT = "tests/contract/assess_released_snapshot.json"
RULE_DIR = "src/catalog"
ATTACHMENT = "release-changes.md"

_NONE = "(none)"
_STATUS = {"A": "added", "M": "changed", "D": "removed", "T": "changed"}


# --------------------------------------------------------------------------- routing


def _score(value: Any) -> int | None:
    """A snapshot score: an int, or a gated engine's ``{"used": n, ...}``."""
    if isinstance(value, dict):
        value = value.get("used")
    return value if isinstance(value, int) else None


def final_placement(query: Mapping[str, Any]) -> tuple[str | None, int | None, str | None]:
    """``(engine, score, reason_code)`` a snapshot query ends with.

    The engine is the reality check's pick when it moved the query (``r``), else the
    resolver's (``f``); the score is the reality check's parsed fit for a moved query,
    else the score routing used for that engine; the reason code is the last Move's
    (the reality check's own Moves first)."""
    final = query.get("r") or query.get("f") or {}
    engine = final.get("e")
    score = final["fit"] if "fit" in final else _score((query.get("s") or {}).get(engine))
    moves = query.get("rmv") or query.get("mv") or []
    reason = moves[-1].rsplit(":", 1)[-1] if moves else None
    return engine, score, reason


def routing_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per query whose engine, score or reason code differs, sorted by sample
    and query id: ``{"sample", "query_id", "engine", "score", "reason_code"}``, each of
    the last three a ``(before, after)`` pair (``None`` where the query is absent)."""
    rows: list[dict[str, Any]] = []
    b_samples, a_samples = before.get("samples") or {}, after.get("samples") or {}
    for sample in sorted(set(b_samples) | set(a_samples)):
        b_queries = (b_samples.get(sample) or {}).get("queries") or {}
        a_queries = (a_samples.get(sample) or {}).get("queries") or {}
        for qid in sorted(set(b_queries) | set(a_queries)):
            b = final_placement(b_queries[qid]) if qid in b_queries else (None, None, None)
            a = final_placement(a_queries[qid]) if qid in a_queries else (None, None, None)
            if b == a and qid in b_queries and qid in a_queries:
                continue
            rows.append(
                {
                    "sample": sample,
                    "query_id": qid,
                    "engine": (b[0], a[0]),
                    "score": (b[1], a[1]),
                    "reason_code": (b[2], a[2]),
                }
            )
    return rows


# --------------------------------------------------------------------------- changelog


def contract_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """One line per contract whose version moved, or that was added or removed, between
    two snapshots of ``{key: {"version", "schema_hash", "fields"}}``."""
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b is None and a is not None:
            lines.append(f"Contract `{key}` added at {a['version']}")
        elif a is None and b is not None:
            lines.append(f"Contract `{key}` removed (was {b['version']})")
        elif a is not None and b is not None and b["version"] != a["version"]:
            changes = [
                c for c in diff_fields(b["fields"], a["fields"]) if c[0] != "contract_version"
            ]
            detail = _format_changes(changes) if changes else "no field change"
            kind = overall_bump(changes).upper() if changes else "version only"
            lines.append(f"Contract `{key}` {b['version']} -> {a['version']} ({kind}): {detail}")
    return lines


def rule_changes(
    changed: Sequence[tuple[str, str]], subjects: Mapping[str, list[str]]
) -> list[str]:
    """One line per rule catalog file added, changed or removed (``changed`` is git's
    ``--name-status``), with the subjects of the commits that touched it."""
    lines = []
    prefix = RULE_DIR + "/"
    for status, path in sorted(changed, key=lambda item: item[1]):
        name = path.removeprefix(prefix)
        what = _STATUS.get(status[:1], "changed")
        touched = "; ".join(" ".join(s.split()) for s in subjects.get(path, []))
        lines.append(f"Rule `{name}` {what}" + (f": {touched}" if touched else ""))
    return lines


# --------------------------------------------------------------------------- rendering

#: The release body's caps (GitHub refuses a release body over 125,000 characters); the
#: attachment carries everything.
BODY_ROWS = 50
BODY_CHANGELOG_LINES = 40
BODY_LINE_CHARS = 400
BODY_LIMIT = 100_000


def is_score_only(row: Mapping[str, Any]) -> bool:
    """Same engine and reason code on both sides; only the score on that engine moved."""
    engine, reason = row["engine"], row["reason_code"]
    return engine[0] is not None and engine[0] == engine[1] and reason[0] == reason[1]


def _pair(values: tuple[Any, Any]) -> str:
    before, after = (md_cell(_NONE if v is None else v) for v in values)
    return before if before == after else f"{before} -> {after}"


def _table(rows: list[dict[str, Any]]) -> list[str]:
    lines = ["| Sample | Query | Engine | Score | Reason code |", "|---|---|---|---|---|"]
    lines.extend(
        f"| {md_cell(row['sample'])} | {md_cell(row['query_id'])} | "
        f"{_pair(row['engine'])} | {_pair(row['score'])} | {_pair(row['reason_code'])} |"
        for row in rows
    )
    return lines


def _per_sample(rows: list[dict[str, Any]]) -> str:
    return ", ".join(f"{s}: {n}" for s, n in sorted(Counter(r["sample"] for r in rows).items()))


def score_drift(rows: list[dict[str, Any]]) -> str:
    """Score-only rows as one line: per sample, and by score change."""
    deltas: Counter[str] = Counter()
    for row in rows:
        before, after = row["score"]
        if isinstance(before, int) and isinstance(after, int):
            deltas[f"{after - before:+d}"] += 1
        else:
            deltas["n/a"] += 1
    order = sorted(deltas, key=lambda d: (d == "n/a", int(d) if d != "n/a" else 0))
    by_delta = ", ".join(f"{d}: {deltas[d]}" for d in order)
    return f"by sample {_per_sample(rows)}; by score change {by_delta}"


def _shorten(line: str, limit: int) -> str:
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def render(
    since: str,
    until: str,
    rows: list[dict[str, Any]],
    changelog: list[str],
    body: bool = False,
    max_rows: int = BODY_ROWS,
    max_changelog: int = BODY_CHANGELOG_LINES,
) -> str:
    """The Markdown section: engine changes first, then reason-code-only changes (each by
    sample, then query), then score-only drift, then the changelog lines.

    The attachment (``body=False``) lists every row and line. The release body
    (``body=True``) lists at most ``max_rows`` engine or reason-code changes, summarises
    score-only drift instead of listing it, keeps ``max_changelog`` changelog lines of at
    most ``BODY_LINE_CHARS`` characters, and stays under ``BODY_LIMIT`` characters; each
    cut points at the attachment."""
    engine = [r for r in rows if r["engine"][0] != r["engine"][1]]
    reason = [r for r in rows if r["engine"][0] == r["engine"][1] and not is_score_only(r)]
    placement = engine + reason
    drift = [r for r in rows if is_score_only(r)]
    more = f"in the attached {ATTACHMENT}"
    lines = [f"## Routing and contract changes since {since}", ""]
    lines.append(
        f"### Routing snapshot diff ({since}..{until}): {len(engine)} engine changes, "
        f"{len(reason)} reason-code-only changes, {len(drift)} score-only changes"
    )
    lines.append("")
    if not rows:
        lines.append("No query moved.")
    else:
        lines.append(
            f"Per query, from `{ROUTING_SNAPSHOT}`: the final engine, its score on that engine "
            "and the reason code of the query's last Move."
        )
    if placement:
        lines.extend(["", f"#### Engine and reason-code changes ({_per_sample(placement)})", ""])
        lines.append("Engine changes first, then changes of the reason code alone.")
        lines.append("")
        shown = placement[:max_rows] if body else placement
        lines.extend(_table(shown))
        if len(shown) < len(placement):
            lines.extend(["", f"... and {len(placement) - len(shown)} more {more}."])
    if drift:
        lines.extend(["", "#### Score-only changes", ""])
        lines.append(
            f"{len(drift)} queries keep their engine and reason code; only their score moved: "
            f"{score_drift(drift)}."
        )
        if body:
            lines.append(f"Every row is {more}.")
        else:
            lines.extend(["", *_table(drift)])
    lines.extend(["", "### Rule and contract changes", ""])
    if not changelog:
        lines.append("No rule or contract changes.")
    elif body:
        lines.extend(f"- {_shorten(line, BODY_LINE_CHARS)}" for line in changelog[:max_changelog])
        if len(changelog) > max_changelog:
            lines.extend(["", f"... and {len(changelog) - max_changelog} more lines {more}."])
    else:
        lines.extend(f"- {' '.join(line.split())}" for line in changelog)
    text = "\n".join(lines) + "\n"
    if body and len(text) > BODY_LIMIT:
        tail = f"\n... cut to fit the release body; everything is {more}.\n"
        text = text[: text.rfind("\n", 0, BODY_LIMIT - len(tail))] + tail
    return text


def previous_tag(tag: str, tags: list[str]) -> str | None:
    """The release ``tag`` is compared with: the highest version below its own among
    ``tags`` (PEP 440 order of ``v``-less names, so ``v0.3.0`` follows ``v0.3.0-beta.2``
    and a hotfix ``v1.5.1`` follows ``v1.5.0`` even after ``v1.6.0`` exists). Tags that
    are not versions are ignored; None when there is no earlier release."""
    current = _version(tag)
    if current is None:
        raise SystemExit(f"release_notes: {tag!r} is not a release version tag")
    earlier = [(v, t) for t in tags if (v := _version(t)) is not None and v < current]
    return max(earlier)[1] if earlier else None


def _version(tag: str) -> Version | None:
    if not tag.startswith("v"):
        return None
    try:
        return Version(tag[1:])
    except InvalidVersion:
        return None


# --------------------------------------------------------------------------- git


def _git(repo: Path, *args: str) -> str:
    # Fixed git subcommands; refs and paths are passed as separate arguments.
    done = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _verify_ref(repo: Path, ref: str) -> None:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise SystemExit(f"release_notes: unknown git ref {ref!r}") from None


def _json_at(repo: Path, ref: str, path: str) -> dict[str, Any]:
    """The JSON file at ``ref``, or ``{}`` when it does not exist there."""
    try:
        loaded: dict[str, Any] = json.loads(_git(repo, "show", f"{ref}:{path}"))
    except subprocess.CalledProcessError:
        return {}
    return loaded


def changelog_between(repo: Path, since: str, until: str) -> list[str]:
    status = _git(repo, "diff", "--name-status", "--no-renames", since, until, "--", RULE_DIR)
    changed = [
        (parts[0], parts[-1])
        for parts in (line.split("\t") for line in status.splitlines() if line.strip())
        if parts[-1].endswith(".toml")
    ]
    subjects = {
        path: _git(repo, "log", "--format=%s", f"{since}..{until}", "--", path).splitlines()
        for _, path in changed
    }
    contracts = contract_changes(
        _json_at(repo, since, CONTRACT_SNAPSHOT), _json_at(repo, until, CONTRACT_SNAPSHOT)
    )
    released = _json_at(repo, until, PUBLIC_CONTRACT_SNAPSHOT)
    public = contract_changes(
        _json_at(repo, since, PUBLIC_CONTRACT_SNAPSHOT).get("contracts", {}),
        released.get("contracts", {}),
    )
    return rule_changes(changed, subjects) + contracts + public + _unrecorded(repo, until, released)


def _unrecorded(repo: Path, until: str, released: Mapping[str, Any]) -> list[str]:
    """A note when ``until`` is a release tag the public contract was not recorded for
    (the release-prep step was skipped), so the next release compares with an older one."""
    if not released or not _git(repo, "tag", "--list", until).strip():
        return []
    recorded = released.get("released")
    if recorded == until:
        return []
    return [
        f"Note: the public `assess()` contract was not recorded for {until} (recorded: "
        f"{recorded or 'none'}). Record the shape {until} shipped, read from the tag "
        f"itself, in a pull request to main: uv run python "
        f"scripts/contract_schema_snapshot.py --write-released {until}"
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--since", help="the previous release tag")
    parser.add_argument("--until", default="HEAD", help="the release ref (default HEAD)")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    parser.add_argument(
        "--body", action="store_true", help="the release body's capped copy (see render())"
    )
    parser.add_argument("--max-rows", type=int, default=BODY_ROWS, help="with --body")
    parser.add_argument(
        "--previous-tag",
        metavar="TAG",
        help="print the tag release TAG is compared with (empty when none) and exit",
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.previous_tag is not None:
        _verify_ref(args.repo, f"refs/tags/{args.previous_tag}")
        tags = _git(args.repo, "tag", "--list", "v*").split()
        print(previous_tag(args.previous_tag, tags) or "")
        return 0
    if not args.since:
        parser.error("--since is required")
    for ref in (args.since, args.until):
        _verify_ref(args.repo, ref)
    rows = routing_changes(
        _json_at(args.repo, args.since, ROUTING_SNAPSHOT),
        _json_at(args.repo, args.until, ROUTING_SNAPSHOT),
    )
    text = render(
        args.since,
        args.until,
        rows,
        changelog_between(args.repo, args.since, args.until),
        body=args.body,
        max_rows=args.max_rows,
    )
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
