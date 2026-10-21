"""ci/needs_llm_run.py: the deterministic gate for real-model runs."""

from __future__ import annotations

import ast
import functools
import os
import re
import subprocess  # nosec B404 -- runs git and this repo's own script with fixed argv
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from ci.needs_llm_run import BEDROCK_SMOKE, E2E_LLM, MODEL_FACING, main, verdict

REPO_ROOT = Path(__file__).resolve().parents[3]

# What marks a file as a model call site or a builder of model input.
CALL_SITE = re.compile(
    r"^\s*(?:from|import) strands\b|^\s*from src\.shared\.(?:models|role_bedrock_model) import|bedrock-runtime|BedrockModel|agent_builder_sdk"
    r"|environ(?:\.get)?\(\s*[\"']\w*MODEL_ID[\"']|getenv\(\s*[\"']\w*MODEL_ID[\"']"
    r"|^def (?:prepare|apply)_\w*llm_(?:input|output)\b"
    r"|^def (?:prepare_schema_design_input|finalize_schema_design)\b"
    r"|^def (?:build|render)_\w*request\b",
    re.MULTILINE,
)
MIN_CALL_SITES = 25


def _runs(*paths: str) -> dict[str, list[str]]:
    return verdict(list(paths))


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/commands/modernize.md",
        ".claude/agents/pr-reviewer.md",
        ".claude/settings.ci.json",
        "src/skills/dynamodb-data-modeling.md",
        "ci/llm/judge.py",
        "src/agents/schema_design/group_input.py",
        "src/agents/referee/reality_check_request.py",
        "src/agents/referee/migration_waves.py",
        "src/agents/referee/migration_wave_builders.py",
        "src/agents/referee/migration_wave_tables.py",
        "src/tools/schema/aurora_common/design_view.py",
        "src/tools/analysis/documentdb_pattern_catalog.py",
        "src/tools/analysis/redis_analysis_tools.py",
        "src/tools/analysis/aurora_common_pattern_catalog.py",
        "src/tools/analysis/pattern_catalog_base.py",
        "src/tools/analysis/scoring.py",
        "src/tools/schema/design_metrics.py",
        "src/agents/referee/table_resolution.py",
        "src/storage/assignment_versioning.py",
    ],
)
def test_headless_paths_need_e2e_llm_only(path: str) -> None:
    assert _runs(path) == {"e2e-llm": [path], "bedrock-smoke": []}


@pytest.mark.parametrize(
    "path",
    [
        "src/tools/schema/dynamodb_schema_agent.py",
        "src/tools/analysis/llm_advisor_base.py",
        "src/tools/schema/aurora_common/bedrock_delta.py",
        "src/agents/answer_validation.py",
        "src/atx_orchestrator/orchestrator.py",
        "src/atx_orchestrator/app.py",
        "src/atx_orchestrator/subagents/base.py",
        "src/shared/role_bedrock_model.py",
        "src/contracts/reality_check_model_response.py",
        "src/contracts/executive_summary_response.py",
        "src/contracts/dynamodb_pe_review.py",
    ],
)
def test_bedrock_only_paths_need_bedrock_smoke_only(path: str) -> None:
    assert _runs(path) == {"e2e-llm": [], "bedrock-smoke": [path]}


@pytest.mark.parametrize(
    "p",
    [
        "src/agents/referee/reality_check_handler.py",
        "src/agents/referee/consolidation_validator.py",
        "src/agents/referee/synthesis_handler.py",
        "src/agents/schema_design/handler.py",
        "src/agents/analysis/dynamodb_analysis_agent.py",
        "scripts/run_schema_design.py",
        "src/shared/models.py",
        # #1281: answer models sent to Bedrock as tool schemas, also read headless.
        "src/tools/analysis/documentdb_analysis_tools.py",
        "src/tools/analysis/dynamodb_analysis_tools.py",
        "src/contracts/documentdb_model_output.py",
        "src/contracts/aurora_design_delta.py",
    ],
)
def test_shared_file_needs_both(p: str) -> None:
    assert _runs(p) == {"e2e-llm": [p], "bedrock-smoke": [p]}


