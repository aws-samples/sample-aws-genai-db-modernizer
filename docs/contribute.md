# Contribute

This page is the short map for extending the pipeline. [CONTRIBUTING.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/CONTRIBUTING.md) has the full process (branching, commit format, PR checklist); [AGENTS.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/AGENTS.md) is the working guide for coding agents and a quick map for everyone else.

## Architecture overview

The pipeline runs locally: a `LocalOrchestrator` calls each phase's agent as a direct function call over a local artifact store (no event bus, no hosted queue). See the
[guides index](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/guides/README.md) and
[high-level design](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/high-level-design.md)
for the full picture, and the [architecture decision records](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/architecture/decisions) for how the design got here.

## Agents and the LLM seam pattern

Every phase that can use a model splits into three functions, so the same code serves Amazon Bedrock, Claude Code, and fully deterministic runs:

1. `run_*_deterministic(...)`: the complete result with no model call.
2. `prepare_*_llm_input(det)`: the payload a model reasons over.
3. `apply_*_llm_output(det, llm_output)`: merges and validates the model's answer on top of the deterministic result.

See the [implementation guides](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/guides/README.md) for the agent patterns themselves, and `src/agents/analysis/aurora_*_analysis_agent.py` for a worked example of the shape.

## Contracts

All agent I/O flows through Pydantic models in `src/contracts/`. Breaking changes go through a proposal issue, a version bump and a migration note. See the
[agent contracts spec](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/contracts/agent-contracts-spec.md) and
[contracts README](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/contracts/README.md).

## Writing or changing Claude Code commands

Commands live in [`.claude/commands/`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/.claude/commands); each one calls a `uv run python scripts/<script>.py` entry point and follows a `src/skills/*.md` prompt. `uv run python scripts/validate_skills.py` checks that every script and skill path a command names exists. Run it (or `make lint`) after you edit a command. `.claude/settings.ci.json` is the permission allowlist headless runs use; a new script a command calls needs an allow rule there too.

## Testing

| Tier | Command | What it checks |
| --- | --- | --- |
| Lint | `make lint` / `./ci/lint.sh` | ruff, black, isort, mypy, markdownlint, the command validator |
| Unit/contract | `make test` / `./ci/test.sh -q` | unit, contract, property, graph; no network |
| Deterministic e2e | `make e2e` / `./ci/e2e.sh` | full pipeline on both samples, rendered HTML/PDF, UI smoke test |
| Headless LLM e2e | `make e2e-llm` | a real `/modernize --auto` run against a model, same deliverable checks plus a rubric judge |

See [`ci/README.md`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/ci/README.md) for what each script runs and why, and the testing guide for strategy per agent type.

## Release process

See [`docs/RELEASE_MANAGEMENT.md`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/RELEASE_MANAGEMENT.md) for versioning, the git workflow, and the release/hotfix procedures.
