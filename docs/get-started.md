# Get started

--8<-- "README.md:quickstart"

## The one approval point

`/modernize` has a single decision gate, after Reality Check: the chat presents the final engine assignment (which engines survived consolidation, the query distribution across them, anything the LLM validator redirected) and asks you to approve it before Schema Design runs. Unless you're in chat-only mode, it also points you at the Sankey/assignment view in the UI so you can look at the full breakdown before deciding.

Once you approve, the run continues on its own — no further approval asks — through Schema Design and Synthesis to the finished deliverables: the decision report, engineering report, executive PDF/deck, and the UI's own view, all built from the same `report.json`. With `--auto`, the gate auto-approves and the whole run is unattended.

## Next

- [Use Claude Code](use-claude-code.md) for the full command reference and how to analyze your own database.
- [Other ways to run it](other-ways-to-run-it.md) for the deterministic CLI, Amazon Bedrock, or the local UI on its own.
