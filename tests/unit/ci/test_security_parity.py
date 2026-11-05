# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One security scan and one set of lint paths everywhere (#1844).

The pre-commit semgrep hook ran ``--config=auto`` while the GitHub ``security`` job ran
``p/python p/security-audit``, so code that passed one failed the other (#1837), and
``ci/lint.sh`` ran Black on ``src/`` and ``tests/`` only while the pre-commit hook also
covered ``scripts/``, so an unformatted ``scripts/`` line merged green. These tests keep
every place on the same script, the same pinned rules and the same paths.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from ci import python_files

REPO = Path(__file__).resolve().parents[3]
PRECOMMIT = REPO / ".pre-commit-config.yaml"
WORKFLOWS = REPO / ".github" / "workflows"
SCANNER = re.compile(r"semgrep|bandit|gitleaks", re.IGNORECASE)
# Ways a semgrep ruleset used to be named outside ci/security.toml.
FLOATING_RULESET = re.compile(r"--config[= ]auto|\bp/[a-z][a-z-]+|semgrep\.dev/[cr]/")
FORMAT_LINT_HOOKS = ("black", "isort", "ruff")


def _hooks() -> list[dict]:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    hooks = []
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            hooks.append({**hook, "repo": repo["repo"], "top_exclude": config.get("exclude")})
    return hooks


def _steps() -> list[tuple[str, str, dict]]:
    steps = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                steps.append((path.name, job_id, step))
    return steps


def _tracked(*patterns: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *patterns], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line]


# ---------------------------------------------------------------- one entry point


def test_the_precommit_security_hook_runs_the_script_on_every_staged_file() -> None:
    hooks = [h for h in _hooks() if h["id"] == "security"]
    assert len(hooks) == 1, "one pre-commit hook runs the security scan"
    hook = hooks[0]
    # The hook alone skips the semgrep-environment reinstall (9 to 14 s) (CI and pre_merge do it).
    assert hook["entry"] == "ci/security.sh --skip-env-reinstall"
    assert hook["pass_filenames"] is True
    # Only on commit: at the commit-msg stage pre-commit would pass the message file.
    assert hook["stages"] == ["pre-commit"]
    # Every staged file reaches the script; each scanner applies its own ignores.
    assert "files" not in hook and "exclude" not in hook and "types" not in hook
    assert not hook["top_exclude"]


def test_no_other_precommit_hook_runs_a_scanner() -> None:
    others = [
        h["id"]
        for h in _hooks()
        if h["id"] != "security" and SCANNER.search(f"{h['id']} {h['repo']} {h.get('entry', '')}")
    ]
    assert others == [], f"run these through ci/security.sh instead: {others}"


def test_the_github_security_job_runs_the_script_on_the_whole_tree() -> None:
    runs = [
        (wf, job, step["run"].strip())
        for wf, job, step in _steps()
        if "ci/security.sh" in step.get("run", "")
    ]
    assert runs == [("ci.yml", "security", "./ci/security.sh")]


def test_no_workflow_step_runs_a_scanner_any_other_way() -> None:
    others = [
        (wf, job, step.get("name") or step.get("uses"))
        for wf, job, step in _steps()
        if "ci/security.sh" not in step.get("run", "")
        and SCANNER.search(f"{step.get('uses', '')} {step.get('run', '')}")
    ]
    assert others == []


