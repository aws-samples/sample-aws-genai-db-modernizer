# Strands Agent Development Guide

**Document Type:** Implementation Guide
**Status:** Current

---

## Overview

This guide covers two things that apply across the pipeline:

1. The **agent entrypoint pattern** — how `AGENT_TYPE` dispatch and the
   artifact store work for every agent, with or without an LLM.
2. The **Strands SDK pattern** — how the subset of agents that call a model
   (schema design, the analysis LLM advisor, synthesis, reality check) build
   and invoke a `strands.Agent` against Amazon Bedrock.

Everything here describes the local pipeline (`LocalOrchestrator`, direct
Python function calls, the local `ArtifactStore`). The hosted deployment
(Step Functions driving per-agent ECS tasks, EventBridge progress events) was
retired in [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175);
`src/agents/entrypoint.py` still dispatches on `AGENT_TYPE` because it serves
the `agent-load-test` container image, a run-to-completion batch dispatch.
The AWS Transform integration does **not** route through this file — it has
its own sibling, `src/atx_orchestrator/atx_entrypoint.py`, which also
dispatches on an `AGENT_TYPE` env var but with its own vocabulary, and uses
it to launch long-running A2A/AgentCore servers rather than run-to-completion
agents. Neither entrypoint talks to Step Functions or ECS anymore.

---

## Agent Entrypoint Pattern

`src/agents/entrypoint.py` reads a handful of environment variables and
dispatches to the matching handler:

| Variable | Purpose |
|----------|---------|
| `AGENT_TYPE` | Which agent to run: `collector`, `referee-triage`, an engine name (`dynamodb`, `aurora_mysql`, ...), `referee-synthesis`, `assignment-resolver`, `reality-check`, `schema-design`, `schema-split`, `schema-merge`, `load-test` |
| `JOB_ID` | A UUID (truncated to 8 hex characters by the local scripts) — not a KSUID |
| `TARGET_TYPE` | Required alongside `AGENT_TYPE` for `schema-design`, `schema-split`, `schema-merge` and `load-test` — the engine name the agent designs/tests for |
| `DATABASE_NAME` | Source database name; forms the artifact path prefix |
| `S3_BUCKET` | If set, `create_artifact_store()` returns an `S3ArtifactStore`; otherwise a `LocalArtifactStore` rooted at `ARTIFACT_DIR` (default `./artifacts`) |
| `ASSIGNMENT_VERSION`, `SCOPE_ENGINES` | Optional, used for re-running a subset of engines against a specific assignment version |

```python
# src/agents/entrypoint.py (actual dispatch, trimmed)
def _dispatch_agent():
    if AGENT_TYPE == "collector":
        from src.agents.collector.handler import run_collector
        run_collector(JOB_ID, DATABASE_NAME, store)
    elif AGENT_TYPE == "referee-triage":
        from src.agents.referee.triage_handler import run_triage
        run_triage(JOB_ID, DATABASE_NAME, store)
    elif AGENT_TYPE in ANALYSIS_AGENTS:
        from src.agents.analysis.handler import run_analysis
        run_analysis(JOB_ID, DATABASE_NAME, AGENT_TYPE, store)
    elif AGENT_TYPE == "referee-synthesis":
        from src.agents.referee.synthesis_handler import run_synthesis
        run_synthesis(JOB_ID, DATABASE_NAME, store, assignment_version=...)
    # ...and so on for assignment-resolver, reality-check, schema-design, load-test
```

Exit code contract (what a container launcher sees — `LocalOrchestrator`
itself calls handler functions directly for most of the pipeline and checks
no exit code; this matters for the `agent-load-test` image and the AWS
Transform integration, which do launch separate processes):

- **0** — success
- **1** — failure (an error artifact is written before re-raising)
- **2** — the agent needs human/LLM input and is waiting (`AgentNeedsInputError`)

The entrypoint is a run-to-completion script, never a server: it does its
work, writes to the artifact store, and exits. `LocalOrchestrator` calls agent
*handler functions* directly for everything except the load-test container
and the AWS Transform path — there is no process boundary to cross for most
of the pipeline.

See [the orchestrator README](../../src/orchestrator/README.md) for the full
workflow diagram and artifact path table, and
[storage-architecture-guide.md](storage-architecture-guide.md) for the
`ArtifactStore` abstraction.

---

## The LLM Seam Pattern

Every phase that can use a model (schema design, synthesis, reality check,
the analysis LLM advisor) splits into three pieces so the same code serves
Bedrock, Claude Code and fully deterministic runs. See `AGENTS.md` for the
canonical description; the short version:

1. `run_*_deterministic(...)` — the complete result with no model call.
2. `prepare_*_llm_input(det)` — the payload a model reasons over.
3. `apply_*_llm_output(det, llm_output)` — merges and validates the model's
   answer on top of the deterministic result.

`--llm-mode` controls who plays the model's part: `external` (Claude Code
writes `llm_input.json` and the command supplies the response), `bedrock`
(the functions below call a Strands agent directly), or `none` (the seam is
skipped — see AGENTS.md for what each phase falls back to).

---

## Strands SDK: the Bedrock Agents

Strands (`strands-agents` on PyPI) is only in the picture for `--llm-mode
bedrock`. These places build a `strands.Agent` with a `BedrockModel`:

