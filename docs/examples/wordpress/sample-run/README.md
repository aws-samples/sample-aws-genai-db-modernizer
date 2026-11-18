# WordPress sample run

The recorded assessment the help site's [Sample report](https://aws-samples.github.io/sample-aws-genai-db-modernizer/sample/) renders: one complete `/modernize` run of the WordPress sample bundled in `docs/examples/wordpress/wordpress.zip` (the 163-statement collection of #1955).

| | |
|---|---|
| Release | v0.5.0-beta.1 |
| How it ran | Claude Code, interactive `/modernize` (`--llm-mode external`): Claude wrote the reality-check review, the schema designs (Aurora MySQL, DynamoDB with its design review, ElastiCache) and the executive summary; the analysis and the query routing are deterministic, as in every run |
| Synthesis contract | 1.99 |
| Assignment version | v2 (after the reality check) |

## What is here

Only what the deliverable renderers read, under the job folder's own layout:

- `synthesis/v2/report.json`: the run's synthesis report, as the run wrote it (plus a final newline). Every deliverable is rendered from it, including the per-engine schemas.
- `collector/output.json`: trimmed to the collected names (tables, views, procedures, triggers and their columns) the executive report keeps out of its text. The query text, metrics and host metadata of the real collector output are left out.
- `assignment/v1/assignment.json`, `assignment/v2/assignment.json` and `assignment/v2/access_patterns.json`: the effective assignment's lineage and its access-pattern record, which the detailed engineering report shows in full.
- `analysis-<engine>/analysis.json`: each engine's analysis (#1472).

The rest of the job (per-phase outputs, the context graph, model inputs and responses) is not needed to render the deliverables and is not committed.

## Sample run for v0.5.0-beta.1

This folder holds the sample run for v0.5.0-beta.1. Use this run from now on, because it carries richer descriptions and more information.

## Sample run for v0.4.0-beta.1

The v0.4.0-beta.1 run is in the [sample-run folder at the v0.4.0-beta.3 tag](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/v0.4.0-beta.3/docs/examples/wordpress/sample-run) on GitHub. Its format is still supported, so a report like it still loads, but use the v0.5.0-beta.1 run: its data is richer. <!-- claim: tests/unit/shared/test_adoption_facts.py::test_a_report_written_before_the_slice_plan_loads_and_is_filled_at_read_time, tests/contract/test_old_job_loads.py::test_every_stored_file_reads_and_every_contract_loads -->

## How it is used

`scripts/build_docs_sample.py` copies this folder into a temporary artifact store and renders it with `scripts/run_report.py`, so the help site shows the recorded assessment through the current renderers: a wording or layout fix on main shows up on the page without a new run. A report written by an older release gets the figures it lacks from the same facts backfill every saved report goes through (`src.report.facts.report_facts`, ADR-032 Decision 4). `tests/unit/scripts/test_build_docs_sample.py` checks that this folder renders every deliverable with no errors and no warnings.

## Refreshing it

The sample is refreshed once per release (see "Releases" in `CONTRIBUTING.md`): run `/modernize` on the WordPress sample with the release candidate, then

```bash
uv run python scripts/build_docs_sample.py --refresh-from artifacts/wordpress/<job>
```

and update the release and contract version in this README and in `docs/sample.md`. The refresh copies only the files above, refuses a job whose outputs carry one of the private details the release step lists, and regenerates this folder's entries in `.secrets.baseline`; a person then reads the refreshed report's diff before it merges.
