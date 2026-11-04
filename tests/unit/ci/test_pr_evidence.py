# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PR Evidence section is written from the diff and checked in CI (#1153)."""

from __future__ import annotations

import re
import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from ci import pr_evidence

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-evidence.yml"
BOTH_NO: dict[str, list[str]] = {"e2e-llm": [], "bedrock-smoke": []}
E2E: dict[str, list[str]] = {"e2e-llm": ["AGENTS.md"], "bedrock-smoke": []}
BOTH: dict[str, list[str]] = {"e2e-llm": ["x"], "bedrock-smoke": ["y"]}


def body(*lines: str) -> str:
    return "## Evidence\n\n" + "\n".join(lines) + "\n"


def test_agreeing_body_passes():
    assert (
        pr_evidence.check(body("e2e-llm: not required", "bedrock-smoke: not required"), BOTH_NO)
        == []
    )


def test_missing_verdict_fails():
    errs = pr_evidence.check(body("e2e-llm: not required"), BOTH_NO)
    assert len(errs) == 1 and "bedrock-smoke" in errs[0]


def test_contradicting_verdict_fails():
    errs = pr_evidence.check(body("e2e-llm: not required", "bedrock-smoke: not required"), E2E)
    assert len(errs) == 1 and "e2e-llm" in errs[0] and "required" in errs[0]


def test_stating_required_when_not_needed_fails():
    errs = pr_evidence.check(
        body(
            "e2e-llm: required",
            "e2e-llm result: MODERNIZE_RESULT: complete job_id=1",
            "bedrock-smoke: not required",
        ),
        BOTH_NO,
    )
    assert len(errs) == 1 and "e2e-llm" in errs[0]


def test_conflicting_lines_for_one_run_fail():
    errs = pr_evidence.check(
        body("e2e-llm: required", "e2e-llm: not required", "bedrock-smoke: not required"), E2E
    )
    assert any("e2e-llm" in e for e in errs)


def test_required_run_without_a_result_line_is_pending_not_an_error():
    text = body("e2e-llm: required", "bedrock-smoke: not required")
    assert pr_evidence.check(text, E2E) == []
    assert pr_evidence.pending_results(text, E2E) == ["e2e-llm"]


def test_require_results_turns_pending_into_an_error():
    text = body("e2e-llm: required", "bedrock-smoke: not required")
    errs = pr_evidence.check(text, E2E, require_results=True)
    assert len(errs) == 1 and "e2e-llm result" in errs[0]
    both = body("e2e-llm: required", "bedrock-smoke: required")
    assert len(pr_evidence.check(both, BOTH, require_results=True)) == 2


def test_nothing_is_pending_once_every_required_run_passes():
    text = body("e2e-llm: required", "e2e-llm result: pass", "bedrock-smoke: not required")
    assert pr_evidence.pending_results(text, E2E) == []
    assert pr_evidence.check(text, E2E, require_results=True) == []


def test_not_required_runs_are_never_pending():
    text = body("e2e-llm: not required", "bedrock-smoke: not required")
    assert pr_evidence.pending_results(text, BOTH_NO) == []


def test_a_contradiction_is_not_softened_by_a_missing_result():
    # the verdict is wrong AND the result is missing: still an error, and not pending
    text = body("e2e-llm: not required", "bedrock-smoke: not required")
    assert len(pr_evidence.check(text, E2E)) == 1
    assert pr_evidence.pending_results(text, E2E) == []


def test_one_passing_run_does_not_hide_the_other_pending_one():
    text = body(
        "e2e-llm: required",
        "e2e-llm result: pass",
        "bedrock-smoke: required",
    )
    assert pr_evidence.check(text, BOTH) == []
    assert pr_evidence.pending_results(text, BOTH) == ["bedrock-smoke"]


def test_result_line_satisfies_required_run():
    text = body(
        "- e2e-llm: required",
        "- e2e-llm result: MODERNIZE_RESULT: complete job_id=1 db=d mode=both",
        "bedrock-smoke: not required",
    )
    assert pr_evidence.check(text, E2E) == []


def test_placeholder_or_empty_result_is_an_error_not_pending():
    for res in (
        "e2e-llm result:",
        "e2e-llm result:   ",
        "e2e-llm result: <paste the excerpt or outcome>",
    ):
        text = body("e2e-llm: required", res, "bedrock-smoke: not required")
        errs = pr_evidence.check(text, E2E)
        assert len(errs) == 1 and "e2e-llm" in errs[0], res
        assert pr_evidence.pending_results(text, E2E) == [], res


def test_a_placeholder_does_not_hide_behind_a_passing_line_for_another_run():
    text = body(
        "e2e-llm: required",
        "e2e-llm result: pass",
        "bedrock-smoke: required",
        "bedrock-smoke result: <paste>",
    )
    errs = pr_evidence.check(text, BOTH)
    assert len(errs) == 1 and "bedrock-smoke" in errs[0] and "placeholder" in errs[0]


def test_both_required_each_needs_its_result():
    errs = pr_evidence.check(
        body(
            "e2e-llm: required",
            "e2e-llm result: MODERNIZE_RESULT: complete job_id=1",
            "bedrock-smoke: required",
        ),
        BOTH,
        require_results=True,
    )
    assert len(errs) == 1 and "bedrock-smoke" in errs[0]


def test_html_comments_are_ignored():
    text = "<!-- e2e-llm: not required\nbedrock-smoke: not required -->\n"
    assert len(pr_evidence.check(text, BOTH_NO)) == 2


def test_result_line_for_a_run_does_not_count_as_its_verdict():
    assert pr_evidence.stated_verdicts("e2e-llm result: required") == {}


