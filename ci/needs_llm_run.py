#!/usr/bin/env python3
"""Decide which real-model runs a diff needs.

Usage: ``ci/needs_llm_run.sh [BASE]`` (BASE defaults to origin/main), or
``uv run python ci/needs_llm_run.py [--base BASE | PATH...]``.

Two kinds of run exercise different code:

* ``e2e-llm`` (``make e2e-llm``): a headless Claude Code ``/modernize`` run.
  Exercises what the session loads (commands, agents, skills, settings,
  AGENTS.md/CLAUDE.md) and every builder or validator of the ``llm_input`` /
  request files that run reads and writes.
* ``bedrock-smoke``: Bedrock-only call sites (Strands agents, advisors, the
  validator and summary model calls, the AWS Transform orchestrator). The
  headless run never reaches them.

Output: one ``<run>: required|not required`` line per run, each followed by
its matching paths. A file may be in both lists. Exit 0 on success; on any git
error, exit 2 with a message on stderr and print no verdict, so a broken diff
can never read as "not required". E2E_LLM and BEDROCK_SMOKE are the single
source of truth; AGENTS.md points here, and
tests/unit/ci/test_needs_llm_run.py fails if a model call site is not listed.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs git with fixed argv
import sys
from fnmatch import fnmatchcase

# fnmatch patterns; "*" also matches "/", so a trailing "/*" covers a directory.
E2E_LLM: tuple[str, ...] = (
    # Loaded into or driving the headless session.
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/*",
    "src/skills/*",
    "ci/llm/*",
    "ci/e2e-llm.sh",
    # Considered, deterministic, left out: scripts/search_artifacts.py, check_decision.py,
    # start_local_ui.py and _sandbox.py (only ci/e2e-llm.sh runs them; they have unit tests).
    # Builders and appliers of llm_input / request files, and model-output validators.
    "src/agents/prompt_framing.py",
    "src/shared/models.py",
    "src/agents/analysis/*_analysis_agent.py",
    "src/agents/schema_design/*",
    "src/agents/referee/reality_check_handler.py",
    "src/agents/referee/reality_check_request.py",
    "src/agents/referee/synthesis_handler.py",
    "src/agents/referee/synthesis_report.py",
    # The migration waves (rationale text included) go into the executive-summary
    # model's input (prepare_synthesis_llm_input); their map sentence only appears
    # when schema design ran, which a deterministic run never does (#1284).
    "src/agents/referee/migration_wave*.py",
    "src/agents/referee/consolidation_validator.py",  # corrections and sanity sweep
    "src/tools/schema/aurora_common/design_view.py",
    "src/tools/schema/aurora_common/delta_merge.py",
    # Feed the designer input: analysis.json (and decision-trace.json) is read whole into
    # llm_input, so the tools and pattern catalogs that build it change what the model is told.
    "src/tools/analysis/*_analysis_tools.py",
    "src/tools/analysis/*_pattern_catalog.py",
    "src/tools/analysis/pattern_catalog_base.py",
    "src/tools/analysis/scoring.py",
    "src/tools/analysis/variant_attributes.py",
    "src/tools/analysis/key_access.py",
    # Decide which queries and numbers the designer sees (prepare_schema_design_input).
    "src/tools/schema/design_metrics.py",
    # Decides which DynamoDB designs are sent back for unknown source columns (#1431).
    "src/tools/schema/dynamodb_attribute_columns.py",
    "src/tools/sql/query_pattern_groups.py",  # queries.query_pattern_groups (#1146)
    # analysis_output.redesign_findings' evidence text (#1231), read from the
    # access-pattern record through query_pattern.fixable_findings.
    "src/agents/referee/redesign_findings.py",
    # analysis_output.carried_writes (#1362): which statements, their intent and the
    # engine's approach, worded by the write-intent catalog.
    "src/agents/referee/carried_writes.py",
    "src/catalog/write_intents/*",
    "src/catalog/carried_writes.toml",
    "src/shared/catalog/write_intents.py",
    "src/agents/referee/table_resolution.py",
    "src/storage/assignment_versioning.py",
    "src/contracts/*_model_output.py",
    "src/contracts/aurora_design_delta.py",
    "scripts/run_analysis.py",
    "scripts/run_assessment.py",
    "scripts/run_reality_check.py",
    "scripts/run_schema_design.py",
    "scripts/run_synthesis.py",
)
BEDROCK_SMOKE: tuple[str, ...] = (
    # Files with an external branch and a Bedrock branch are in both lists.
    "src/agents/schema_design/handler.py",
    "src/agents/schema_design/revision_handler.py",
    "src/agents/analysis/dynamodb_analysis_agent.py",
    "src/agents/analysis/documentdb_analysis_agent.py",
    "src/agents/analysis/aurora_pg_analysis_agent.py",
    "src/agents/analysis/aurora_mysql_analysis_agent.py",
    "scripts/test_local_phased.py",
    "src/agents/referee/synthesis_handler.py",
    "scripts/run_analysis.py",
    "scripts/run_assessment.py",
    "scripts/run_reality_check.py",
    "scripts/run_schema_design.py",
    "scripts/run_synthesis.py",
    "src/agents/answer_validation.py",
    "src/shared/models.py",
    "src/agents/referee/reality_check_handler.py",
    "src/agents/referee/consolidation_validator.py",
    "src/agents/referee/synthesis_report.py",
    "src/tools/analysis/llm_advisor_base.py",
    "src/tools/schema/*_schema_agent.py",
    "src/shared/role_bedrock_model.py",
    "src/shared/bedrock_guardrail.py",
    "src/tools/schema/aurora_common/bedrock_delta.py",
    "src/atx_orchestrator/*.py",  # runtime/* uses agent_builder_sdk for plumbing, still listed
    # Answer models a Strands agent sends to Bedrock as its tool schema
    # (structured_output_model=): a shape change here is proven only when Bedrock
    # accepts the converted schema (#1281; tests/contract/test_bedrock_tool_schemas.py).
    "src/contracts/*_model_output.py",
    "src/contracts/aurora_design_delta.py",
    "src/contracts/dynamodb_pe_review.py",
    "src/contracts/reality_check_model_response.py",
    "src/contracts/executive_summary_response.py",
    "src/tools/analysis/documentdb_analysis_tools.py",
    "src/tools/analysis/dynamodb_analysis_tools.py",
)
RUNS: dict[str, tuple[str, ...]] = {"e2e-llm": E2E_LLM, "bedrock-smoke": BEDROCK_SMOKE}
MODEL_FACING: tuple[str, ...] = E2E_LLM + BEDROCK_SMOKE


class DiffError(Exception):
    """The changed-file list could not be computed."""


def matching(paths: list[str], patterns: tuple[str, ...]) -> list[str]:
    """Paths matching any pattern, in input order. Paths are used verbatim."""
    return [
        p for p in paths if p and any(fnmatchcase(p.removeprefix("./"), pat) for pat in patterns)
    ]


def verdict(paths: list[str]) -> dict[str, list[str]]:
    return {run: matching(paths, pats) for run, pats in RUNS.items()}


def changed_paths(base: str) -> list[str]:
    """Paths changed on HEAD since the merge-base with ``base``; raises DiffError."""
    cmd = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        f"{base}...HEAD",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, check=False)  # nosec B603 -- fixed argv
    except OSError as exc:
        raise DiffError(f"cannot run git: {exc}") from exc
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()
        raise DiffError(
            f"git diff {base}...HEAD failed (exit {res.returncode}): {err}\n"
            "Need a full clone with the base ref fetched (e.g. fetch-depth: 0)."
        )
    return [p for p in res.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["--base"]:
            if len(argv) != 2:
                raise DiffError("usage: --base BASE (exactly one value)")
            if argv[1].startswith("-"):
                raise DiffError(f"invalid base {argv[1]!r}: must not start with '-'")
            paths = changed_paths(argv[1])
        elif argv:
            if any(a.startswith("--") for a in argv):
                raise DiffError(f"unknown option in {argv!r}; usage: [--base BASE | PATH...]")
            paths = argv
        else:
            paths = changed_paths("origin/main")
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for run, hits in verdict(paths).items():
        print(f"{run}: {'required' if hits else 'not required'}")
        for p in hits:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
