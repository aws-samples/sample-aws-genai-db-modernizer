#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Decide which real-model runs a diff needs, and when (#1965).

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

Each run has two tiers of paths:

* **prompt** (``*_PROMPT``): files that write, word, frame or tag text sent to a
  model: the skills, commands and session files, the e2e-llm fixtures' sample
  collections, the prompt and request builders
  (every ``prepare_*_llm_input`` home, the schema-design input builders, the run
  scripts that build the external-mode request), the prompt framing, catalog and
  module prose that reaches a model word for word, the model-output contracts, and
  the model settings (model id, token cap, temperature). The run is ``required``
  before the pull request merges.
* **nightly** (``*_NIGHTLY``): pipeline logic whose output a model only reads as
  data (routing, scoring, pattern catalogs, collectors and analysis values,
  synthesis math), the model-call plumbing and the runner scripts that only read
  and write paths. Unit tests (and, for the pipeline, the deterministic e2e) cover
  them per pull request; the maintainers' nightly run on main covers the real model.

A path's tier, when unclear, is prompt tier (the rule is applied conservatively):
does a change to it alter the wording, framing, tags or field set of what a model
receives, prose that reaches it word for word, the shape it is asked to return,
or the model settings? Values computed upstream and read as data are nightly.

Output: one ``<run>: required|nightly|not required`` line per run, each
followed by its matching paths (a nightly-tier path under a ``required`` run is
marked ``(nightly)``). The prompt tier wins: one prompt-tier path makes the run
``required``. A file may be in both runs. Exit 0 on success; on any git error,
exit 2 with a message on stderr and print no verdict, so a broken diff can never
read as "not required". The four tier lists are the single source of truth;
AGENTS.md points here, and tests/unit/ci/test_needs_llm_run.py fails if a model
call site is not listed, or a file its scan finds building a prompt is unlisted or
not in the prompt tier.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs git with fixed argv
import sys
from fnmatch import fnmatchcase

REQUIRED = "required"
NIGHTLY = "nightly"
NOT_REQUIRED = "not required"
STATES: tuple[str, ...] = (REQUIRED, NIGHTLY, NOT_REQUIRED)