def test_render_agrees_with_check():
    text = pr_evidence.render(E2E, ["run tests/unit/a.py::t"], ["AGENTS.md"], set())
    assert "- run tests/unit/a.py::t" in text
    assert "New and changed tests: 1 (0 marked passes_on_main)" in text
    # the rendered section has no result line yet: pending, never a red check
    assert pr_evidence.check(text, E2E) == []
    assert pr_evidence.pending_results(text, E2E) == ["e2e-llm"]
    assert "<" not in "".join(
        ln for ln in pr_evidence._visible(text).splitlines() if "result" in ln
    )
    filled = text + "e2e-llm result: pass, cost $4, judge 4.0\n"
    assert pr_evidence.pending_results(filled, E2E) == []
    assert pr_evidence.check(filled, E2E, require_results=True) == []


# Scratch repos get a fixed identity and no global or system config, so a runner without
# a git identity behaves like a developer machine.
GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}
# Placed before a call's own arguments, so a call's later "-c user.email=..." still wins.
GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


def _git(repo: Path, *args: str) -> None:
    res = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=repo, capture_output=True, text=True, env=GIT_ENV
    )  # nosec B603 B607
    assert res.returncode == 0, f"git {args}: {res.stderr}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "AGENTS.md").write_text("changed")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "head")
    return tmp_path


def _run(repo: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(ROOT / "ci" / "pr_evidence.py"), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={**GIT_ENV, **(env or {})},
    )


def test_cli_checks_the_body_from_an_env_var_against_the_diff(repo: Path):
    wrong = _run(
        repo,
        "check",
        "--body-env",
        "B",
        env={"B": body("e2e-llm: not required", "bedrock-smoke: not required")},
    )
    assert wrong.returncode == 1 and "e2e-llm" in wrong.stdout
    right = _run(
        repo,
        "check",
        "--body-env",
        "B",
        env={
            "B": body(
                "e2e-llm: required",
                "e2e-llm result: MODERNIZE_RESULT: complete job_id=1",
                "bedrock-smoke: not required",
            )
        },
    )
    assert right.returncode == 0, right.stdout


def test_cli_missing_result_warns_and_exits_0_unless_results_are_required(repo: Path):
    env = {"B": body("e2e-llm: required", "bedrock-smoke: not required")}
    soft = _run(repo, "check", "--body-env", "B", env=env)
    assert soft.returncode == 0, soft.stdout
    assert "::warning::" in soft.stdout and "e2e-llm" in soft.stdout
    assert "::error::" not in soft.stdout
    hard = _run(repo, "check", "--body-env", "B", "--require-results", env=env)
    assert hard.returncode == 1 and "::error::" in hard.stdout


@pytest.mark.passes_on_main
def test_cli_contradiction_still_fails_without_require_results(repo: Path):
    env = {"B": body("e2e-llm: not required", "bedrock-smoke: not required")}
    res = _run(repo, "check", "--body-env", "B", env=env)
    assert res.returncode == 1


@pytest.mark.passes_on_main
def test_cli_negative_result_text_still_fails_without_require_results(repo: Path):
    env = {
        "B": body("e2e-llm: required", "e2e-llm result: not yet run", "bedrock-smoke: not required")
    }
    res = _run(repo, "check", "--body-env", "B", env=env)
    assert res.returncode == 1


def test_queue_ref_gives_the_pr_number():
    sha = "a" * 40
    assert pr_evidence.pr_from_queue_ref(f"refs/heads/gh-readonly-queue/main/pr-123-{sha}") == 123
    assert pr_evidence.pr_from_queue_ref(f"gh-readonly-queue/feat/x/pr-7-{sha}") == 7
    for bad in (
        "",
        "refs/heads/main",
        f"refs/heads/evil/pr-5-{sha}",
        "gh-readonly-queue/main/pr-x-abc",
    ):
        assert pr_evidence.pr_from_queue_ref(bad) is None, bad


def test_cli_queue_ref_without_a_pr_number_exits_2(repo: Path):
    res = _run(
        repo, "check", "--queue-ref-env", "R", "--require-results", env={"R": "refs/heads/main"}
    )
    assert res.returncode == 2 and "no PR number" in res.stderr


def _base(repo: Path) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "rev-parse", "HEAD^1"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _queue(repo: Path, *extra: str, env: dict[str, str]):
    return _run(repo, "check", "--queue-ref-env", "R", "--base", _base(repo), *extra, env=env)


BOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"


def _commit_json(
    author: str = BOT_EMAIL, committer: str = "noreply@github.com", verified: bool = True
) -> str:
    """The shape of GitHub's commits API for a Dependabot commit made through the web-flow."""
    import json

    return json.dumps(
        {
            "commit": {
                "author": {"name": "dependabot[bot]", "email": author},
                "committer": {"name": "GitHub", "email": committer},
                "verification": {
                    "verified": verified,
                    "reason": "valid" if verified else "unsigned",
                },
            }
        }
    )


def _fake_gh(tmp: Path, payload: str, repo: Path, commit: str | None = None) -> dict[str, str]:
    """A ``gh`` that answers the commits API with ``commit`` (exit 1 when None), else the PR."""
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "pr.json").write_text(payload)
    if commit is not None:
        (bindir / "commit.json").write_text(commit)
    gh = bindir / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$*" in\n*/commits/*) cat "$(dirname "$0")/commit.json" || exit 1 ;;\n'
        '*) cat "$(dirname "$0")/pr.json" ;;\nesac\n'
    )
    gh.chmod(0o755)
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_REPOSITORY": "o/r",
        "R": f"refs/heads/gh-readonly-queue/main/pr-9-{_base(repo)}",
    }


def test_cli_queue_gate_blocks_a_pr_without_its_result(repo: Path, tmp_path: Path):
    import json

    pr = json.dumps(
        {"body": body("e2e-llm: required", "bedrock-smoke: not required"), "user": {"login": "me"}}
    )
    env = _fake_gh(tmp_path, pr, repo)
    res = _queue(repo, "--require-results", env=env)
    assert res.returncode == 1 and "e2e-llm" in res.stdout
    # the same body in the pull request job is only pending
    soft = _queue(repo, env=env)
    assert soft.returncode == 0 and "::warning::" in soft.stdout


