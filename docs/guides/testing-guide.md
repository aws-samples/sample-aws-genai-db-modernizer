# Testing Guide for Database Modernizer Assessment

**Document Type:** Implementation Guide
**Status:** Current
**Audience:** All Engineers

---

## Overview

Testing strategy for the pipeline, agents, contracts and deliverables. The
full tier breakdown — which command to run for which kind of change, cost,
and timing — lives in `AGENTS.md`'s "Which test tier for which change" table
and in `ci/README.md`; this guide covers conventions specific to writing
agent/contract tests. Key principles:

- Mock database connections and the model entry point — no real databases,
  no real model calls, anywhere in the gated suites.
- Tests are organized by what they exercise (`tests/unit`, `tests/contract`,
  `tests/property`, `tests/graph`, `tests/e2e`), not by a fixed-percentage
  pyramid.
- Contract validation happens via the real Pydantic models in
  `src/contracts/`, not hand-maintained JSON Schema.
- `tests/integration/` needs a live stack or manually generated artifacts and
  is deselected from the gated run (`make test`); it is for local use, not CI.

See [tests/README.md](../../tests/README.md) for the directory layout and
[ci/README.md](../../ci/README.md) for exactly what each `make`/`ci/*.sh`
command runs.

---

## Test Tiers

| Tier | Directory | What it checks | Run with |
|------|-----------|-----------------|----------|
| Unit | `tests/unit/` | Individual functions/classes: pattern detection, scoring, contract builders, handler logic | `make test` / `uv run pytest tests/unit/` |
| Contract | `tests/contract/` | Agent outputs validate against the Pydantic models in `src/contracts/` | `make test` |
| Property | `tests/property/` | Hypothesis-based property tests (e.g. triage/analysis invariants hold across generated inputs) | `make test` |
| Graph | `tests/graph/` | Context graph layer | `make test` (gated) |
| Integration | `tests/integration/` | Component interactions needing a live stack or hand-built artifacts | `uv run pytest tests/integration/` (not gated) |
| Deterministic e2e | `tests/e2e/` | The full pipeline, no LLM, no AWS, both sample databases, rendered HTML (Chromium + WebKit), PDF content, UI smoke | `make e2e` / `./ci/e2e.sh` |
| Headless LLM e2e | `tests/e2e/` driven via `claude -p "/modernize ... --auto"` | A real `/modernize` run against a model, the same deliverable checks, plus a rubric-based quality judge | `make e2e-llm` |

`make test` runs with `--fail-on-skip` and no network access — nothing in
that run may silently skip, and nothing it does may reach outside the
process. `./ci/test.sh --cov=src --cov-report=term --cov-fail-under=65` is
the exact invocation CI gates on.

---

## Mocking Conventions

### Database connections

Collector unit tests never open a real connection — in fact, no collector
imports a database driver directly. `offline`/`ddl` mode parsing and LIVE
mode's row-shaping both go through small, pure `_build_*` helpers
(`_build_tables`, `_build_columns`, `_build_queries`, ...) in
`mysql_collector.py`, shared by every engine's offline path via
`_collect_offline`. LIVE mode's actual data retrieval goes only through
`SSMExecutor` (`src/tools/aws/ssm_executor.py`), never a direct connection
from the local process.

```python
# tests/unit/agents/test_mysql_collector.py (real pattern)
from src.agents.collector.mysql_collector import _build_columns, _build_tables

def test_build_tables_skips_empty_columns():
    """Oracle's SYS_IOT_OVER_* internal tables arrive with columns=[]
    after the table_name-based join; Table's min_length=1 validator would
    otherwise reject them, so _build_tables filters them out instead."""
    raw = [{"table_name": "t", "row_count": 0, "data_size_mb": 0,
            "columns": [], "indexes": [], "foreign_keys": []}]
    assert _build_tables(raw, "mydb") == []
```

For the LIVE mode path specifically, mock `SSMExecutor` rather than a
database driver — see `src/tools/database/mysql_tools.py`'s
`MySQLRemoteCollector` for what it wraps.

