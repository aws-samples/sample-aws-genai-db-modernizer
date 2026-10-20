# Get started

--8<-- "README.md:quickstart"

## The one approval point

`/modernize` stops once for a decision, after Reality Check. The chat shows the final engine assignment: which engines remain after consolidation, how many queries each one serves, and any query that the model's check redirected. When a table's queries tie between engines, it asks you once per table which engine to use (with `--auto` it decides for you and marks each decision for review), and overriding a query onto an engine that can't run it is rejected with a reason. It asks you to approve it before Schema Design runs. Outside chat-only mode it also links the job's summary page in the UI (`http://localhost:3000/analysis/monitor/summary/<job_id>`), with the Sankey and the full query-to-engine breakdown, so you can see it first.

After you approve, the run goes on by itself, with no more questions, through Schema Design and Synthesis to the finished deliverables in `artifacts/<db>/<job>/deliverables/` (listed above, and in that folder's `index.md`) and, outside chat-only mode, a link to the job's results page (`http://localhost:3000/analysis/results/<job_id>`). All of them are built from the same `report.json`. With `--auto` the approval is automatic and the whole run is unattended.

## Next

- [Use Claude Code](use-claude-code.md) for the full command reference and how to analyze your own database.
- [Other ways to run it](other-ways-to-run-it.md) for the deterministic CLI, Amazon Bedrock, or the local UI on its own.