def test_cli_queue_gate_passes_with_the_result(repo: Path, tmp_path: Path):
    import json

    ok = body("e2e-llm: required", "e2e-llm result: pass", "bedrock-smoke: not required")
    env = _fake_gh(tmp_path, json.dumps({"body": ok, "user": {"login": "me"}}), repo)
    assert _queue(repo, "--require-results", env=env).returncode == 0


def test_cli_queue_gate_skips_a_dependabot_pr_but_not_one_with_a_human_commit(
    repo: Path, tmp_path: Path
):
    import json

    payload = json.dumps({"body": "", "user": {"login": "dependabot[bot]"}})
    # the fixture's commits are human: a bot author alone does not skip
    env = _fake_gh(tmp_path, payload, repo)
    assert _queue(repo, "--require-results", env=env).returncode == 1
    bot_repo = tmp_path / "botrepo"
    bot_repo.mkdir()
    _bot_commits(bot_repo, human_on_top=False)
    env = _fake_gh(bot_repo, payload, bot_repo, commit=_commit_json())
    res = _queue(bot_repo, "--require-results", env=env)
    assert res.returncode == 0 and "dependency bot" in res.stdout
    unverified = tmp_path / "unverified"
    unverified.mkdir()
    env = _fake_gh(unverified, payload, bot_repo, commit=_commit_json(verified=False))
    assert _queue(bot_repo, "--require-results", env=env).returncode == 1


def test_cli_queue_gate_exits_2_when_the_base_is_not_the_queue_refs_sha(repo: Path, tmp_path: Path):
    import json

    env = _fake_gh(tmp_path, json.dumps({"body": "", "user": {"login": "me"}}), repo)
    res = _run(
        repo, "check", "--queue-ref-env", "R", "--base", "HEAD", "--require-results", env=env
    )
    assert res.returncode == 2 and "queue ref" in res.stderr


def test_cli_queue_gate_exits_2_when_the_api_fails(repo: Path, tmp_path: Path):
    env = _fake_gh(tmp_path, "not json", repo)
    res = _queue(repo, "--require-results", env=env)
    assert res.returncode == 2 and "Expecting value" in res.stderr and "Traceback" not in res.stderr


def test_cli_body_is_data_not_shell(repo: Path):
    evil = body("e2e-llm: required", "e2e-llm result: $(touch pwned)`touch pwned`")
    _run(repo, "check", "--body-env", "B", env={"B": evil})
    assert not (repo / "pwned").exists()


def test_cli_exit_2_when_the_diff_cannot_be_read(repo: Path):
    res = _run(repo, "check", "--body-env", "B", "--base", "nope", env={"B": ""})
    assert res.returncode == 2


def test_cli_render_prints_verdict_lines(repo: Path):
    res = _run(repo, "render", "--base", "HEAD^1")
    assert res.returncode == 0
    assert "e2e-llm: required" in res.stdout and "bedrock-smoke: not required" in res.stdout


def test_workflow_is_hardened_reads_the_body_from_env_and_reports_in_the_queue():
    wf = yaml.safe_load(WORKFLOW.read_text())
    triggers = wf.get("on") or wf[True]
    assert "edited" in triggers["pull_request"]["types"]
    assert "merge_group" in triggers
    assert "pull_request_target" not in triggers
    assert wf["permissions"] == {"contents": "read"}
    text = WORKFLOW.read_text()
    for uses in re.findall(r"uses:\s*(\S+)", text):
        assert re.search(r"@[0-9a-f]{40}$", uses), uses
    steps = wf["jobs"]["pr-evidence"]["steps"]
    run = next(s for s in steps if "env" in s)
    assert "github.event" not in run["run"], "the PR body must not be interpolated into the shell"
    assert "github.event.pull_request.body" in run["env"]["PR_BODY"]
    assert "secrets." not in text


def test_the_template_and_readme_document_the_marker():
    template = (ROOT / ".github" / "pull_request_template.md").read_text()
    assert "e2e-llm: " in template and "bedrock-smoke: " in template
    assert "result:" in template
    assert "pr_evidence" in (ROOT / "ci" / "README.md").read_text()
    assert "reported as pending" in template


def test_crlf_and_cr_bodies_parse_like_lf():
    for nl in ("\r\n", "\r"):
        text = nl.join(["e2e-llm: not required", "bedrock-smoke: not required", ""])
        assert pr_evidence.check(text, BOTH_NO) == [], repr(nl)


def test_cli_crlf_body_through_the_env_var(repo: Path):
    crlf = "e2e-llm: required\r\ne2e-llm result: MODERNIZE_RESULT: complete\r\nbedrock-smoke: not required\r\n"
    res = _run(repo, "check", "--body-env", "B", env={"B": crlf})
    assert res.returncode == 0, res.stdout


ADVERSARIAL = [
    "e2e-llm result: not yet run; will pass, cost ~$4",
    "e2e-llm result: did not pass (judge 1.2, cost $4)",
    "e2e-llm result: TBD, queued; should pass, cost estimate $4",
    "e2e-llm result: errors: [] expected, not yet run",
    "e2e-llm result: phase did not complete",
]


@pytest.mark.parametrize(
    "result",
    ADVERSARIAL
    + [
        "e2e-llm result: not run, next batch",
        "e2e-llm result: pending",
        "e2e-llm result: MODERNIZE_RESULT: failed phase=synthesis reason=x",
        "e2e-llm result: pass but 2 checks failed",
        "e2e-llm result: all good",
        "e2e-llm result: skipped, pass",
        "e2e-llm result: cost $4 pass",
        "e2e-llm result: passed? unsure",
    ],
)
def test_required_result_must_be_a_strict_pass(result: str):
    errs = pr_evidence.check(body("e2e-llm: required", result, "bedrock-smoke: not required"), E2E)
    assert len(errs) == 1 and "e2e-llm" in errs[0]