@pytest.mark.parametrize(
    "path",
    [
        "src/agents/referee/tie_break.py",
        "src/agents/referee/cache_overlay.py",
        "src/agents/referee/query_scoring.py",
        "src/report/html.py",
        "tests/unit/test_reality_check.py",
        "README.md",
        "ci/lint.sh",
        "tests/e2e/routing_snapshot.json",
    ],
)
def test_deterministic_paths_need_neither(path: str) -> None:
    assert _runs(path) == {"e2e-llm": [], "bedrock-smoke": []}


def test_main_prints_verdict_per_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["README.md"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "e2e-llm: not required",
        "bedrock-smoke: not required",
    ]
    assert main(["AGENTS.md", "src/atx_orchestrator/app.py"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "e2e-llm: required",
        "  AGENTS.md",
        "bedrock-smoke: required",
        "  src/atx_orchestrator/app.py",
    ]


# --- the git-facing path, in throwaway repos ---------------------------------


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env=env
    )  # nosec B603 B607


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(REPO_ROOT / "ci/needs_llm_run.py"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def _commit(repo: Path, rel: str, text: str = "x") -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", rel)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "README.md")
    _git(r, "checkout", "-q", "-b", "feature")
    return r


def test_cli_diffs_against_merge_base(repo: Path) -> None:
    _commit(repo, "src/skills/a.md")
    res = _cli(repo, "--base", "main")
    assert res.returncode == 0
    assert res.stdout.splitlines() == [
        "e2e-llm: required",
        "  src/skills/a.md",
        "bedrock-smoke: not required",
    ]


def test_cli_empty_diff_is_not_required(repo: Path) -> None:
    res = _cli(repo, "--base", "main")
    assert res.returncode == 0
    assert "e2e-llm: not required" in res.stdout


def test_cli_fails_closed_on_bad_base(repo: Path) -> None:
    res = _cli(repo, "--base", "origin/nope")
    assert res.returncode == 2
    assert "not required" not in res.stdout
    assert "error: git diff origin/nope...HEAD failed" in res.stderr


def test_cli_fails_closed_without_merge_base(repo: Path) -> None:
    _git(repo, "checkout", "-q", "--orphan", "other")
    _commit(repo, "other.md")
    res = _cli(repo, "--base", "main")
    assert res.returncode == 2
    assert res.stdout == ""
    assert "no merge base" in res.stderr


def test_cli_fails_closed_in_shallow_clone(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "a.md")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "b.md")
    shallow = tmp_path / "shallow"
    subprocess.run(  # nosec B603 B607
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--no-single-branch",
            f"file://{repo}",
            str(shallow),
        ],
        check=True,
        capture_output=True,
    )
    _git(shallow, "checkout", "-q", "feature")
    res = _cli(shallow, "--base", "origin/main")
    assert res.returncode == 2
    assert res.stdout == ""
    assert "failed" in res.stderr


def test_cli_fails_closed_outside_a_repo(tmp_path: Path) -> None:
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)}
    res = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(REPO_ROOT / "ci/needs_llm_run.py"), "--base", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert res.returncode == 2
    assert res.stdout == ""


def test_cli_rename_counts_the_old_path(repo: Path) -> None:
    _commit(repo, "src/skills/old.md", "same content\n" * 5)
    _git(repo, "checkout", "-q", "-b", "mv")
    _git(repo, "mv", "src/skills/old.md", "docs-old.md")
    _git(repo, "commit", "-qm", "mv")
    res = _cli(repo, "--base", "feature")
    assert res.returncode == 0
    assert "  src/skills/old.md" in res.stdout.splitlines()


def test_cli_non_ascii_and_space_paths(repo: Path) -> None:
    _commit(repo, "src/skills/café notes.md")
    res = _cli(repo, "--base", "main")
    assert res.returncode == 0
    assert "  src/skills/café notes.md" in res.stdout.splitlines()


