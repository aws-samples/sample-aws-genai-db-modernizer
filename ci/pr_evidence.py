"""The PR Evidence section: write it from the head commit, check it in CI (#1153).

Review kept finding PR bodies whose ``needs_llm_run`` verdict was wrong or whose
required smoke run had no result. ``ci/needs_llm_run.py`` computes the verdict; this
compares the PR body with it.

The marker the body must carry (outside HTML comments, code spans, fenced and
indented code blocks; a leading ``- `` or ``* `` is fine), one verdict line per run,
exactly as ``ci/needs_llm_run.sh`` prints it. Prose such as "e2e-llm not required" is
not read::

    e2e-llm: required
    bedrock-smoke: not required

and, for every run the diff requires, a result line that begins with ``pass`` right
after the colon, or is a ``MODERNIZE_RESULT: complete`` line::

    e2e-llm result: pass (judge 3.9, $3.74)
    e2e-llm result: MODERNIZE_RESULT: complete job_id=... db=... mode=both
    bedrock-smoke result: pass (errors: [])

A result line is rejected when it contains a negation or a future or tentative word
(see ``_REJECT``) anywhere, or a ``<...>`` placeholder. The check reads the form only;
a reviewer still checks that the excerpt is real.

Subcommands (``check`` needs only the standard library)::

    uv run python ci/pr_evidence.py render [--base REF] [--main-tests-line TEXT]
    python3 ci/pr_evidence.py check (--body-file FILE | --body-env VAR) [--base REF]
                                    [--author-env VAR]

``render`` prints the Evidence section from the head commit: the verdict lines, a
result placeholder per required run, the number of new and changed tests and their
node ids (``ci/main_tests.py changed``), and a routing drift line. It exits 2 when
``ci/main_tests.py`` fails, never printing an empty list. How many tests fail on main
comes from ``ci/pre_merge.sh --quick``'s main-tests line (pass it as
``--main-tests-line``). The routing drift line is ``none`` only when no changed path is
in the routing set (:func:`routing_files`, the ``src/assessment/ruleset.py`` import
closure plus the routing snapshot and sample fixtures); when the set cannot be computed
it says ``unknown`` whenever anything under ``src/`` or ``docs/examples/`` changed.
``check`` exits 1 when the body's verdict is missing or contradicts the diff against REF
(default ``HEAD^1``, the base of a pull request's merge checkout), or a required run
has no passing result line; the message says which lines to add. A pull request whose
author login (``--author-env``) ends in ``[bot]`` is skipped. Exit 2 when it could not
run. The body is read as data, never executed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 -- runs a sibling script with fixed argv
import sys
from pathlib import Path

try:
    from ci import needs_llm_run
except ImportError:  # run as a script: ci/ is sys.path[0]
    import needs_llm_run  # type: ignore[no-redef]

RUNS = tuple(needs_llm_run.RUNS)
REPO = Path(__file__).resolve().parents[1]
# Besides the ruleset closure: the routing snapshot harness and baselines, and the samples.
ROUTING_EXTRA: tuple[str, ...] = (
    "tests/e2e/routing_snapshot.py",
    "tests/e2e/routing_baseline.py",
    "tests/e2e/baselines/routing_*",
    "docs/examples/*",
)
# Without a computable set, a change under these means drift is unknown.
ROUTING_COARSE: tuple[str, ...] = ("src/*", "docs/examples/*")
DRIFT_COMMAND = "uv run pytest tests/e2e/test_routing_snapshot.py -p no:xdist -q"
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
_BULLET = r"^[ \t]*(?:[-*][ \t]+)?"
_RUN_ALT = "|".join(map(re.escape, RUNS))
_VERDICT = re.compile(
    _BULLET + r"(" + _RUN_ALT + r"):[ \t]*(not required|required)[ \t]*$", re.MULTILINE
)
_RESULT = re.compile(_BULLET + r"(" + _RUN_ALT + r") result:[ \t]*(?!<)(\S.*)$", re.MULTILINE)
_REJECT = re.compile(
    r"\b(?:not|never|yet|tbd|queued|expected|will|should|estimate\w*|pending|skipped|"
    r"fail\w*|unable|cannot)\b|n't",
    re.IGNORECASE,
)
_STARTS_PASS = re.compile(r"(?:pass\b|MODERNIZE_RESULT:[ \t]*complete\b)", re.IGNORECASE)
ACCEPTED_TEXT = (
    "'pass' right after the colon (for example 'pass (judge 3.9, $3.74)'), or a "
    "'MODERNIZE_RESULT: complete' line"
)


def routing_files() -> set[str]:
    """Repo-relative files that can move routing: the import closure and data of
    ``src/assessment/ruleset.py`` (a static walk, nothing imported from the pipeline).
    Raises on any failure so the caller can fall back to "unknown"."""
    sys.path.insert(0, str(REPO))
    try:
        from src.assessment import ruleset
    finally:
        sys.path.remove(str(REPO))
    root = Path(ruleset.__file__).resolve().parents[2]
    return {p.relative_to(root).as_posix() for p in ruleset.ruleset_files()}


def routing_drift_line(changed: list[str], files: set[str] | None) -> str:
    """The Evidence section's routing drift line; "none" only when nothing relevant changed."""
    if files is None:
        hits = needs_llm_run.matching(changed, ROUTING_COARSE)
        if hits:
            return (
                f"routing drift: unknown ({len(hits)} file(s) under src/ or docs/examples/ "
                f"changed, first: {hits[0]}); run `{DRIFT_COMMAND}` and paste the result."
            )
        return "routing drift: none (no file under src/ or docs/examples/ changed)"
    hits = [
        c
        for c in changed
        if c in files
        or needs_llm_run.matching([c], ROUTING_EXTRA)
        or (c.startswith("src/") and not (REPO / c).exists())  # deleted: not in the walk
    ]
    if hits:
        return (
            f"routing drift: {len(hits)} routing file(s) changed (first: {hits[0]}); "
            f"paste the output of `{DRIFT_COMMAND}`."
        )
    return "routing drift: none (no routing paths changed)"