@pytest.mark.parametrize(
    "result",
    [
        "e2e-llm result: MODERNIZE_RESULT: complete job_id=1 db=d mode=both",
        "e2e-llm result: pass (judge 3.9, $3.74)",
        "e2e-llm result: pass",
        "e2e-llm result: pass (judge 3.9, no reroute, $3.74)",
    ],
)
def test_e2e_llm_accepted_forms(result: str):
    text = body("e2e-llm: required", result, "bedrock-smoke: not required")
    assert pr_evidence.check(text, E2E) == []


@pytest.mark.parametrize(
    "result", ["bedrock-smoke result: pass", "bedrock-smoke result: pass (errors: [])"]
)
def test_bedrock_smoke_accepted_forms(result: str):
    verdict: dict[str, list[str]] = {"e2e-llm": [], "bedrock-smoke": ["y"]}
    ok = body("e2e-llm: not required", "bedrock-smoke: required", result)
    assert pr_evidence.check(ok, verdict) == []
    for bad_result in (
        "bedrock-smoke result: failed",
        "bedrock-smoke result: errors: [] expected, not yet run",
        "bedrock-smoke result: phase did not complete",
    ):
        bad = body("e2e-llm: not required", "bedrock-smoke: required", bad_result)
        assert pr_evidence.check(bad, verdict), bad_result


def test_lines_in_an_unclosed_comment_or_a_fence_do_not_count():
    unclosed = "<!-- note\ne2e-llm: not required\nbedrock-smoke: not required\n"
    assert len(pr_evidence.check(unclosed, BOTH_NO)) == 2
    fenced = "```\ne2e-llm: not required\nbedrock-smoke: not required\n```\n"
    assert len(pr_evidence.check(fenced, BOTH_NO)) == 2
    after = fenced + "e2e-llm: not required\nbedrock-smoke: not required\n"
    assert pr_evidence.check(after, BOTH_NO) == []


def test_prose_verdict_gets_a_message_naming_the_lines_to_add():
    errs = pr_evidence.check("e2e-llm not required, bedrock-smoke not required", E2E)
    assert "add the line 'e2e-llm: required'" in errs[0]
    assert "add the line 'bedrock-smoke: not required'" in errs[1]


def _bot_commits(path: Path, human_on_top: bool) -> None:
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    (path / "AGENTS.md").write_text("bot change")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.email=49699333+dependabot[bot]@users.noreply.github.com",
        "-c",
        "user.name=dependabot[bot]",
        "commit",
        "-qm",
        "bump",
    )
    if human_on_top:
        (path / "AGENTS.md").write_text("human change")
        _git(path, "add", ".")
        _git(path, "commit", "-qm", "human")


def _bot_env(tmp_path: Path, repo: Path, commit: str | None, name: str = "ghdir") -> dict[str, str]:
    (tmp_path / name).mkdir()
    gh = _fake_gh(tmp_path / name, "{}", repo, commit=commit)
    return {"B": "x", "A": "dependabot[bot]", "PATH": gh["PATH"], "GITHUB_REPOSITORY": "o/r"}


def test_cli_skips_only_dependabot_with_only_dependabot_commits(tmp_path: Path):
    bot = tmp_path / "bot"
    bot.mkdir()
    _bot_commits(bot, human_on_top=False)
    env = _bot_env(tmp_path, bot, _commit_json())  # author bot, committer GitHub, verified
    skip = _run(bot, "check", "--body-env", "B", "--author-env", "A", env=env)
    assert skip.returncode == 0 and "dependency bot" in skip.stdout


def test_cli_checks_a_dependabot_pr_with_a_human_commit_on_top(tmp_path: Path):
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    _bot_commits(mixed, human_on_top=True)
    res = _run(
        mixed,
        "check",
        "--body-env",
        "B",
        "--author-env",
        "A",
        env={"B": "x", "A": "dependabot[bot]"},
    )
    assert res.returncode == 1


@pytest.mark.parametrize("author", ["github-actions[bot]", "some-app[bot]", "someone", ""])
def test_cli_checks_every_other_author(tmp_path: Path, author: str):
    bot = tmp_path / "bot"
    bot.mkdir()
    _bot_commits(bot, human_on_top=False)
    res = _run(bot, "check", "--body-env", "B", "--author-env", "A", env={"B": "x", "A": author})
    assert res.returncode == 1


def test_fetch_pr_rejects_a_bad_repository(monkeypatch: pytest.MonkeyPatch):
    for bad in ("", "norepo", "o/r; rm -rf x", "o/../x", "a/b/c", "o/.."):
        monkeypatch.setenv("GITHUB_REPOSITORY", bad)
        with pytest.raises(RuntimeError, match="owner/name"):
            pr_evidence._fetch_pr(9)


def test_render_fails_loudly_when_main_tests_fails(repo: Path):
    res = _run(repo, "render", "--base", "nope")
    assert res.returncode == 2 and "- none" not in res.stdout
    assert "main_tests" in _run_render_with_broken_main_tests(repo).stderr


def _run_render_with_broken_main_tests(repo: Path):
    (repo / "ci").mkdir()
    for name in ("pr_evidence.py", "needs_llm_run.py"):
        (repo / "ci" / name).write_text((ROOT / "ci" / name).read_text())
    (repo / "ci" / "main_tests.py").write_text(
        "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n"
    )
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, "ci/pr_evidence.py", "render", "--base", "HEAD^1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_routing_set_comes_from_the_ruleset_closure_and_covers_the_gaps():
    files = pr_evidence.routing_files()
    # in the closure but outside the old hand-kept list
    for path in ("src/tools/sql/facade.py", "src/agents/collector/offline_ingest.py"):
        assert path in files, path
    assert "src/agents/referee/query_scoring.py" in files
    assert any(f.startswith("src/catalog/") for f in files)


