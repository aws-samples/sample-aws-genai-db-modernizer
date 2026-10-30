# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PR Evidence section is written from the diff and checked in CI (#1153)."""

from __future__ import annotations

import re
import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
import sys
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


def test_required_run_needs_a_result_line():
    errs = pr_evidence.check(body("e2e-llm: required", "bedrock-smoke: not required"), E2E)
    assert len(errs) == 1 and "result" in errs[0]


def test_result_line_satisfies_required_run():
    text = body(
        "- e2e-llm: required",
        "- e2e-llm result: MODERNIZE_RESULT: complete job_id=1 db=d mode=both",
        "bedrock-smoke: not required",
    )
    assert pr_evidence.check(text, E2E) == []


def test_placeholder_or_empty_result_does_not_count():
    for res in ("e2e-llm result:", "e2e-llm result: <paste the excerpt or outcome>"):
        errs = pr_evidence.check(body("e2e-llm: required", res, "bedrock-smoke: not required"), E2E)
        assert errs, res


def test_both_required_each_needs_its_result():
    errs = pr_evidence.check(
        body(
            "e2e-llm: required",
            "e2e-llm result: MODERNIZE_RESULT: complete job_id=1",
            "bedrock-smoke: required",
        ),
        BOTH,
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
    # only the result placeholder is missing
    errs = pr_evidence.check(text, E2E)
    assert len(errs) == 1 and "result" in errs[0]
    filled = text.replace("<paste the excerpt or outcome>", "pass, cost $4, judge 4.0")
    assert pr_evidence.check(filled, E2E) == []


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # nosec B603 B607


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
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
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


def test_cli_skips_a_bot_author(repo: Path):
    res = _run(
        repo,
        "check",
        "--body-env",
        "B",
        "--author-env",
        "A",
        env={"B": "no marker", "A": "dependabot[bot]"},
    )
    assert res.returncode == 0 and "bot" in res.stdout
    human = _run(
        repo,
        "check",
        "--body-env",
        "B",
        "--author-env",
        "A",
        env={"B": "no marker", "A": "someone"},
    )
    assert human.returncode == 1


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


def test_pre_merge_removes_the_body_file_in_its_cleanup_trap():
    sh = (ROOT / "ci" / "pre_merge.sh").read_text()
    cleanup = sh.split("cleanup() {")[1].split("}")[0]
    assert '"$pr_body_file"' in cleanup
