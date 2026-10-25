# Get started

--8<-- "README.md:quickstart"

## The one approval point

`/modernize` stops once, after Reality Check. The chat shows the final engine assignment: which engines remain after consolidation, how many queries each one serves, and any query the model's check redirected. Outside chat-only mode it also links the job's summary page in the UI (`http://localhost:3000/analysis/monitor/summary/<job_id>`), with the Sankey of queries to engines and the full query-to-engine breakdown.

When a table's queries tie between engines, it asks you once per table which engine to use. Overriding a query onto an engine that can't run it is rejected with a reason. After you approve, the run goes on with no more questions through Schema Design and Synthesis, and ends with the deliverables and, outside chat-only mode, a link to the job's results page (`http://localhost:3000/analysis/results/<job_id>`). With `--auto` the approval and the tie choices are made for you (each marked for review) and the whole run is unattended.

## Next

- [Analyze your own database](use-claude-code.md#analyzing-your-own-database) with the read-only collection scripts.
- [Understand the results](understand-the-results.md) for what each deliverable shows.
- [Use Claude Code](use-claude-code.md) for every `/modernize` option and the one-phase-at-a-time commands.
- [Other ways to run it](other-ways-to-run-it.md) for the deterministic CLI, Amazon Bedrock, the Python library and the local UI on its own.