def _visible(body: str) -> str:
    """The body as the checks read it: newlines normalised; comments, code spans, fenced
    and indented code removed."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    fence: str | None = None
    in_comment = False
    for line in text.split("\n"):
        if fence is not None:
            m = _FENCE.match(line)
            if m and m.group(1) == fence:
                fence = None
            continue
        if in_comment:
            end = line.find("-->")
            if end < 0:
                continue
            line, in_comment = line[end + 3 :], False
        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
            continue
        if _INDENTED.match(line):
            continue
        line = _CODE_SPAN.sub("", line)
        line = re.sub(r"<!--.*?-->", "", line)
        if "<!--" in line:
            line, in_comment = line.split("<!--", 1)[0], True
        out.append(line)
    return "\n".join(out)


def stated_verdicts(body: str) -> dict[str, set[str]]:
    """Each run's stated verdicts (``required``/``not required``), comments ignored."""
    out: dict[str, set[str]] = {}
    for run, state in _VERDICT.findall(_visible(body)):
        out.setdefault(run, set()).add(state)
    return out


def stated_results(body: str) -> dict[str, list[str]]:
    """Each run's non-placeholder result lines' text."""
    out: dict[str, list[str]] = {}
    for run, text in _RESULT.findall(_visible(body)):
        out.setdefault(run, []).append(text.strip())
    return out


def is_pass(run: str, text: str) -> bool:
    return bool(_STARTS_PASS.match(text)) and not _REJECT.search(text)


def check(body: str, verdict: dict[str, list[str]]) -> list[str]:
    """Problems with the body against the diff; empty when it agrees."""
    stated = stated_verdicts(body)
    results = stated_results(body)
    actual = {run: "required" if verdict.get(run) else "not required" for run in RUNS}
    errors: list[str] = []
    for run in RUNS:
        got = stated.get(run, set())
        if not got:
            errors.append(
                f"add the line '{run}: {actual[run]}' (own line, exactly as ci/needs_llm_run.sh "
                "prints it; prose is not read)"
            )
        elif got != {actual[run]}:
            errors.append(
                f"the body says '{run}: {' / '.join(sorted(got))}' but ci/needs_llm_run.sh "
                f"says '{run}: {actual[run]}'"
            )
        elif actual[run] == "required" and not any(is_pass(run, t) for t in results.get(run, [])):
            errors.append(
                f"'{run}' is required: add a line '{run} result: <...>' with "
                f"{ACCEPTED_TEXT}, with no negation or tentative word (not, TBD, queued, will, "
                "should, expected, estimate, pending, skipped, fail)"
            )
    return errors


def render(
    verdict: dict[str, list[str]],
    tests: list[str],
    changed: list[str],
    routing: set[str] | None = None,
    main_tests_line: str | None = None,
) -> str:
    lines = ["## Evidence", ""]
    for run in RUNS:
        lines.append(f"{run}: {'required' if verdict.get(run) else 'not required'}")
        if verdict.get(run):
            lines.append(f"{run} result: <paste the excerpt or outcome>")
    marked = sum(1 for t in tests if t.startswith("marked "))
    lines += ["", f"New and changed tests: {len(tests)} ({marked} marked passes_on_main)."]
    lines.append(
        main_tests_line
        or "On main's code: run ./ci/pre_merge.sh --quick and paste its main-tests line here."
    )
    lines += [""] + [f"- {t}" for t in tests] + [""]
    lines.append(routing_drift_line(changed, routing))
    return "\n".join(lines) + "\n"


def _changed_tests(base: str) -> list[str]:
    """New and changed tests; raises RuntimeError when ``ci/main_tests.py`` fails."""
    here = Path(__file__).resolve().parent
    res = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(here / "main_tests.py"), "changed", base],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"ci/main_tests.py changed {base} failed (exit {res.returncode}): "
            f"{res.stderr.strip()[-500:]}"
        )
    return [ln for ln in res.stdout.splitlines() if ln.startswith(("run ", "marked "))]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("render", "check"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="HEAD^1" if name == "check" else "origin/main")
        if name == "render":
            p.add_argument("--main-tests-line")
        else:
            src = p.add_mutually_exclusive_group(required=True)
            src.add_argument("--body-file")
            src.add_argument("--body-env")
            p.add_argument("--author-env")
    args = ap.parse_args(argv)
    if args.cmd == "check" and args.author_env:
        author = os.environ.get(args.author_env, "")
        if author.endswith("[bot]"):
            print(f"PR Evidence: skipped, the author {author} is a bot.")
            return 0
    try:
        changed = needs_llm_run.changed_paths(args.base)
    except needs_llm_run.DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdict = needs_llm_run.verdict(changed)
    if args.cmd == "render":
        try:
            tests = _changed_tests(args.base)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            routing: set[str] | None = routing_files()
        except Exception:  # noqa: BLE001 -- any failure means "unknown", never "none"
            routing = None
        print(render(verdict, tests, changed, routing, args.main_tests_line), end="")
        return 0
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        body = os.environ.get(args.body_env, "")
    errors = check(body, verdict)
    for err in errors:
        print(f"::error::PR Evidence: {err}")
    if errors:
        print("Fix: `uv run python ci/pr_evidence.py render` prints the section; see ci/README.md.")
        return 1
    print("PR Evidence agrees with ci/needs_llm_run.sh.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
