# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

A required run with no result line yet is *pending*: ``check`` prints a ``::warning::``
and passes, because the line cannot exist before the run finishes (#1850). A result line
that is present but empty, a ``<...>`` placeholder, or contains a negation or a future or
tentative word (see ``_REJECT``) anywhere, is an error. So is a line that names a result
for the run but is not written as exactly ``<run> result: ...`` on its own line (other
casing, ``-`` instead of ``:``, bold, a code span, a blockquote): it is never read as
"no result yet". Two result lines for one run that disagree (a failing one and a passing
one) are an error, as two conflicting verdict lines are. The check reads the form only;
a reviewer still checks that the excerpt is real. ``--require-results`` turns pending
into an error: the workflow's separate "PR Evidence results" job passes it in the merge
queue, so a neutral PR check never lets a PR merge without its result.

Subcommands (``check`` needs only the standard library)::

    uv run python ci/pr_evidence.py render [--base REF] [--main-tests-line TEXT]
    python3 ci/pr_evidence.py check (--body-file FILE | --body-env VAR | --queue-ref-env VAR)
                                    [--base REF] [--author-env VAR] [--require-results]

``render`` prints the Evidence section from the head commit: the verdict lines, a
comment per required run saying to add its result line when the run finishes, the number of new and changed tests and their
node ids (``ci/main_tests.py changed``), and a routing drift line. It exits 2 when
``ci/main_tests.py`` fails, never printing an empty list. How many tests fail on main
comes from ``ci/pre_merge.sh --quick``'s main-tests line (pass it as
``--main-tests-line``). The routing drift line is ``none`` only when no changed path is
in the routing set (:func:`routing_files`, the ``src/assessment/ruleset.py`` import
closure plus the routing snapshot and sample fixtures); when the set cannot be computed
it says ``unknown`` whenever anything under ``src/`` or ``docs/examples/`` changed.
``check`` exits 1 when the body's verdict is missing or contradicts the diff against REF
(default ``HEAD^1``, the base of a pull request's merge checkout), or a required run
has a result line that is not a pass; the message says which lines to add. With
``--require-results`` a required run with no result line fails too. A pull request is
skipped only when its author is a known dependency bot (``dependabot[bot]``) and every
commit since REF is that bot's own work as GitHub reports it (author the bot, committer the
bot or GitHub's web-flow identity, signature verified, read through the API with the job's
token) with no merge commit among them. A human commit, a merge, or an API failure means the
pull request is checked. ``--queue-ref-env`` reads the body of the pull request named by a
merge queue ref (its ``--base`` must be the sha in that ref). Exit 2 when it could not
run. The body is read as data, never executed.
"""

from __future__ import annotations

import argparse
import json
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
_LIST_MARKER = r"(?:[-*+]|\d{1,9}[.)])[ \t]+"
_BULLET = r"^[ \t]*(?:" + _LIST_MARKER + r")?"
_LIST_ITEM = re.compile(r"^[ \t]*" + _LIST_MARKER)
_RUN_ALT = "|".join(map(re.escape, RUNS))
_VERDICT = re.compile(
    _BULLET + r"(" + _RUN_ALT + r"):[ \t]*(not required|required)[ \t]*$", re.MULTILINE
)
_RESULT = re.compile(_BULLET + r"(" + _RUN_ALT + r") result:[ \t]*(.*)$", re.MULTILINE)
_QUEUE_REF = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/.+/pr-(\d+)-(?P<sha>[0-9a-f]{40})$")
# A line that names a result for a run in any form: other casing, "-" for ":", bold, a code
# span, a blockquote or a heading. One that is not the exact form (_RESULT) is a near miss.
# It needs a separator after "result" (":", "=" or "-"), so prose that mentions the run's
# result ("e2e-llm result was green", "### e2e-llm result") is not read.
_LOOSE_RESULT = re.compile(
    r"^(?:[\s>*_`#+-]|\d{1,9}[.)](?=\s))*(" + _RUN_ALT + r")[\s*_`]*result[\s*_`]*[:=-]",
    re.IGNORECASE,
)
# Authors that may skip the check: dependency bots only, never "any [bot]".
SKIPPED_BOT_AUTHORS = ("dependabot[bot]",)
_BOT_EMAIL = re.compile(r"^(?:\d+\+)?dependabot\[bot\]@users\.noreply\.github\.com$")
# GitHub's web-flow identity: the committer of a bot's commits made through the API.
_GITHUB_COMMITTER_EMAIL = "noreply@github.com"
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


def _visible(body: str, keep_spans: bool = False) -> str:
    """The body as the checks read it: newlines normalised; comments, code spans, fenced
    and indented code removed (``keep_spans`` leaves the code spans in place)."""
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
        if _INDENTED.match(line) and not _LIST_ITEM.match(line):
            continue  # indented code; an indented list item (a nested bullet) is read
        if keep_spans:
            # a "<!--" inside a code span must not open a comment in this pass
            line = _CODE_SPAN.sub(lambda m: m.group(0).replace("<!--", "\x00"), line)
        else:
            line = _CODE_SPAN.sub("", line)
        line = re.sub(r"<!--.*?-->", "", line)
        if "<!--" in line:
            line, in_comment = line.split("<!--", 1)[0], True
        out.append(line.replace("\x00", "<!--"))
    return "\n".join(out)


def stated_verdicts(body: str) -> dict[str, set[str]]:
    """Each run's stated verdicts (``required``/``not required``), comments ignored."""
    out: dict[str, set[str]] = {}
    for run, state in _VERDICT.findall(_visible(body)):
        out.setdefault(run, set()).add(state)
    return out