def test_missing_base_value_is_an_error() -> None:
    assert main(["--base"]) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--base", "--output=/tmp/x"],
        ["--base", "main", "extra"],
        ["--bogus"],
        ["README.md", "--base"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("error:")


# --- self-checks: a broken pattern or regex must not pass silently -----------


def _scan() -> list[str]:
    found = []
    for root in ("src", "scripts"):
        for f in sorted((REPO_ROOT / root).rglob("*.py")):
            if CALL_SITE.search(f.read_text()):
                found.append(f.relative_to(REPO_ROOT).as_posix())
    return found


def test_every_model_call_site_is_listed() -> None:
    found = _scan()
    assert len(found) >= MIN_CALL_SITES, f"scan found only {len(found)} call sites; fix CALL_SITE"
    missing = [f for f in found if not any(fnmatchcase(f, p) for p in MODEL_FACING)]
    assert not missing, f"add to E2E_LLM or BEDROCK_SMOKE in ci/needs_llm_run.py: {missing}"


# A file with a Bedrock call or branch must be in BEDROCK_SMOKE; one that
# builds or applies llm_input / request files must be in E2E_LLM.
BEDROCK_SITE = re.compile(
    r"\w*mode\s*==\s*[\"']bedrock|def run_bedrock|_dispatch_schema_agent|build_bedrock_model"
    r"|^\s*(?:from|import) strands\b|BedrockModel|bedrock-runtime|agent_builder_sdk"
    r"|^\s*from src\.shared\.(?:models|role_bedrock_model) import",
    re.MULTILINE,
)
HEADLESS_SITE = re.compile(
    r"^def (?:prepare|apply)_\w*llm_(?:input|output)\b"
    r"|^def (?:prepare_schema_design_input|finalize_schema_design)\b"
    r"|^def (?:build|render)_\w*request\b",
    re.MULTILINE,
)


def _in(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, p) for p in patterns)


def _sources() -> list[tuple[str, str]]:
    return [
        (f.relative_to(REPO_ROOT).as_posix(), f.read_text())
        for root in ("src", "scripts")
        for f in sorted((REPO_ROOT / root).rglob("*.py"))
    ]


def test_bedrock_sites_are_in_bedrock_smoke() -> None:
    hits = [p for p, t in _sources() if BEDROCK_SITE.search(t)]
    assert len(hits) >= 15, f"BEDROCK_SITE found only {len(hits)} files; fix the regex"
    missing = [p for p in hits if not _in(p, BEDROCK_SMOKE)]
    assert not missing, f"add to BEDROCK_SMOKE in ci/needs_llm_run.py: {missing}"


def test_llm_input_builders_are_in_e2e_llm() -> None:
    hits = [p for p, t in _sources() if HEADLESS_SITE.search(t)]
    assert len(hits) >= 8, f"HEADLESS_SITE found only {len(hits)} files; fix the regex"
    missing = [p for p in hits if not _in(p, E2E_LLM)]
    assert not missing, f"add to E2E_LLM in ci/needs_llm_run.py: {missing}"


# Modules whose output goes into the designer input (llm_input) but that do not call a
# model themselves, found from the imports of the builders below. Each one not listed in
# MODEL_FACING needs a reason here; a new import that is neither listed nor reviewed fails.
DESIGNER_INPUT_ROOTS = (
    "src/agents/schema_design/handler.py",  # prepare_schema_design_input
    "src/agents/analysis/*_analysis_agent.py",  # analysis.json is read whole into llm_input
)
REVIEWED_DETERMINISTIC_IMPORTS: dict[str, str] = {
    "src/storage/artifact_store.py": "file storage only",
    "src/agents/referee/cache_overlay.py": (
        "the designer-input builder imports one constant and one small helper; the rest of the "
        "module is assignment/routing logic, so gating the whole file would pull routing-only "
        "PRs into e2e-llm"
    ),
    "src/agents/interaction.py": "reads and writes question/answer files; no input content",
    "src/tools/analysis/llm_advisor_base.py": "Bedrock-only advisor, in BEDROCK_SMOKE",
    "src/agents/referee/query_pattern.py": (
        "the designer-input builder imports the record's file name and one filter of it "
        "(fixable_findings, #1231); the findings' text comes from redesign_findings.py, in "
        "E2E_LLM, so gating the whole module would pull every routing-only class-score PR "
        "into e2e-llm"
    ),
    "src/tools/sql/query_facts.py": (
        "the ElastiCache agent imports its timestamp-column trace helper (#935); the facts "
        "reach analysis.json through the analysis tools, which are in E2E_LLM, so gating the "
        "whole facts module would pull every routing-only facts PR into e2e-llm"
    ),
}


