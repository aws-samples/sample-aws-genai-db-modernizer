# Get started

--8<-- "README.md:quickstart"

## The one approval point

`/modernize` stops once for a decision, after Reality Check. The chat shows the final engine assignment: which engines remain after consolidation, how many queries each one serves, and any query that the model's check redirected. It asks you to approve it before Schema Design runs. Outside chat-only mode it also points you to the Sankey and assignment view in the UI, so you can see the full breakdown first.

After you approve, the run goes on by itself, with no more questions, through Schema Design and Synthesis to the finished deliverables: the decision report, the engineering report, the executive PDF and deck, and the UI view. All of them are built from the same `report.json`. With `--auto` the approval is automatic and the whole run is unattended.

## Next

- [Use Claude Code](use-claude-code.md) for the full command reference and how to analyze your own database.
- [Other ways to run it](other-ways-to-run-it.md) for the deterministic CLI, Amazon Bedrock, or the local UI on its own.