def stated_results(body: str) -> dict[str, list[str]]:
    """Each run's result lines' text, as written (an empty or ``<...>`` one included)."""
    out: dict[str, list[str]] = {}
    for run, text in _RESULT.findall(_visible(body)):
        out.setdefault(run, []).append(text.strip())
    return out


def is_pass(run: str, text: str) -> bool:
    return bool(_STARTS_PASS.match(text)) and not _REJECT.search(text)


def near_miss_results(body: str) -> dict[str, list[str]]:
    """Lines that name a run's result but are not written as ``<run> result: ...``."""
    out: dict[str, list[str]] = {}
    for line in _visible(body, keep_spans=True).split("\n"):
        m = _LOOSE_RESULT.match(line)
        if m and not _RESULT.match(line):
            out.setdefault(m.group(1).lower(), []).append(line.strip())
    return out


def _is_placeholder(text: str) -> bool:
    return not text or text.startswith("<")


def pending_results(body: str, verdict: dict[str, list[str]]) -> list[str]:
    """Required runs whose verdict line is right but that have no result line yet.

    A result line that is present but empty, a ``<...>`` placeholder or not a pass is
    not pending: :func:`check` reports it as an error.
    """
    stated = stated_verdicts(body)
    results = stated_results(body)
    near = near_miss_results(body)
    return [
        run
        for run in RUNS
        if verdict.get(run)
        and stated.get(run) == {"required"}
        and not results.get(run)
        and not near.get(run)
    ]


def check(body: str, verdict: dict[str, list[str]], require_results: bool = False) -> list[str]:
    """Problems with the body against the diff; empty when it agrees.

    A required run with no result line yet is pending, not a problem, unless
    ``require_results`` is set (the merge queue's gate).
    """
    stated = stated_verdicts(body)
    results = stated_results(body)
    near = near_miss_results(body)
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
        elif actual[run] == "required" and near.get(run):
            errors.append(
                f"the line '{near[run][0]}' names a '{run}' result but is not written as "
                f"'{run} result: ...' (lower case, a colon, on its own line, no bold, code "
                f"span or quote): rewrite it with {ACCEPTED_TEXT}, or remove it until the "
                "run finishes"
            )
        elif actual[run] == "required" and len({is_pass(run, t) for t in results.get(run, [])}) > 1:
            errors.append(
                f"the '{run} result:' lines disagree (a passing one and one that is not): "
                "keep the one that reflects the latest run"
            )
        elif actual[run] == "required" and not any(is_pass(run, t) for t in results.get(run, [])):
            lines = results.get(run, [])
            if not lines:
                if require_results:
                    errors.append(
                        f"'{run}' is required and has no result yet: add a line "
                        f"'{run} result: ...' with {ACCEPTED_TEXT}"
                    )
                continue
            shown = lines[0] or "(empty)"
            why = (
                "a placeholder or empty" if any(_is_placeholder(t) for t in lines) else "not a pass"
            )
            errors.append(
                f"the '{run} result:' line is {why} ('{shown}'): use {ACCEPTED_TEXT}, with no "
                "negation or tentative word (not, TBD, queued, will, should, expected, "
                "estimate, pending, skipped, fail), or remove the line until the run finishes"
            )
    return errors


def pr_from_queue_ref(ref: str) -> int | None:
    """The PR number in a merge queue branch (``gh-readonly-queue/<base>/pr-<N>-<sha>``)."""
    m = _QUEUE_REF.match(ref.strip())
    return int(m.group(1)) if m else None


def queue_ref_sha(ref: str) -> str | None:
    """The base sha in a merge queue branch name."""
    m = _QUEUE_REF.match(ref.strip())
    return m.group("sha") if m else None


def _git_out(*args: str) -> tuple[int, str]:
    res = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *args], capture_output=True, text=True, check=False
    )
    return res.returncode, res.stdout