### The model entry point

Agents that use the LLM seam pattern (schema design, synthesis, reality
check, the analysis LLM advisor — see
[strands-agent-development-guide.md](strands-agent-development-guide.md))
are tested by patching `strands.Agent` / `strands.models.bedrock.BedrockModel`
at the entry point, never by calling a real model. Prefer testing the
deterministic builder directly (`run_*_deterministic`) wherever the seam
allows it — those paths need no mocking at all.

```python
from unittest.mock import patch

@patch("src.tools.analysis.llm_advisor_base.LlmAdvisorBase._get_agent")
def test_advisor_falls_back_after_retries(mock_get_agent):
    mock_get_agent.return_value.side_effect = Exception("boom")
    # advisor.advise(...) should return None, not raise, after MAX_RETRIES
```

### Fixtures

Three fixture layers under `tests/fixtures/`, reused across engines:

1. **Per-pattern fixtures** (`dynamodb_pattern_fixtures.py`,
   `documentdb_pattern_fixtures.py`, `opensearch_pattern_fixtures.py`,
   `redis_pattern_fixtures.py`) — small, isolated collector-output dicts that
   trigger exactly one pattern, for per-agent unit tests.
2. **Vertical fixtures** (`ecommerce_collector_output.py`,
   `saas_platform_collector_output.py`, `gaming_collector_output.py`) —
   realistic, production-like collector outputs shared across every analysis
   agent (the same e-commerce fixture is analyzed by the DynamoDB,
   ElastiCache and DocumentDB agents alike).
3. **Synthetic generator** (`generate_synthetic_collector_output.py`) — for
   benchmarking and property tests at scale.

---

## Testing a New Agent

1. Write the per-pattern fixtures first (TDD) — see
   [new-analysis-agent-guide.md](new-analysis-agent-guide.md) step 9 for the
   full walkthrough and checklist.
2. Unit-test the deterministic builder against the fixtures.
3. If the agent has an LLM seam, unit-test `prepare_*_llm_input` and
   `apply_*_llm_output` with a canned LLM response — never a real call.
4. Add a contract test asserting the output validates against the Pydantic
   model in `src/contracts/`.
5. Run the deterministic pipeline (`uv run python scripts/test_local_phased.py`
   or `make e2e`) to confirm the new agent's output doesn't change the
   rendered deliverables for an engine it wasn't meant to touch.

---

## End-to-End Testing

- **Deterministic (`make e2e`)**: runs collect → triage → per-engine analysis
  → assignment → reality check → schema design → synthesis → report with no
  LLM and no AWS credentials, on both bundled samples
  (`docs/examples/wordpress/`, `docs/examples/discourse/`), then checks the
  rendered HTML in real browsers (Chromium + WebKit), the PDF content, and a
  local-UI smoke test. See `ci/README.md`'s `e2e.sh` section for the exact
  pytest invocations and why it runs twice.
- **Headless LLM (`make e2e-llm`)**: drives a real `claude -p "/modernize ...
  --auto"` run (`ci/e2e-llm.sh`) against a real model, applies the same
  deliverable checks, and scores the run with a rubric-based judge
  (`ci/llm/rubric.md`). Costs real tokens — see AGENTS.md's test-tier table
  for current cost/time estimates per sample. A dry-run mode
  (`E2E_LLM_DRY_RUN=1` with a saved transcript) replays everything after the
  model call for free.
- **Manual integration (`tests/integration/`)**: exercises a real database
  connection or a manually generated artifact tree; not run in CI.

---

## Related Documentation

- [tests/README.md](../../tests/README.md) — directory layout
- [ci/README.md](../../ci/README.md) — exact CI commands and what they check
- `AGENTS.md` — "Which test tier for which change"
- [Strands Agent Development Guide](strands-agent-development-guide.md) — the LLM seam pattern
- [New Analysis Agent Guide](new-analysis-agent-guide.md) — the fixture layers in context
