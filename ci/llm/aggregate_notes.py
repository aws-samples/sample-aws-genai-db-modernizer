#!/usr/bin/env python3
"""Aggregate recurring rubric-judge notes across saved e2e-llm runs.

Usage:
    uv run python ci/llm/aggregate_notes.py [PATH_OR_GLOB ...]
        [--min-runs N] [--threshold T] [--concerns-only] [--json]

Reads ``judge.json`` files (default: ``test-results/**/judge.json``; a
directory argument means ``<dir>/**/judge.json``, a file or glob is used as
given). Each criterion's note is split into sentences, and the sentences are
grouped by a deterministic token similarity (no model calls). Prints, per
criterion, the score mean and spread, then every group of notes that recurs
in at least ``--min-runs`` distinct runs (default 3), with the runs it came
from. A recurring note is a steadier signal than any single noisy score.

Similarity: lowercase, every number collapsed to the single token ``#``,
punctuation dropped, stopwords removed (negations such as "not" are kept as
tokens, and two sentences never group when the same content word is negated in
one and plain in the other, so "consistent" and "not consistent" stay apart), then the Jaccard index of the token sets must reach
``--threshold`` (default 0.5) against the first (longest) sentence of a group. Sentences are sorted
canonically before grouping, so the result does not depend on input order or
run names. Paraphrases below the threshold do not merge: counts are a lower
bound.
``--concerns-only`` drops sentences with no hedge/gap wording (mostly praise).

Structured defects: a judge.json written since #952 also carries ``defects[]``
(criterion, kind, where, quote). Those are grouped per criterion and kind by
the same similarity on the quote and listed after the notes, as a ranked
worklist; older files have none and contribute nothing there.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GLOB = "test-results/**/judge.json"
DEFAULT_MIN_RUNS = 3
DEFAULT_THRESHOLD = 0.5
MIN_TOKENS = 3  # shorter fragments carry no signal
NEGATIONS = frozenset({"not", "no", "never", "only", "without", "cannot", "nothing"})

_STOPWORDS = frozenset(
    """a an the and or but of to in on at by for with from as is are was were be been
    it its this that these those there which also so such than then
    into over per via can may might could would should will""".split()
)
# Words that mark a sentence as a concern rather than praise. Bare negations
# (not, no, only, without) are deliberately absent: they appear in praise
# ("contradicts nothing", "honestly stated as 'not provided'").
_CONCERN = re.compile(
    r"\b(but|however|though|although|minor|weak\w*|soft|somewhat|thin|stale|blemish\w*|"
    r"gaps?|ambigu\w*|tension|hedg\w*|could|cannot|can't|unclear|"
    r"missing|inconsisten\w*|partly|partial\w*|vague|generic|complicates?|persist\w*|"
    r"confus\w*|mismatch\w*|overstat\w*|lacks?|lacking)\b",
    re.IGNORECASE,
)
# A sentence ends at ". " before an uppercase letter or quote; decimals,
# "a.b" identifiers and "e.g." style fragments do not split.
_SENTENCE_END = re.compile(r"(?<=[a-z0-9)'\"\]])[.;!?]\s+(?=[A-Z\"'(])")
_TOKEN = re.compile(r"[a-z#][a-z#_]*")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def _ordered_tokens(sentence: str) -> list[str]:
    text = re.sub(r"\d+(?:[.,]\d+)*", "#", sentence.lower())
    return [t for t in _TOKEN.findall(text) if t not in _STOPWORDS and (len(t) > 1 or t == "#")]


def normalize(sentence: str) -> frozenset[str]:
    return frozenset(_ordered_tokens(sentence))


NEGATION_REACH = 2  # a negation flips the next this-many content words


def polarity(sentence: str) -> dict[str, bool]:
    """Map each content word to True when a negation sits just before it.

    A word seen both plain and negated in one sentence counts as plain.
    """
    out: dict[str, bool] = {}
    reach = 0
    for tok in _ordered_tokens(sentence):
        if tok in NEGATIONS:
            reach = NEGATION_REACH
            continue
        negated = reach > 0
        reach = max(reach - 1, 0)
        out[tok] = out.get(tok, True) and negated
    return out


def opposite_polarity(a: dict[str, bool], b: dict[str, bool]) -> bool:
    """True when some content word is negated in one sentence and plain in the other."""
    return any(a[w] != b[w] for w in a.keys() & b.keys())


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Group:
    criterion: str
    sample: str
    tokens: frozenset[str]
    polarity: dict[str, bool]
    members: list[tuple[str, str]] = field(default_factory=list)  # (run, sentence)

    @property
    def runs(self) -> list[str]:
        return sorted({run for run, _ in self.members})


def expand_paths(args: list[str]) -> tuple[list[Path], list[str]]:
    """Return the judge.json files (real paths, de-duplicated) and the explicit
    patterns that matched nothing at all."""
    patterns = args or [DEFAULT_GLOB]
    found: set[Path] = set()
    unmatched: list[str] = []
    for pat in patterns:
        p = Path(pat).expanduser()
        matched: set[Path] = set()
        candidates = [p] if p.exists() else [Path(m) for m in glob.glob(str(p), recursive=True)]
        for c in candidates:
            if c.is_dir():
                matched.update(c.glob("**/judge.json"))
            elif c.is_file():
                matched.add(c)
        if not matched and args:
            unmatched.append(pat)
        found.update(m.resolve() for m in matched)
    return sorted(found), unmatched


def _display(path: Path) -> str:
    """A path relative to the working directory, or with the home directory as ``~``."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        pass
    try:
        return "~/" + str(resolved.relative_to(Path.home().resolve()))
    except ValueError:
        return str(resolved)


