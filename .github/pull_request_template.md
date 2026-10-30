## What does this PR do?

<!-- Brief description. Link to issue/task if applicable. -->

Closes #

## Changes

-
-

## Type

- [ ] Feature
- [ ] Bug fix
- [ ] Infrastructure / CI
- [ ] Documentation
- [ ] Refactor

## Maintainer decision this implements

<!-- Link the issue comment where the maintainer decision was recorded, or write "None". This is a recorded decision, not the "Maintainer Decision" label. The reviewer checks this PR against that text. -->

## ADR

<!-- A change to behaviour under src/agents/referee/ or src/shared/ edits the ADR it touches. Write "ADR-0xx edited" (name the ADR), or "No ADR change: <reason>". ci/pre_merge.sh fails without one of them. -->

## Evidence

<!-- For each behavior claim above, paste the command and its output (trimmed) or a rendered excerpt. Do not assert behavior in prose. -->

<!-- Verdict lines, one per run, exactly as `./ci/needs_llm_run.sh` prints them (run `uv run python ci/pr_evidence.py render` to write this section from your head commit). The `PR Evidence` check fails when a line is missing or contradicts the script for this PR's diff:

e2e-llm: required | not required
bedrock-smoke: required | not required

For each run that is required, add a result line with the excerpt or outcome, outside this comment:

e2e-llm result: pass (judge 3.9, $3.74)   (or a MODERNIZE_RESULT: complete line)
bedrock-smoke result: pass

The result must begin with pass and contain no negation or tentative word (not, TBD, queued, will, should, expected, pending, skipped, fail). See ci/README.md. -->

## Checklist

- [ ] Tests added/updated (if applicable)
- [ ] Pre-commit hooks pass locally
- [ ] No new warnings

<!-- Delete sections below if not applicable -->

## Deployment Notes

<!-- Any special steps needed? IAM updates, manual cleanup, etc. -->

## Screenshots

<!-- For UI changes only -->