def test_drift_none_only_when_no_routing_file_changed():
    files = pr_evidence.routing_files()
    quiet = pr_evidence.routing_drift_line(["docs/x.md", "ci/pr_evidence.py"], files)
    assert quiet == "routing drift: none (no routing paths changed)"
    for path in (
        "src/agents/referee/query_scoring.py",
        "tests/e2e/routing_snapshot.py",
        "tests/e2e/baselines/routing_snapshot.json",
        "docs/examples/wordpress/queries.sql",
        "src/agents/collector/offline_ingest.py",
    ):
        line = pr_evidence.routing_drift_line([path], files)
        assert line.startswith("routing drift: 1 routing file(s) changed"), path
        assert "test_routing_snapshot.py" in line


def test_a_deleted_src_file_counts_as_routing():
    line = pr_evidence.routing_drift_line(["src/agents/referee/gone_module.py"], set())
    assert "1 routing file(s) changed" in line


def test_drift_is_unknown_when_the_set_cannot_be_computed():
    line = pr_evidence.routing_drift_line(["src/shared/x.py"], None)
    assert line.startswith("routing drift: unknown") and "test_routing_snapshot.py" in line
    line = pr_evidence.routing_drift_line(["docs/examples/wordpress/a.sql"], None)
    assert line.startswith("routing drift: unknown")
    assert "none" in pr_evidence.routing_drift_line(["docs/x.md", "ci/a.sh"], None)
    assert not pr_evidence.routing_drift_line(["docs/x.md"], None).startswith(
        "routing drift: unknown"
    )


def test_render_uses_the_routing_set():
    text = pr_evidence.render(BOTH_NO, [], ["src/tools/sql/facade.py"], {"src/tools/sql/facade.py"})
    assert "routing drift: 1 routing file(s) changed" in text
    assert "routing drift: unknown" in pr_evidence.render(BOTH_NO, [], ["src/a.py"], None)


def test_render_prints_the_main_tests_line_when_given():
    text = pr_evidence.render(
        BOTH_NO, ["run a::t"], [], set(), "main-tests: 1 new test, 1 fail on main"
    )
    assert "main-tests: 1 new test, 1 fail on main" in text


def test_pre_merge_runs_the_check_on_the_body_it_already_fetched():
    sh = (ROOT / "ci" / "pre_merge.sh").read_text()
    assert sh.count("gh pr view --json") == 1
    assert 'ci/pr_evidence.py check --body-file "$pr_body_file"' in sh
    assert (
        sh.index("helper adr") < sh.index("ci/pr_evidence.py check") < sh.index("pre_merge summary")
    )
    assert 'rm -f "$pr_body_file"' in sh.split("ci/pr_evidence.py check")[1]


def test_comment_marker_inside_code_is_text():
    ok = "e2e-llm: not required\nbedrock-smoke: not required\n"
    assert pr_evidence.check("Strip `<!--` from bodies.\n" + ok, BOTH_NO) == []
    assert pr_evidence.check("```\n<!--\n```\n" + ok, BOTH_NO) == []
    assert pr_evidence.check("<!-- hidden -->\n" + ok, BOTH_NO) == []
    assert (
        len(
            pr_evidence.check(
                "<!-- a\n  e2e-llm: not required\n-->\n" + ok.splitlines()[1], BOTH_NO
            )
        )
        == 1
    )


def test_indented_code_blocks_do_not_count():
    for indent in ("    ", "\t"):
        text = f"{indent}e2e-llm: not required\n{indent}bedrock-smoke: not required\n"
        assert len(pr_evidence.check(text, BOTH_NO)) == 2, repr(indent)


def test_every_workflow_step_but_the_noop_is_limited_to_pull_request():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pr-evidence"]["steps"]
    noop = [s for s in steps if s.get("if") == "github.event_name == 'merge_group'"]
    assert len(noop) == 1
    for step in steps:
        if step not in noop:
            assert step.get("if") == "github.event_name == 'pull_request'", step


def test_pull_request_job_gets_the_read_only_token_for_the_bot_skip():
    wf = yaml.safe_load(WORKFLOW.read_text())
    assert wf["permissions"] == {"contents": "read"}
    steps = wf["jobs"]["pr-evidence"]["steps"]
    run = next(s for s in steps if "env" in s)
    assert run["env"]["GH_TOKEN"] == "${{ github.token }}"


@pytest.mark.passes_on_main
def test_pull_request_job_does_not_require_results():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pr-evidence"]["steps"]
    run = next(s for s in steps if "env" in s)
    assert "--require-results" not in run["run"]


def test_results_gate_is_a_separate_job_that_only_enforces_in_the_merge_queue():
    wf = yaml.safe_load(WORKFLOW.read_text())
    assert set(wf["jobs"]) == {"pr-evidence", "pr-evidence-results"}
    job = wf["jobs"]["pr-evidence-results"]
    assert job["name"] == "PR Evidence results"
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    steps = job["steps"]
    enforcing = [s for s in steps if "--require-results" in s.get("run", "")]
    assert len(enforcing) == 1
    gate = enforcing[0]
    assert gate["if"] == "github.event_name == 'merge_group'"
    assert "--queue-ref-env" in gate["run"] and "github.event" not in gate["run"]
    assert "github.event.merge_group.base_sha" in gate["env"]["MERGE_GROUP_BASE"]
    # every other step is a no-op or checkout limited to the merge group
    for step in steps:
        if step is not gate:
            assert "--require-results" not in step.get("run", "")
    checkout = next(s for s in steps if "uses" in s)
    assert checkout["if"] == "github.event_name == 'merge_group'"
    assert checkout["with"]["fetch-depth"] == 0
    assert any(s.get("if") == "github.event_name == 'pull_request'" and "run" in s for s in steps)


def test_pre_merge_removes_the_body_file_in_its_cleanup_trap():
    sh = (ROOT / "ci" / "pre_merge.sh").read_text()
    cleanup = sh.split("cleanup() {")[1].split("}")[0]
    assert '"$pr_body_file"' in cleanup