def load_runs(paths: list[Path]) -> tuple[dict[str, dict], list[str]]:
    """Return ``{run_label: judge_dict}`` and a list of skipped-file warnings."""
    runs: dict[str, dict] = {}
    warnings: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"{_display(path)}: unreadable ({exc.__class__.__name__})")
            continue
        try:
            data = json.loads(text)
        except ValueError:
            warnings.append(f"{_display(path)}: not valid JSON, skipped")
            continue
        if isinstance(data, dict) and "error" in data and "notes" not in data:
            warnings.append(f"{_display(path)}: judge error, skipped")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
            warnings.append(f"{_display(path)}: no 'notes' object, skipped")
            continue
        runs[str(path.parent.resolve())] = data
    return _shorten_labels(runs), warnings


def _shorten_labels(runs: dict[str, dict]) -> dict[str, dict]:
    """Label runs by the directory part that differs; a lone run by its cwd/~ path."""
    if not runs:
        return runs
    if len(runs) == 1:
        ((label, data),) = runs.items()
        return {_display(Path(label)): data}
    try:
        base = os.path.commonpath(list(runs))
    except ValueError:
        return runs
    return {os.path.relpath(label, base): data for label, data in runs.items()}


def score_stats(runs: dict[str, dict]) -> dict[str, dict]:
    by_crit: dict[str, list[float]] = {}
    for data in runs.values():
        scores = data.get("scores")
        if isinstance(scores, dict):
            for crit, val in scores.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    by_crit.setdefault(crit, []).append(float(val))
    return {
        crit: {
            "n": len(vals),
            "mean": round(statistics.fmean(vals), 3),
            "min": min(vals),
            "max": max(vals),
            "stdev": round(statistics.pstdev(vals), 3),
        }
        for crit, vals in sorted(by_crit.items())
    }


def group_notes(
    runs: dict[str, dict], threshold: float = DEFAULT_THRESHOLD, concerns_only: bool = False
) -> list[Group]:
    """Group similar sentences per criterion, independent of input order.

    Sentences are sorted (longest first, then alphabetically) before the greedy
    pass, so the seed of each group, and therefore the counts, do not depend on
    run names or the order runs were read.
    """
    entries: list[tuple[str, frozenset[str], str, str]] = []  # (crit, tokens, sentence, run)
    for run, data in runs.items():
        notes = data.get("notes", {})
        for crit, text in notes.items():
            if not isinstance(text, str):
                continue
            for sentence in split_sentences(text):
                tokens = normalize(sentence)
                if len(tokens) < MIN_TOKENS:
                    continue
                if concerns_only and not _CONCERN.search(sentence):
                    continue
                entries.append((crit, tokens, sentence, run))
    entries.sort(key=lambda e: (e[0], -len(e[1]), e[2], e[3]))

    groups: list[Group] = []
    for crit, tokens, sentence, run in entries:
        best, best_sim = None, 0.0
        pol = polarity(sentence)
        for g in groups:
            if g.criterion != crit:
                continue
            sim = similarity(tokens, g.tokens)
            if sim > best_sim and not opposite_polarity(pol, g.polarity):
                best, best_sim = g, sim
        if best is not None and best_sim >= threshold:
            best.members.append((run, sentence))
        else:
            groups.append(Group(crit, sentence, tokens, pol, [(run, sentence)]))
    return groups


def group_defects(
    runs: dict[str, dict], threshold: float = DEFAULT_THRESHOLD
) -> list[tuple[Group, str]]:
    """Group the structured ``defects[]`` entries (#952) the same way as notes:
    per ``criterion/kind``, by token similarity of the quote (or of ``where``
    when the quote is empty), with the polarity guard. A judge.json without
    ``defects`` (saved before #952) contributes nothing; malformed entries are
    skipped. Returns ``(group, where)`` pairs, ``where`` from the group's seed."""
    entries: list[tuple[str, frozenset[str], str, str, str]] = (
        []
    )  # (key, tokens, quote, where, run)
    for run, data in runs.items():
        defects = data.get("defects")
        if not isinstance(defects, list):
            continue
        for d in defects:
            if not isinstance(d, dict) or not d.get("criterion") or not d.get("kind"):
                continue
            quote = str(d.get("quote") or "").strip()
            where = str(d.get("where") or "").strip()
            tokens = normalize(quote or where)
            if not tokens:
                continue
            entries.append((f"{d['criterion']}/{d['kind']}", tokens, quote or where, where, run))
    entries.sort(key=lambda e: (e[0], -len(e[1]), e[2], e[4]))
    groups: list[Group] = []
    wheres: list[str] = []
    for key, tokens, quote, where, run in entries:
        pol = polarity(quote)
        best, best_sim = None, 0.0
        for g in groups:
            if g.criterion != key:
                continue
            sim = similarity(tokens, g.tokens)
            if sim > best_sim and not opposite_polarity(pol, g.polarity):
                best, best_sim = g, sim
        if best is not None and best_sim >= threshold:
            best.members.append((run, quote))
        else:
            groups.append(Group(key, quote, tokens, pol, [(run, quote)]))
            wheres.append(where)
    return list(zip(groups, wheres, strict=True))


