# Library demo: `assess()` from Python

`demo_assess.py` runs the modernizer as a Python library on a bundled sample and walks through what `assess()` returns, one short printed section per feature. Run it from the repository root after `uv sync`:

```bash
uv run python docs/examples/library/demo_assess.py
```

That run calls no model: it is free, works offline and takes a few seconds. Try these too:

```bash
# The larger sample (1,654 statements; about half a minute)
uv run python docs/examples/library/demo_assess.py --sample discourse

# An engine scope: statements no engine in it can serve come back unservable, with the reason
uv run python docs/examples/library/demo_assess.py --target-databases dynamodb,elasticache

# Schema design, the post-schema routing and the report, with Amazon Bedrock (costs money)
uv run python docs/examples/library/demo_assess.py --llm-mode bedrock --run-through synthesis
```

| Option | Values | Default |
|---|---|---|
| `--sample` | `wordpress` (MySQL), `discourse` (PostgreSQL), `trailgear` (SQL Server) | `wordpress` |
| `--llm-mode` | `none` (no model) or `bedrock` (Amazon Bedrock, with your AWS credentials) | `none` |
| `--target-databases` | Engine catalog ids, comma-separated (for example `aurora_postgresql,dynamodb`) | every engine |
| `--run-through` | `analysis`, `reality_check`, `schema_design`, `synthesis` (the last two need `--llm-mode bedrock`) | `reality_check` with `none`, `synthesis` with `bedrock` |
| `--progress` | Also print the phases' progress lines to stderr (`assess()` itself writes nothing to stdout) | off |

## What each section shows

| Section | Feature | Needs a model |
|---|---|---|
| 1 | `load_collection()` reads a sample zip; text that is not a collection raises `CollectionLoadError` | No |
| 2 | `run_through="analysis"`: a score per statement on every engine, nothing placed, and a repeat run that is byte-identical; a statement's `query_text` and the `fit_breakdown` of its best engine (how the scorer reached the score) | No |
| 3 | `run_through="reality_check"`: each engine's role and statement count, consolidations (full or partial), the groups that move together, table owners, and the table facts (`summary.table_facts`) | No |
| 4 | `statements[]`: reason codes, the matched patterns and the reason text; with a model, the statements its consolidation review moved | No |
| 5 | `options.target_engines`: the scope as applied, and the unservable statements with their reason | No |
| 6 | `run_through="schema_design"`: the designed engines and the stable design block (`designs`: tables and keys, indexes, collections, cache keys, OpenSearch indexes and data streams); without a model, the library's refusal | Yes |
| 7 | The post-schema routing: statements moved after a design flagged them (`post_schema_reroute`) and the ones left terminal (`post_schema_terminal`) | Yes |
| 8 | `run_through="synthesis"`: the report's monthly cost, its full-modernization label and the executive summary | Yes |
| 9 | The run's model usage (`artifacts.model_usage`) and its cost, from `scripts/usage_cost.py` | Yes |
| 10 | The result's stable surface, checked against the published JSON Schema | No |

## Before you run it with a model

`--llm-mode bedrock` needs AWS credentials that can invoke the Anthropic models on Amazon Bedrock ([Other ways to run it](../../other-ways-to-run-it.md), Amazon Bedrock tab). A run through `synthesis` on the WordPress sample designs three engines with Claude Opus: two measured runs took 18 and 34 minutes and spent $13.51 and $22.14 (the second had a statement rerouted after schema design, so two engines were designed again). Section 9 prints what your run spent, and `--progress` shows each phase as it runs.

The contract behind every field the demo reads is [`docs/contracts/assess.md`](../../contracts/assess.md). Sections 8 and 9 read the `artifacts` block (the report and the usage record), which has no compatibility promise; the demo says so where it does.