NEAR_MISSES = [
    "E2E-LLM result: fail",
    "e2e-llm Result: fail",
    "e2e-llm result - fail",
    "e2e-llm result = fail",
    "**e2e-llm result:** fail",
    "**e2e-llm result:** pass",
    "__e2e-llm result__: fail",
    "`e2e-llm result: fail`",
    "> e2e-llm result: fail",
    "> - e2e-llm result: fail",
    "# e2e-llm result: fail",
    "  \u00a0e2e-llm result: fail",
]


@pytest.mark.parametrize("line", NEAR_MISSES)
def test_a_near_miss_result_line_is_an_error_never_pending(line: str):
    text = body("e2e-llm: required", line, "bedrock-smoke: not required")
    errs = pr_evidence.check(text, E2E)
    assert len(errs) == 1 and "e2e-llm" in errs[0] and "no result yet" not in errs[0], line
    assert pr_evidence.pending_results(text, E2E) == [], line
    assert pr_evidence.check(text, E2E, require_results=True) == errs, line


def test_near_misses_do_not_flag_prose_code_blocks_or_comments():
    ok = ["e2e-llm: required", "bedrock-smoke: not required"]
    for extra in (
        "The e2e-llm run is described in ci/README.md.",
        "```\ne2e-llm result: fail\n```",
        "    e2e-llm result: fail",
        "<!-- e2e-llm result: fail -->",
        "bedrock-smoke result: fail",  # a run the diff does not require
    ):
        text = body(*ok, extra)
        assert pr_evidence.near_miss_results(text).get("e2e-llm") is None, extra
        assert pr_evidence.check(text, E2E) == [], extra


def test_a_near_miss_beside_a_valid_pass_line_is_still_an_error():
    text = body(
        "e2e-llm: required",
        "e2e-llm result: pass",
        "e2e-llm result - fail",
        "bedrock-smoke: not required",
    )
    assert len(pr_evidence.check(text, E2E)) == 1


def test_a_valid_result_line_is_not_a_near_miss():
    text = body("e2e-llm: required", "- e2e-llm result: pass (judge 4.0)")
    assert pr_evidence.near_miss_results(text) == {}


def test_a_fail_line_then_a_pass_line_for_one_run_is_a_contradiction():
    for lines in (
        ("e2e-llm result: failed (judge 1.0)", "e2e-llm result: pass"),
        ("e2e-llm result: pass", "e2e-llm result: failed (judge 1.0)"),
        ("e2e-llm result: <paste>", "e2e-llm result: pass"),
    ):
        text = body("e2e-llm: required", *lines, "bedrock-smoke: not required")
        errs = pr_evidence.check(text, E2E)
        assert len(errs) == 1 and "disagree" in errs[0], lines


@pytest.mark.passes_on_main
def test_two_passing_lines_for_one_run_are_fine():
    text = body(
        "e2e-llm: required",
        "e2e-llm result: pass (chat)",
        "e2e-llm result: MODERNIZE_RESULT: complete job_id=1",
        "bedrock-smoke: not required",
    )
    assert pr_evidence.check(text, E2E) == []


def test_only_one_workflow_job_is_named_pr_evidence_results():
    # A second job with the required status's name could satisfy the ruleset in place of the gate.
    found: list[str] = []
    for wf_file in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        wf = yaml.safe_load(wf_file.read_text())
        for key, job in (wf.get("jobs") or {}).items():
            if "PR Evidence results" in (key, job.get("name")):
                found.append(f"{wf_file.name}:{key}")
    assert found == ["pr-evidence.yml:pr-evidence-results"]


def test_the_queue_gate_runs_the_bases_scripts_not_the_pull_requests():
    wf = yaml.safe_load(WORKFLOW.read_text())
    gate = next(
        s
        for s in wf["jobs"]["pr-evidence-results"]["steps"]
        if "--require-results" in s.get("run", "")
    )
    run = gate["run"]
    for script in ("ci/pr_evidence.py", "ci/needs_llm_run.py"):
        assert f'git show "$MERGE_GROUP_BASE:{script}"' in run, script
    assert 'python3 "$gate/pr_evidence.py"' in run
    assert "python3 ci/pr_evidence.py" not in run
    # the head's copy is used only while the base has no results job at all (bootstrap)
    assert '.github/workflows/pr-evidence.yml" 2>/dev/null' in run
    assert "grep -q '^  pr-evidence-results:'" in run
    assert "--require-results" not in run.split("else")[0].split("fi")[0]


PROSE_LINES = [
    pytest.param("### e2e-llm result", id="heading"),
    pytest.param("e2e-llm result was green on the second try.", id="was-green"),
    pytest.param("- e2e-llm result excerpt from results.json:", id="bullet-excerpt"),
    pytest.param("e2e-llm result.json uploaded", id="result-json"),
    pytest.param("The e2e-llm result is below.", id="is-below"),
]


@pytest.mark.parametrize("prose", PROSE_LINES)
def test_prose_about_a_result_never_outweighs_a_valid_result_line(prose: str):
    text = body(
        "e2e-llm: required",
        "e2e-llm result: pass (judge 3.9)",
        prose,
        "bedrock-smoke: not required",
    )
    assert pr_evidence.near_miss_results(text) == {}, prose
    assert pr_evidence.check(text, E2E, require_results=True) == [], prose
    # and prose alone is "no result yet", not an error
    alone = body("e2e-llm: required", prose, "bedrock-smoke: not required")
    assert pr_evidence.check(alone, E2E) == [], prose
    assert pr_evidence.pending_results(alone, E2E) == ["e2e-llm"], prose


LIST_FORMS = [
    pytest.param("{}", id="plain"),
    pytest.param("+ {}", id="plus"),
    pytest.param("1. {}", id="numbered-dot"),
    pytest.param("12) {}", id="numbered-paren"),
    pytest.param("- {}", id="dash"),
    pytest.param("* {}", id="star"),
    pytest.param("    - {}", id="nested-spaces"),
    pytest.param("\t- {}", id="nested-tab"),
    pytest.param("    1. {}", id="nested-numbered"),
]