def aggregate(
    runs: dict[str, dict],
    min_runs: int = DEFAULT_MIN_RUNS,
    threshold: float = DEFAULT_THRESHOLD,
    concerns_only: bool = False,
) -> dict:
    defects = [(g, w) for g, w in group_defects(runs, threshold) if len(g.runs) >= min_runs]
    defects.sort(key=lambda gw: (-len(gw[0].runs), gw[0].criterion, gw[0].sample))
    groups = group_notes(runs, threshold, concerns_only)
    recurring = [g for g in groups if len(g.runs) >= min_runs]
    recurring.sort(key=lambda g: (-len(g.runs), g.criterion, g.sample))
    return {
        "runs": len(runs),
        "min_runs": min_runs,
        "threshold": threshold,
        "concerns_only": concerns_only,
        "scores": score_stats(runs),
        "recurring": [
            {
                "criterion": g.criterion,
                "run_count": len(g.runs),
                "note": g.sample,
                "runs": g.runs,
            }
            for g in recurring
        ],
        "recurring_defects": [
            {
                "criterion": g.criterion.split("/", 1)[0],
                "kind": g.criterion.split("/", 1)[1],
                "run_count": len(g.runs),
                "quote": g.sample,
                "where": where,
                "runs": g.runs,
            }
            for g, where in defects
        ],
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def render_text(result: dict, warnings: list[str], max_runs_listed: int = 6) -> str:
    out = [
        f"Judge notes across {_plural(result['runs'], 'run')}",
        "",
        "Scores (mean, min-max, stdev):",
    ]
    for crit, s in result["scores"].items():
        out.append(
            f"  {crit:<18} mean {s['mean']:.2f}  range {s['min']:g}-{s['max']:g}  "
            f"stdev {s['stdev']:.2f}  (n={s['n']})"
        )
    out += ["", f"Notes recurring in at least {_plural(result['min_runs'], 'run')}:"]
    if not result["recurring"]:
        out.append("  none")
    for item in result["recurring"]:
        out.append(
            f"\n[{item['criterion']}] in {_plural(item['run_count'], 'run')}: {item['note']}"
        )
        out.extend(f"    - {run}" for run in item["runs"][:max_runs_listed])
        hidden = item["run_count"] - max_runs_listed
        if hidden > 0:
            out.append(f"    ... and {hidden} more (use --json for the full list)")
    out += ["", f"Defects recurring in at least {_plural(result['min_runs'], 'run')}:"]
    if not result.get("recurring_defects"):
        out.append("  none")
    for item in result.get("recurring_defects") or []:
        where = f" ({item['where']})" if item["where"] and item["where"] != item["quote"] else ""
        out.append(
            f"\n[{item['criterion']}/{item['kind']}] in {_plural(item['run_count'], 'run')}: "
            f"{item['quote']}{where}"
        )
        out.extend(f"    - {run}" for run in item["runs"][:max_runs_listed])
        hidden = item["run_count"] - max_runs_listed
        if hidden > 0:
            out.append(f"    ... and {hidden} more (use --json for the full list)")
    out += [f"\nwarning: {w}" for w in warnings]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "paths", nargs="*", help=f"judge.json files, dirs or globs (default {DEFAULT_GLOB})"
    )
    ap.add_argument("--min-runs", type=int, default=DEFAULT_MIN_RUNS)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument(
        "--concerns-only",
        action="store_true",
        help="keep only sentences that read as a concern (hedge, gap, weakness), not praise",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.min_runs < 1:
        ap.error("--min-runs must be at least 1")
    if not 0 < args.threshold <= 1:
        ap.error("--threshold must be greater than 0 and at most 1")

    if not args.paths and not Path("test-results").is_dir():
        print(
            "error: test-results/ not found. Run `make e2e-llm` to produce runs, "
            "or pass saved run folders (directories, files or globs).",
            file=sys.stderr,
        )
        return 1
    paths, unmatched = expand_paths(args.paths)
    if unmatched:
        for pat in unmatched:
            print(f"error: no judge.json matches {pat}", file=sys.stderr)
        return 2
    runs, warnings = load_runs(paths)
    if not runs:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(
            "no usable judge.json files found" if paths else "no judge.json files found",
            file=sys.stderr,
        )
        return 1
    result = aggregate(runs, args.min_runs, args.threshold, args.concerns_only)
    if args.json:
        result["warnings"] = warnings
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result, warnings), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