@pytest.mark.parametrize(
    ("flag", "expected"), [("--quick", 'sec_args=(--quick --base "$base")'), ("", "sec_args=()")]
)
def test_pre_merge_runs_the_script_quick_on_the_branch_and_full_otherwise(
    flag: str, expected: str
) -> None:
    text = (REPO / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    assert './ci/security.sh ${sec_args[@]+"${sec_args[@]}"}' in text
    assert expected in text


def test_nothing_else_names_a_floating_semgrep_ruleset() -> None:
    places = [PRECOMMIT, REPO / "Makefile", *WORKFLOWS.glob("*.yml")]
    places += [p for p in (REPO / "ci").rglob("*") if p.suffix in {".sh", ".py", ".yml"}]
    places += list((REPO / "scripts").glob("*.sh"))
    hits = []
    for path in places:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FLOATING_RULESET.search(line):
                hits.append(f"{path.relative_to(REPO)}:{n}")
    assert hits == [], "semgrep rules come from ci/security.toml only"


@pytest.mark.passes_on_main  # checks this file's own helper, which main lacks a use for
def test_floating_ruleset_pattern_catches_the_old_configs() -> None:
    for old in ("semgrep scan --config=auto", "config: p/python p/security-audit"):
        assert FLOATING_RULESET.search(old), old
    assert not FLOATING_RULESET.search("uvx --from semgrep==1.183.0 semgrep scan")


# ---------------------------------------------------------------- the pins


def _config() -> dict:
    return tomllib.loads((REPO / "ci" / "security.toml").read_text(encoding="utf-8"))


def test_semgrep_engine_and_rule_sources_are_pinned() -> None:
    semgrep = _config()["semgrep"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", semgrep["version"])
    for source in semgrep["source"]:
        assert re.fullmatch(r"[0-9a-f]{40}", source["commit"]), source["name"]
        assert source["url"].startswith("https://github.com/"), source["name"]
        assert source["rules"] == sorted(set(source["rules"])), "sorted, no duplicates"
        assert all(r.endswith((".yaml", ".yml")) for r in source["rules"])


def test_semgrep_and_every_dependency_are_pinned_with_hashes() -> None:
    lines = (REPO / "ci" / "semgrep-requirements.txt").read_text(encoding="utf-8").splitlines()
    body = [line for line in lines if line and not line.lstrip().startswith("#")]
    pins = [line.split(" ", 1)[0] for line in body if not line.startswith(" ")]
    assert f"semgrep=={_config()['semgrep']['version']}" in pins
    assert pins and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", p) for p in pins), pins
    # Every requirement is followed by at least one --hash line.
    text = "\n".join(body)
    for pin in pins:
        block = text.split(pin, 1)[1].split("\n", 2)
        assert "--hash=sha256:" in block[1], pin


def test_gitleaks_is_pinned_for_ci_and_developer_machines() -> None:
    gitleaks = _config()["gitleaks"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", gitleaks["version"])
    assert {"linux_x64", "darwin_arm64", "darwin_x64"} <= set(gitleaks["sha256"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in gitleaks["sha256"].values())
    assert (REPO / gitleaks["config"]).is_file()


@pytest.mark.parametrize(
    "registry_id",
    [
        # In p/security-audit, the GitHub job's old ruleset.
        "python.lang.security.audit.dangerous-subprocess-use-tainted-env-args"
        ".dangerous-subprocess-use-tainted-env-args",
        # From --config=auto, the old pre-commit hook's: the two #1837 findings.
        "python.lang.security.audit.formatted-sql-query.formatted-sql-query",
        "python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query",
        "trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal",
        # In both.
        "python.lang.security.audit.eval-detected.eval-detected",
    ],
)
def test_the_pinned_rules_keep_both_old_rulesets(registry_id: str) -> None:
    files = {
        ".".join(p for p in (s["id_prefix"], r.rsplit(".", 1)[0].replace("/", ".")) if p)
        for s in _config()["semgrep"]["source"]
        for r in s["rules"]
    }
    assert registry_id.rsplit(".", 1)[0] in files


# ---------------------------------------------------------------- lint paths


def _lint_sh() -> str:
    return (REPO / "ci" / "lint.sh").read_text(encoding="utf-8")


def _covered_by_hook(hook: dict, files: list[str]) -> set[str]:
    include = re.compile(hook.get("files", ""))
    excludes = [re.compile(e) for e in (hook.get("exclude"), hook.get("top_exclude")) if e]
    return {f for f in files if include.search(f) and not any(e.search(f) for e in excludes)}


def test_lint_sh_checks_the_files_ci_python_files_prints() -> None:
    text = _lint_sh()
    assert 'py_list="$(uv run python ci/python_files.py)"' in text
    assert 'do PY_FILES+=("$f"); done <<<"$py_list"' in text
    for tool in FORMAT_LINT_HOOKS:
        lines = [line for line in text.splitlines() if f"uv run {tool} " in line]
        assert lines and all(line.endswith('"${PY_FILES[@]}"') for line in lines), tool


@pytest.mark.parametrize("hook_id", FORMAT_LINT_HOOKS)
def test_lint_sh_and_the_precommit_hook_cover_the_same_python_files(hook_id: str) -> None:
    files = _tracked("*.py", "*.pyi")
    (hook,) = [h for h in _hooks() if h["id"] == hook_id]
    by_hook, by_lint = _covered_by_hook(hook, files), set(python_files.python_files(REPO))
    assert sorted(by_hook - by_lint) == [], "checked at commit, not by ci/lint.sh"
    assert sorted(by_lint - by_hook) == [], "checked by ci/lint.sh, not at commit"


def test_lint_covers_scripts_and_ci() -> None:
    tops = {f.split("/", 1)[0] for f in python_files.python_files(REPO) if "/" in f}
    assert {"scripts", "ci", "src", "tests"} <= tops
    assert "contrib" not in tops


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_checkout_with_extra_files_keeps_lint_and_hooks_in_step(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".pre-commit-config.yaml").write_text(PRECOMMIT.read_text(encoding="utf-8"))
    for rel in ("src/app.py", "contrib/old.py", ".extra/tool.py", ".extra/stub.pyi", "top.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    (tmp_path / ".extra" / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    tracked = python_files.tracked_python(tmp_path)
    by_lint = python_files.python_files(tmp_path)
    assert by_lint == [".extra/stub.pyi", ".extra/tool.py", "src/app.py", "top.py"]
    for hook_id in FORMAT_LINT_HOOKS:
        (hook,) = [h for h in _hooks() if h["id"] == hook_id]
        assert _covered_by_hook(hook, tracked) == set(by_lint), hook_id


def test_hooks_that_select_differently_stop_the_file_list(tmp_path: Path) -> None:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] == "ruff":
                hook["exclude"] = "^contrib/|^docs/"
    (tmp_path / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _git(tmp_path, "init", "-q")
    assert python_files.main(["--repo", str(tmp_path)]) == 2


def test_no_matching_files_stops_the_file_list(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".pre-commit-config.yaml").write_text(PRECOMMIT.read_text(encoding="utf-8"))
    assert python_files.main(["--repo", str(tmp_path)]) == 2


@pytest.mark.passes_on_main  # checks this file's own helper, which main lacks a use for
def test_hook_coverage_reads_files_and_exclude() -> None:
    hook = {"files": r"^src/", "exclude": r"^src/skip/", "top_exclude": None}
    assert _covered_by_hook(hook, ["src/a.py", "src/skip/b.py", "c.py"]) == {"src/a.py"}


@pytest.mark.passes_on_main  # guards a hook this PR keeps as it was
def test_detect_secrets_stays_a_separate_precommit_hook() -> None:
    (hook,) = [h for h in _hooks() if h["id"] == "detect-secrets"]
    assert hook["repo"] == "https://github.com/Yelp/detect-secrets"
    assert "--baseline" in hook["args"] and ".secrets.baseline" in hook["args"]