def _src_imports(path: str) -> set[str]:
    """Repo paths of the ``src`` modules a file imports (any nesting level).

    ``from src.pkg import name`` resolves to ``src/pkg/name.py`` when that is a module,
    else to ``src/pkg.py``, else to ``src/pkg/__init__.py`` (a name re-exported by the
    package, so a feeder hidden behind an ``__init__`` still shows up as the package).
    Relative and dynamic imports are not followed; none of the scanned roots uses them."""
    tree = ast.parse((REPO_ROOT / path).read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            base = node.module.replace(".", "/")
            for alias in node.names:
                sub = f"{base}/{alias.name}.py"
                module = f"{base}.py"
                out.add(
                    sub
                    if (REPO_ROOT / sub).exists()
                    else module if (REPO_ROOT / module).exists() else f"{base}/__init__.py"
                )
        elif isinstance(node, ast.Import):
            out.update(
                a.name.replace(".", "/") + ".py" for a in node.names if a.name.startswith("src.")
            )
    return {m for m in out if (REPO_ROOT / m).exists()}


def _designer_input_feeders() -> dict[str, list[str]]:
    roots = [p for p, _ in _sources() if _in(p, DESIGNER_INPUT_ROOTS)]
    feeders: dict[str, list[str]] = {}
    # One level deep on purpose: the direct imports of the builders, plus the imports between
    # analysis tools. A full transitive closure would pull in query_scoring, tie_break, triage
    # and src/shared/*, and so send every routing-only PR to e2e-llm. Do not "fix" it into one.
    queue = [(root, m) for root in roots for m in _src_imports(root)]
    while queue:
        root, mod = queue.pop()
        if mod in feeders and root in feeders[mod]:
            continue
        feeders.setdefault(mod, []).append(root)
        # Analysis tools import each other; follow those so a helper behind a tool is covered.
        if mod.startswith("src/tools/analysis/"):
            queue.extend(
                (root, m) for m in _src_imports(mod) if m.startswith("src/tools/analysis/")
            )
    return {m: r for m, r in feeders.items() if not m.startswith("src/contracts/")}


def test_modules_feeding_designer_input_are_listed_or_reviewed() -> None:
    feeders = _designer_input_feeders()
    assert len(feeders) >= 15, f"found only {len(feeders)} feeders; fix the import scan"
    missing = [
        m
        for m in sorted(feeders)
        if not _in(m, MODEL_FACING) and m not in REVIEWED_DETERMINISTIC_IMPORTS
    ]
    assert not missing, (
        "these modules feed designer input: add them to E2E_LLM in ci/needs_llm_run.py, "
        f"or to REVIEWED_DETERMINISTIC_IMPORTS with a reason: {missing}"
    )


def test_reviewed_deterministic_imports_are_still_feeders() -> None:
    feeders = _designer_input_feeders()
    stale = [m for m in REVIEWED_DETERMINISTIC_IMPORTS if m not in feeders]
    assert not stale, f"no longer imported by a designer-input builder; remove: {stale}"


@pytest.mark.parametrize(
    "known",
    [
        "src/tools/analysis/documentdb_analysis_tools.py",
        "src/tools/analysis/documentdb_pattern_catalog.py",
        "src/tools/analysis/aurora_common_pattern_catalog.py",
        "src/tools/schema/design_metrics.py",
    ],
)
def test_feeder_scan_finds_known_modules(known: str) -> None:
    assert known in _designer_input_feeders()


@pytest.mark.parametrize(
    "known",
    [
        "src/agents/schema_design/handler.py",
        "src/agents/referee/reality_check_request.py",
        "src/agents/referee/synthesis_handler.py",
        "src/tools/schema/aurora_common/design_view.py",
        "src/atx_orchestrator/orchestrator.py",
        "src/atx_orchestrator/app.py",
        "src/atx_orchestrator/subagents/base.py",
        "src/tools/analysis/llm_advisor_base.py",
        "src/shared/role_bedrock_model.py",
    ],
)
def test_scan_finds_known_call_sites(known: str) -> None:
    assert known in _scan()


@functools.cache
def _repo_files() -> list[str]:
    out = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


# Patterns for files that do not exist yet (e.g. a future ".mcp.json"). Add one
# here, with a comment, instead of deleting the check below; remove it once the
# file exists.
NOT_YET_PRESENT: frozenset[str] = frozenset()


@pytest.mark.parametrize("pattern", [p for p in MODEL_FACING if p not in NOT_YET_PRESENT])
def test_every_pattern_matches_a_file(pattern: str) -> None:
    files = _repo_files()
    assert files, "git ls-files returned nothing"
    assert any(fnmatchcase(f, pattern) for f in files), f"pattern matches no file: {pattern}"