@pytest.mark.parametrize("form", LIST_FORMS)
def test_list_forms_parse_for_results_and_verdicts(form: str):
    fail = body(
        form.format("e2e-llm: required"),
        form.format("e2e-llm result: fail"),
        form.format("bedrock-smoke: not required"),
    )
    errs = pr_evidence.check(fail, E2E)
    assert len(errs) == 1 and "no result yet" not in errs[0], form
    ok = body(
        form.format("e2e-llm: required"),
        form.format("e2e-llm result: pass"),
        form.format("bedrock-smoke: not required"),
    )
    assert pr_evidence.check(ok, E2E, require_results=True) == [], form


def test_a_four_space_list_item_is_read_but_plain_indented_code_is_not():
    code = body("e2e-llm: required", "    e2e-llm result: fail", "bedrock-smoke: not required")
    assert pr_evidence.check(code, E2E) == []
    nested = body("e2e-llm: required", "    - e2e-llm result: fail", "bedrock-smoke: not required")
    assert len(pr_evidence.check(nested, E2E)) == 1


def test_a_code_span_with_a_comment_opener_does_not_hide_later_near_misses():
    text = body(
        "e2e-llm: required",
        "Strip `<!--` from bodies.",
        "**e2e-llm result:** fail",
        "bedrock-smoke: not required",
    )
    errs = pr_evidence.check(text, E2E)
    assert len(errs) == 1 and "names a" in errs[0]
    assert pr_evidence.pending_results(text, E2E) == []
    commented = body(
        "e2e-llm: required", "<!-- **e2e-llm result:** fail -->", "bedrock-smoke: not required"
    )
    assert pr_evidence.check(commented, E2E) == []


def _commit(path: Path, msg: str, author: str, committer: str | None = None) -> None:
    name, email = author, f"{author}@users.noreply.github.com"
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": committer or name,
        "GIT_COMMITTER_EMAIL": f"{committer or author}@users.noreply.github.com",
    }
    subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "commit", "-qm", msg, "--allow-empty"],
        cwd=path,
        check=True,
        capture_output=True,
        env={**GIT_ENV, "HOME": tempfile.mkdtemp(), **env},
    )


BOT = "49699333+dependabot[bot]"


def _bot_history(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x")
    _git(path, "add", ".")
    _commit(path, "base", "human")


def test_a_merge_commit_in_a_dependabot_pr_disables_the_skip(tmp_path: Path):
    _bot_history(tmp_path)
    _git(tmp_path, "checkout", "-qb", "dependabot/x")
    (tmp_path / "AGENTS.md").write_text("bump")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "bump", BOT)
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "other.txt").write_text("o")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "main moves", "human")
    _git(tmp_path, "checkout", "-q", "dependabot/x")
    _git(tmp_path, "merge", "-q", "--no-ff", "--no-commit", "main")
    (tmp_path / "evil.md").write_text("hidden in the merge")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "merge main", BOT)  # even a merge made under the bot's name
    env = _bot_env(tmp_path, tmp_path, _commit_json())  # every commit looks verified
    res = _run(tmp_path, "check", "--body-env", "B", "--author-env", "A", "--base", "main", env=env)
    assert res.returncode == 1 and "skipped" not in res.stdout


@pytest.mark.parametrize(
    "commit, why",
    [
        pytest.param(_commit_json(verified=False), "unverified", id="unverified"),
        pytest.param(
            _commit_json(committer="human@example.com", verified=False),
            "forged-committer-unverified",
            id="forged-committer-unverified",
        ),
        pytest.param(
            _commit_json(committer="human@example.com", verified=True),
            "other-committer",
            id="other-committer-even-if-verified",
        ),
        pytest.param(_commit_json(author="human@example.com"), "other-author", id="other-author"),
        pytest.param("not json", "bad-json", id="bad-json"),
        pytest.param('{"commit": {}}', "missing-fields", id="missing-fields"),
        pytest.param(None, "api-error", id="api-error-fails-closed"),
    ],
)
def test_a_dependabot_pr_is_checked_unless_github_verifies_every_commit(
    tmp_path: Path, commit: str | None, why: str
):
    _bot_history(tmp_path)
    (tmp_path / "AGENTS.md").write_text("bump")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "bump", BOT, committer="human")  # a locally forged committer
    env = _bot_env(tmp_path, tmp_path, commit)
    res = _run(
        tmp_path, "check", "--body-env", "B", "--author-env", "A", "--base", "HEAD^1", env=env
    )
    assert res.returncode == 1 and "skipped" not in res.stdout, why


def test_the_real_shape_of_a_dependabot_commit_is_skipped(tmp_path: Path):
    # author dependabot, committer GitHub (web-flow), verified true, as in a real Dependabot PR;
    # the local committer is irrelevant, GitHub's answer decides
    _bot_history(tmp_path)
    (tmp_path / "AGENTS.md").write_text("bump")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "bump", BOT, committer="GitHub")
    for n, commit in enumerate((_commit_json(), _commit_json(committer=BOT_EMAIL))):
        env = _bot_env(tmp_path, tmp_path, commit, name=f"gh{n}")
        res = _run(
            tmp_path, "check", "--body-env", "B", "--author-env", "A", "--base", "HEAD^1", env=env
        )
        assert res.returncode == 0 and "dependency bot" in res.stdout