- `src/tools/analysis/llm_advisor_base.py` (`LlmAdvisorBase._get_agent`) — the analysis LLM advisor; builds a fresh agent per call and imports `strands` lazily
- the six `src/tools/schema/*_schema_agent.py` modules (Aurora MySQL, Aurora PostgreSQL, DocumentDB, DynamoDB, ElastiCache, OpenSearch) — each builds a designer agent and a PE-reviewer agent, importing `strands` at module level, and hands them to `SchemaDesignRunner` (`src/tools/schema/base_schema_agent.py`), which runs the designer/reviewer loop but does not build agents itself
- the `src/tools/schema/*_schema_designer.py` classes (DocumentDB, DynamoDB, ElastiCache)
- `src/agents/referee/reality_check_handler.py` — the reality-check executive summary
- `src/agents/referee/consolidation_validator.py` — the LLM validation step for consolidation decisions
- `src/agents/referee/synthesis_report.py` — the synthesis executive summary

The analysis advisor and the three referee modules import `strands` lazily; the
referee modules wrap that import in `try`/`except ImportError`, log a warning and
fall back to the deterministic result if Strands isn't installed.

Most of these are one prompt in, one structured object out, with no tool
wiring. The DynamoDB and OpenSearch schema designers are the exception: both
register `@tool`-decorated functions (for example `load_agent_input` and
`compute_performances_and_costs` in `dynamodb_schema_agent.py`,
`load_agent_input` in `opensearch_schema_agent.py`) that the designer agent
calls during its run, rather than taking everything as plain prompt text.

### Pattern 1: `LlmAdvisorBase` (analysis agents)

`src/tools/analysis/llm_advisor_base.py` is the base class every
engine-specific analysis LLM advisor extends. It handles retries, large-
workload group splitting, and schema filtering so subclasses only implement
`_build_prompt`, `_parse_result`, `_merge_results` and `_output_model`.

```python
# src/tools/analysis/llm_advisor_base.py (real code)
def _get_agent(self):
    """Create a FRESH Strands Agent per call.

    We deliberately do NOT cache the Agent instance. Strands Agents
    maintain conversation history internally — reusing the same Agent
    across N groups of a large workload causes the accumulated context
    to overflow the model's context window (observed 2026-07-11:
    DynamoDB analysis on Discourse workload with 56 groups hit Opus
    4.8's context limit at group 11 with error `bedrock threw context
    window overflow error`, killing the container). Creating a fresh
    Agent per group ensures each call is stateless.
    """
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    model_id = os.environ.get("ANALYSIS_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    model = BedrockModel(model_id=model_id)
    return Agent(
        model=model,
        system_prompt=self.system_prompt,
        tools=[],
        structured_output_model=self._output_model(),
        callback_handler=None,
    )
```

When a workload has more than `MAX_LLM_QUERIES` (30 by default) queries, the
advisor splits them into groups, filters the schema down to only the tables
each group's queries reference, calls the LLM per group with exponential
backoff retry, and merges the per-group results.

### Pattern 2: `SchemaDesignRunner` (schema design)

`src/tools/schema/base_schema_agent.py` runs the designer/PE-reviewer loop
shared by DynamoDB, DocumentDB, both Aurora engines, and ElastiCache (not
OpenSearch, which has its own designer loop): the designer produces a draft, a
PE-reviewer agent critiques it, and the loop repeats (capped at
`MAX_PE_ITERATIONS = 2`) until the reviewer approves or the cap is hit. It
also tracks a separate, larger retry budget for Bedrock throttling (`429`,
`503`, `ThrottlingException`, ...) than for genuine designer failures, since
every engine's schema design groups run concurrently and share the same
model quota.

```python
# src/tools/schema/base_schema_agent.py (usage, real code)
from src.tools.schema.base_schema_agent import SchemaDesignRunner

runner = SchemaDesignRunner(
    target_type="documentdb",
    output_model=DocumentDBModelOutputContract,
    model=bedrock_model,
    designer_agent=agent,
    pe_skill_path="src/skills/documentdb-pe-review.md",
    pe_reviewer_fn=_invoke_pe_reviewer,
    format_pe_feedback_fn=_format_pe_feedback,
)
output, trace = runner.run(designer_prompt, input_summary)
```

**DynamoDB specifically always splits into groups** (roughly 20 queries each)
that are designed independently and then merged by
`src/agents/schema_design/dynamodb_merge.py` — this is mandatory, not a
large-workload fallback like the analysis advisor's group splitting.

### What a system prompt looks like

Each designer/advisor loads its system prompt from a Markdown file under
`src/skills/` (for example `src/skills/documentdb-pe-review.md`), framed with
`frame_untrusted` (`src/agents/prompt_framing.py`) wherever customer or
collector text is interpolated into the prompt — see "Untrusted text is
data" in `AGENTS.md`.

---

## Error Handling

- The deterministic builders raise on malformed input; handlers catch model
  exceptions, log them, and either retry (schema design, the analysis
  advisor) or fall back to the deterministic result (synthesis executive
  summary, reality check).
- `AgentNeedsInputError` (`src/agents/interaction.py`) is raised by any phase
  that is genuinely waiting on human/LLM input in `external` mode; the
  entrypoint maps it to exit code 2 rather than treating it as a failure.

---

## Testing

See [testing-guide.md](testing-guide.md) for the full test tiers. For
Strands-backed code specifically: unit tests patch `strands.Agent` /
`BedrockModel` at the model entry point (never call a real model — see
AGENTS.md "Tests never call a real model") and assert on the deterministic
builders directly wherever the LLM seam allows it.

---

## Related Documentation

- [Orchestrator README](../../src/orchestrator/README.md) — workflow, artifact paths, agent dispatch
- [Referee Agent Guide](referee-agent-guide.md) — triage, reality check, synthesis
- [Strands Collector Guide](strands-collector-guide.md)
- [Storage Architecture Guide](storage-architecture-guide.md)
- [Contract Specifications](../contracts/agent-contracts-spec.md)
- `AGENTS.md` — the LLM seam pattern and the three `--llm-mode` values