def _only_bot_commits(base: str) -> bool:
    """True when the pull request's commits are all the skipped bot's own verified work.

    No commit since ``base`` may be a merge commit (a merge can carry changes no author
    line shows), and every commit must pass :func:`_commit_is_verified_bot_work` (author the
    bot, committer the bot or GitHub, signature verified by GitHub, read with the job's
    token). On a pull request's merge checkout, ``HEAD`` is GitHub's own merge of the branch
    into the base: its second parent's side is read instead. In the merge queue this relies
    on the REBASE merge method, which leaves the entry as plain commits on the base.
    """
    rc_base, base_sha = _git_out("rev-parse", "--verify", "-q", base)
    rc_h1, head1 = _git_out("rev-parse", "--verify", "-q", "HEAD^1")
    rc_h2, _ = _git_out("rev-parse", "--verify", "-q", "HEAD^2")
    tip = "HEAD^2" if rc_h2 == 0 and rc_base == 0 and rc_h1 == 0 and base_sha == head1 else "HEAD"
    rc, merges = _git_out("rev-list", "--merges", f"{base}..{tip}")
    if rc != 0 or merges.strip():
        return False
    rc, out = _git_out("log", "--format=%H %ae", f"{base}..{tip}")
    rows = [ln.split() for ln in out.splitlines() if ln.strip()]
    return (
        rc == 0
        and bool(rows)
        and all(len(r) == 2 and _BOT_EMAIL.match(r[1]) for r in rows)
        and all(_commit_is_verified_bot_work(r[0]) for r in rows)
    )


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or ".." in repo:
        raise RuntimeError("GITHUB_REPOSITORY is not set to owner/name")
    return repo


def _commit_is_verified_bot_work(sha: str) -> bool:
    """Whether GitHub says this commit is the dependency bot's own, signed work.

    The author is the bot, the committer is the bot or GitHub's web-flow identity (what a
    bot's API commit really has), and GitHub reports the signature as verified: a local
    commit cannot forge that. Any API or parsing problem returns False (check normally).
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False
    try:
        res = subprocess.run(  # nosec B603 B607 -- fixed argv, validated repo and sha
            ["gh", "api", f"repos/{_repo()}/commits/{sha}"],
            capture_output=True,
            text=True,
            check=False,
        )
        commit = json.loads(res.stdout)["commit"]
        committer = commit["committer"]["email"]
        return bool(
            _BOT_EMAIL.match(commit["author"]["email"])
            and (_BOT_EMAIL.match(committer) or committer == _GITHUB_COMMITTER_EMAIL)
            and commit["verification"]["verified"] is True
        )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        return False


def _fetch_pr(pr: int) -> tuple[str, str]:
    """The PR's body and author login through ``gh api`` (``GH_TOKEN``, ``GITHUB_REPOSITORY``)."""
    repo = _repo()
    res = subprocess.run(  # nosec B603 B607 -- fixed argv, validated repo
        ["gh", "api", f"repos/{repo}/pulls/{pr}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"gh api pulls/{pr} failed: {res.stderr.strip()[-300:]}")
    data = json.loads(res.stdout)
    return data.get("body") or "", (data.get("user") or {}).get("login") or ""


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
            lines.append(
                f"<!-- add the line '{run} result: pass (...)' when the run finishes; "
                "until then the check reports it pending -->"
            )
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
            src.add_argument("--queue-ref-env", help="read the PR body of this merge queue ref")
            p.add_argument("--author-env")
            p.add_argument("--require-results", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "check" and args.author_env:
        author = os.environ.get(args.author_env, "")
        if author in SKIPPED_BOT_AUTHORS and _only_bot_commits(args.base):
            print(f"PR Evidence: skipped, the author {author} is a dependency bot.")
            return 0
    if args.cmd == "check" and args.queue_ref_env:
        ref_sha = queue_ref_sha(os.environ.get(args.queue_ref_env, ""))
        if ref_sha is None:
            print(f"error: no PR number in the queue ref ${args.queue_ref_env}", file=sys.stderr)
            return 2
        if args.base != ref_sha:
            print(
                f"error: --base {args.base} is not the base sha in the queue ref ({ref_sha}): "
                "the diff would not be this entry's own commits",
                file=sys.stderr,
            )
            return 2
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
    elif args.queue_ref_env:
        pr = pr_from_queue_ref(os.environ.get(args.queue_ref_env, ""))
        if pr is None:
            print(f"error: no PR number in the queue ref ${args.queue_ref_env}", file=sys.stderr)
            return 2
        try:
            body, author = _fetch_pr(pr)
        except (RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if author in SKIPPED_BOT_AUTHORS and _only_bot_commits(args.base):
            print(f"PR Evidence: skipped, the author {author} is a dependency bot.")
            return 0
    else:
        body = os.environ.get(args.body_env, "")
    errors = check(body, verdict, require_results=args.require_results)
    for err in errors:
        print(f"::error::PR Evidence: {err}")
    if errors:
        print("Fix: `uv run python ci/pr_evidence.py render` prints the section; see ci/README.md.")
        return 1
    for run in pending_results(body, verdict):
        print(
            f"::warning::PR Evidence: '{run}' is required and has no result yet (pending). "
            f"Add '{run} result: pass ...' when it finishes; the PR Evidence results job fails in the merge queue "
            "until that line exists."
        )
    print("PR Evidence agrees with ci/needs_llm_run.sh.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