# fnmatch patterns; "*" also matches "/", so a trailing "/*" covers a directory.
# A path in a prompt list is prompt tier even when a nightly pattern matches it too.
E2E_LLM_PROMPT: tuple[str, ...] = (
    # Loaded into or driving the headless session.
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/*",
    "src/skills/*",
    # The session's prompt and settings, and the judge's prompt and the facts it is given.
    "ci/e2e-llm.sh",
    "ci/llm/judge.py",
    "ci/llm/judge_facts.py",
    "ci/llm/rubric.md",
    # The sample collections ci/e2e-llm.sh unzips per fixture: the model's input data (#1986).
    "docs/examples/wordpress/wordpress.zip",
    "docs/examples/discourse/discourse.zip",
    "src/agents/prompt_framing.py",
    # Builders of llm_input / request files (prepare_*_llm_input, build_*_request) and the
    # prompts the validators are given.
    "src/agents/analysis/*_analysis_agent.py",
    "src/agents/schema_design/*",
    "src/agents/referee/reality_check_handler.py",
    "src/agents/referee/reality_check_request.py",
    "src/agents/referee/synthesis_handler.py",
    "src/agents/referee/synthesis_report.py",  # the executive-summary prompt
    "src/agents/referee/consolidation_validator.py",  # the validator's prompt
    "src/tools/schema/aurora_common/design_view.py",
    # Advisor prompts in the analysis tools (#1281).
    "src/tools/analysis/documentdb_analysis_tools.py",
    "src/tools/analysis/dynamodb_analysis_tools.py",
    # The run scripts that build the external-mode request: the output schema, the
    # untrusted-data notice, the Aurora delta request and the skill the session follows.
    "scripts/run_analysis.py",
    "scripts/run_schema_design.py",
    # The executive-summary request's per-list budget and slimming (#762, #1064).
    "src/agents/referee/summary_input_budget.py",
    "src/agents/referee/summary_input_slim.py",
    # The migration waves' titles and rationale prose go into the executive-summary
    # model's input (prepare_synthesis_llm_input); their map sentence only appears
    # when schema design ran, which a deterministic run never does (#1284).
    "src/agents/referee/migration_wave*.py",
    # Messages quoted word for word in the designer's correction turn
    # (numbers_correction_prompt), and the designs sent back for unknown source
    # columns (#1431).
    "src/tools/schema/design_metrics.py",
    "src/tools/schema/dynamodb_attribute_columns.py",
    # The write-intent catalog's approach text reaches the designer word for word
    # through analysis_output.carried_writes (#1362), and its loader.
    "src/catalog/write_intents/*",
    "src/shared/catalog/write_intents.py",
    # Model settings per role: model id, token cap, temperature, thinking.
    "src/shared/models.py",
    # Fields of the designer input's analysis_output: the redesign findings' evidence text
    # (#1231), the carried writes (#1362), and the scope filter on the analysis prose every
    # design path reads (scope_filtered_analysis, #1574).
    "src/agents/referee/redesign_findings.py",
    "src/agents/referee/carried_writes.py",
    "src/agents/referee/engine_scope.py",
    # Which statements on temporary objects follow their routine and so stay out of
    # every designer's input (#1891).
    "src/agents/referee/routine_units.py",
    # The shapes the model is asked to return.
    "src/contracts/*_model_output.py",
    "src/contracts/aurora_design_delta.py",
)
E2E_LLM_NIGHTLY: tuple[str, ...] = (
    # The rest of the e2e-llm harness (checks, replay, costs); scripts/search_artifacts.py,
    # check_decision.py, start_local_ui.py and _sandbox.py are left out (only
    # ci/e2e-llm.sh runs them; they have unit tests).
    "ci/llm/*",
    "src/tools/schema/aurora_common/delta_merge.py",
    # Feed the designer input: analysis.json (and decision-trace.json) is read whole into
    # llm_input, so the tools and pattern catalogs that build it change its values.
    "src/tools/analysis/*_analysis_tools.py",
    "src/tools/analysis/*_pattern_catalog.py",
    "src/tools/analysis/pattern_catalog_base.py",
    "src/tools/analysis/scoring.py",
    "src/tools/analysis/variant_attributes.py",
    "src/tools/analysis/key_access.py",
    "src/tools/sql/query_pattern_groups.py",  # queries.query_pattern_groups (#1146)
    # The carried writes' version floor (#1362); the intents themselves are prompt tier.
    "src/catalog/carried_writes.toml",
    "src/agents/referee/table_resolution.py",
    # The one list of placeholder tables (#1817): which tables a design group and the
    # analysis name, and the saved write groups the designer is told about.
    "src/shared/placeholder_tables.py",
    "src/storage/assignment_versioning.py",
    # Which saved trade-offs are the pipeline's notes, and the topic every saved design
    # carries (write_schema_output, finalize_schema_design, #1221).
    "src/shared/trade_off_topics.py",
    "scripts/run_assessment.py",
    "scripts/run_reality_check.py",
    "scripts/run_synthesis.py",
    # The /explore step (#307) runs a catalog entry on the job and gives its rows to a
    # model; the check compares the model's answer with the same entry's result.
    "src/graph/catalog.py",
    "scripts/graph_query.py",
)
BEDROCK_SMOKE_PROMPT: tuple[str, ...] = (
    # Files with an external branch and a Bedrock branch are in both runs.
    "src/agents/prompt_framing.py",
    "src/agents/schema_design/handler.py",
    "src/agents/schema_design/revision_handler.py",
    "src/agents/analysis/dynamodb_analysis_agent.py",
    "src/agents/analysis/documentdb_analysis_agent.py",
    "src/agents/analysis/aurora_pg_analysis_agent.py",
    "src/agents/analysis/aurora_mysql_analysis_agent.py",
    "src/agents/referee/synthesis_handler.py",
    "src/agents/referee/reality_check_handler.py",
    "src/agents/referee/consolidation_validator.py",
    "src/agents/referee/synthesis_report.py",
    "src/agents/answer_validation.py",
    "src/tools/analysis/llm_advisor_base.py",
    "src/tools/analysis/documentdb_analysis_tools.py",
    "src/tools/analysis/dynamodb_analysis_tools.py",
    "src/tools/schema/*_schema_agent.py",
    "src/tools/schema/aurora_common/bedrock_delta.py",
    # The orchestrator's and its subagents' system prompts, its tools' descriptions and
    # framed results, and the plan wording it is given.
    "src/atx_orchestrator/orchestrator.py",
    "src/atx_orchestrator/subagents/*.py",
    "src/atx_orchestrator/tools.py",
    "src/atx_orchestrator/mcp_tools.py",
    # Re-splits and tags the text Bedrock receives around the framed data.
    "src/shared/bedrock_guardrail.py",
    # Model settings per role, the summary request's budget and slimming, and the
    # messages quoted in the designer's correction turn.
    "src/shared/models.py",
    "src/agents/referee/summary_input_budget.py",
    "src/agents/referee/summary_input_slim.py",
    "src/tools/schema/design_metrics.py",
    "src/tools/schema/dynamodb_attribute_columns.py",
    # Answer models a Strands agent sends to Bedrock as its tool schema
    # (structured_output_model=): a shape change here is proven only when Bedrock
    # accepts the converted schema (#1281; tests/contract/test_bedrock_tool_schemas.py).
    "src/contracts/*_model_output.py",
    "src/contracts/aurora_design_delta.py",
    "src/contracts/dynamodb_pe_review.py",
    "src/contracts/reality_check_model_response.py",
    "src/contracts/executive_summary_response.py",
)
BEDROCK_SMOKE_NIGHTLY: tuple[str, ...] = (
    # Model construction and the runners: how a call is made, not what it says. The run
    # scripts' Bedrock branch only delegates.
    "scripts/test_local_phased.py",
    "scripts/run_analysis.py",
    "scripts/run_assessment.py",
    "scripts/run_reality_check.py",
    "scripts/run_schema_design.py",
    "scripts/run_synthesis.py",
    "src/shared/role_bedrock_model.py",
    "src/atx_orchestrator/*.py",  # runtime/* uses agent_builder_sdk for plumbing, still listed
)
TIERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "e2e-llm": (E2E_LLM_PROMPT, E2E_LLM_NIGHTLY),
    "bedrock-smoke": (BEDROCK_SMOKE_PROMPT, BEDROCK_SMOKE_NIGHTLY),
}
E2E_LLM: tuple[str, ...] = E2E_LLM_PROMPT + E2E_LLM_NIGHTLY
BEDROCK_SMOKE: tuple[str, ...] = BEDROCK_SMOKE_PROMPT + BEDROCK_SMOKE_NIGHTLY
RUNS: dict[str, tuple[str, ...]] = {"e2e-llm": E2E_LLM, "bedrock-smoke": BEDROCK_SMOKE}
MODEL_FACING: tuple[str, ...] = E2E_LLM + BEDROCK_SMOKE
PROMPT_TIER: tuple[str, ...] = E2E_LLM_PROMPT + BEDROCK_SMOKE_PROMPT


class DiffError(Exception):
    """The changed-file list could not be computed."""


def matching(paths: list[str], patterns: tuple[str, ...]) -> list[str]:
    """Paths matching any pattern, in input order. Paths are used verbatim."""
    return [
        p for p in paths if p and any(fnmatchcase(p.removeprefix("./"), pat) for pat in patterns)
    ]


def tier_hits(paths: list[str]) -> dict[str, tuple[list[str], list[str]]]:
    """Per run, the changed paths in its prompt tier and the others in its nightly tier."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    for run, (prompt, nightly) in TIERS.items():
        now = matching(paths, prompt)
        out[run] = (now, [p for p in matching(paths, nightly) if p not in now])
    return out


def state_of(prompt_hits: list[str], nightly_hits: list[str]) -> str:
    """The prompt tier wins: one prompt-tier path makes the run required."""
    return REQUIRED if prompt_hits else NIGHTLY if nightly_hits else NOT_REQUIRED


def verdict(paths: list[str]) -> dict[str, str]:
    """Each run's state for these paths: ``required``, ``nightly`` or ``not required``."""
    return {run: state_of(*hits) for run, hits in tier_hits(paths).items()}


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
    for run, (now, later) in tier_hits(paths).items():
        state = state_of(now, later)
        print(f"{run}: {state}")
        for p in now:
            print(f"  {p}")
        for p in later:
            print(f"  {p} (nightly)" if state == REQUIRED else f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
