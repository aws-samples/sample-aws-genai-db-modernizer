# Get started

--8<-- "README.md:quickstart"

## The one approval point

`/modernize` stops once, after Reality Check. The chat shows the final engine assignment: which engines remain after consolidation, how many queries each one serves, and any query the model's check redirected. Outside chat-only mode it also links the assessment's summary page in the UI (`http://localhost:3000/analysis/monitor/summary/<job_id>`), with the Sankey of queries to engines and the full query-to-engine breakdown.

The gate also states every assumption the plan rests on once, each with its origin: growth, the peak source, the rate window, and the region and price date. Approving the gate approves them, and nothing asks about them separately. <!-- claim: tests/unit/api/test_assignment_assumptions.py::test_the_chat_gate_prints_the_same_lines_once, tests/unit/scripts/test_run_assessment_assumptions.py::test_the_files_values_are_recorded_and_the_gate_states_them -->

When a table's queries tie between engines, it asks you once per table which engine to use. Overriding a query onto an engine that can't run it is rejected with a reason. After you approve, the run goes on with no more questions through Schema Design and Synthesis, and ends with the deliverables and, outside chat-only mode, a link to the assessment's results page (`http://localhost:3000/analysis/results/<job_id>`). With `--auto` the approval and the tie choices are made for you (each marked for review) and the whole run is unattended.

After schema design, the run asks a model to repair the routines its checks could not carry, for at most 3 rounds, with no question to you. A repaired routine is a candidate in the deliverables and is never loaded on a database. <!-- claim: tests/unit/scripts/test_modernize_routine_repair.py::test_the_phase_asks_nothing_and_deploys_nothing, tests/unit/scripts/test_modernize_routine_repair.py::test_one_fresh_subagent_per_round_at_most_max_rounds -->

## Next

- [Analyze your own database](use-claude-code.md#analyzing-your-own-database) with the read-only collection scripts.
- [Understand the results](understand-the-results.md) for what each deliverable shows.
- [After the assessment](use-claude-code.md#after-the-assessment) for `/test-routines`, `/move step-1`, `/wave-edit` and `/explore`.
- [Use Claude Code](use-claude-code.md) for every `/modernize` option and the one-phase-at-a-time commands.
- [Other ways to run it](other-ways-to-run-it.md) for the deterministic CLI, Amazon Bedrock, the Python library and the local UI on its own.