def test_a_dependabot_pr_on_githubs_merge_checkout_is_skipped(tmp_path: Path):
    # the pull_request checkout: HEAD is GitHub's merge of the branch into the base
    _bot_history(tmp_path)
    _git(tmp_path, "checkout", "-qb", "dependabot/x")
    (tmp_path / "AGENTS.md").write_text("bump")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "bump", BOT)
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "synthetic", "dependabot/x")
    env = _bot_env(tmp_path, tmp_path, _commit_json())
    res = _run(
        tmp_path, "check", "--body-env", "B", "--author-env", "A", "--base", "HEAD^1", env=env
    )
    assert res.returncode == 0 and "dependency bot" in res.stdout
    # a human commit on the branch, under the same synthetic merge, is checked
    _git(tmp_path, "reset", "-q", "--hard", "HEAD^1")
    _git(tmp_path, "checkout", "-q", "dependabot/x")
    (tmp_path / "AGENTS.md").write_text("human edit")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "human", "human")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "synthetic", "dependabot/x")
    res = _run(
        tmp_path, "check", "--body-env", "B", "--author-env", "A", "--base", "HEAD^1", env=env
    )
    assert res.returncode == 1


def _gate_run() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pr-evidence-results"]["steps"]
    return str(next(s for s in steps if "--require-results" in s.get("run", ""))["run"])


WORKFLOW_WITH_GATE = "jobs:\n  pr-evidence-results:\n    runs-on: x\n"
WORKFLOW_WITHOUT_GATE = "jobs:\n  pr-evidence:\n    runs-on: x\n"
STUB = "import pathlib, sys\npathlib.Path('stub-ran').write_text('x')\nsys.exit(0)\n"


def _gate_repo(path: Path, base_workflow: str, base_pr_evidence: str) -> str:
    """A base commit with a gate (or none) and a head commit whose scripts are stubs."""
    _git(path, "init", "-q", "-b", "main")
    (path / "ci").mkdir()
    (path / ".github" / "workflows").mkdir(parents=True)
    (path / ".github" / "workflows" / "pr-evidence.yml").write_text(base_workflow)
    (path / "ci" / "pr_evidence.py").write_text(base_pr_evidence)
    (path / "ci" / "needs_llm_run.py").write_text((ROOT / "ci" / "needs_llm_run.py").read_text())
    (path / "README.md").write_text("x")
    _git(path, "add", ".")
    _commit(path, "base", "human")
    base = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()
    (path / "ci" / "pr_evidence.py").write_text(STUB)  # the PR replaces the checker
    (path / "AGENTS.md").write_text("changed")
    _git(path, "add", ".")
    _commit(path, "head", "human")
    return base


def _run_gate(
    path: Path, base: str, tmp: Path, body_text: str = ""
) -> subprocess.CompletedProcess[str]:
    import json

    tmp.mkdir(parents=True, exist_ok=True)
    env = _fake_gh(tmp, json.dumps({"body": body_text, "user": {"login": "me"}}), path)
    env["R"] = f"refs/heads/gh-readonly-queue/main/pr-9-{base}"
    env |= {
        "MERGE_GROUP_BASE": base,
        "MERGE_GROUP_REF": env["R"],
        "GH_TOKEN": "t",
        "HOME": str(tmp),
    }
    return subprocess.run(  # nosec B603 B607 -- runs the workflow step's own script
        ["bash", "-e", "-c", _gate_run()],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_the_gate_step_fails_closed_when_the_base_has_the_gate_but_an_incompatible_script(
    tmp_path: Path,
):
    # a merged refactor renamed the flag: the base's script rejects the call. The PR's stub
    # checker (exit 0) must not run in its place.
    renamed = (
        (ROOT / "ci" / "pr_evidence.py")
        .read_text()
        .replace("--require-results", "--demand-results")
    )
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    base = _gate_repo(repo_dir, WORKFLOW_WITH_GATE, renamed)
    res = _run_gate(repo_dir, base, tmp_path / "t")
    assert res.returncode != 0, res.stdout + res.stderr
    assert not (repo_dir / "stub-ran").exists()
    assert "branch's scripts" not in res.stdout


def test_the_gate_step_fails_closed_when_the_base_script_is_missing(tmp_path: Path):
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    base = _gate_repo(repo_dir, WORKFLOW_WITH_GATE, (ROOT / "ci" / "pr_evidence.py").read_text())
    _git(repo_dir, "checkout", "-q", base)
    _git(repo_dir, "rm", "-q", "ci/pr_evidence.py")
    _commit(repo_dir, "drop the script from the base", "human")
    gone = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo_dir / "ci").mkdir(exist_ok=True)
    (repo_dir / "ci" / "pr_evidence.py").write_text(STUB)
    _git(repo_dir, "add", ".")
    _commit(repo_dir, "head", "human")
    res = _run_gate(repo_dir, gone, tmp_path / "t")
    assert res.returncode != 0 and not (repo_dir / "stub-ran").exists()


def test_the_gate_step_uses_the_base_scripts_when_the_base_has_the_gate(tmp_path: Path):
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    base = _gate_repo(repo_dir, WORKFLOW_WITH_GATE, (ROOT / "ci" / "pr_evidence.py").read_text())
    ok = body("e2e-llm: required", "e2e-llm result: pass", "bedrock-smoke: not required")
    assert _run_gate(repo_dir, base, tmp_path / "t1", ok).returncode == 0
    assert not (repo_dir / "stub-ran").exists()
    missing = _run_gate(
        repo_dir, base, tmp_path / "t2", body("e2e-llm: required", "bedrock-smoke: not required")
    )
    assert missing.returncode == 1


def test_the_gate_step_bootstraps_only_while_the_base_has_no_results_job(tmp_path: Path):
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    base = _gate_repo(repo_dir, WORKFLOW_WITHOUT_GATE, (ROOT / "ci" / "pr_evidence.py").read_text())
    res = _run_gate(repo_dir, base, tmp_path / "t")
    assert res.returncode == 0 and (repo_dir / "stub-ran").exists()
    assert "::warning::" in res.stdout and "adds it" in res.stdout


def test_a_non_sha_is_never_sent_to_the_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    called: list[object] = []
    monkeypatch.setattr(pr_evidence.subprocess, "run", lambda *a, **k: called.append(a))
    for bad in ("", "HEAD", "abc", "a" * 39, "A" * 40, "x; rm", "../../x"):
        assert pr_evidence._commit_is_verified_bot_work(bad) is False, bad
    assert called == []
