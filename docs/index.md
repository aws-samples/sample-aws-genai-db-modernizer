---
title: Run an assessment with Claude Code
hide:
  - navigation
  - toc
---

# Run a full database modernization assessment with Claude Code

Give this tool the schema and queries of a monolithic relational database. It tells you which queries belong on which AWS purpose-built engine and designs the target schemas.

```
/modernize docs/examples/wordpress/wordpress-collection.json
```

Run this command in [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with the repository open. The chat reports each phase, and by default a local UI opens next to it with the assignment Sankey diagram, the schema designs and the full report as each one is ready.

[Get started with Claude Code](get-started.md){ .md-button .md-button--primary }
[See a sample report](sample.md){ .md-button }

## Demo

![A full /modernize run on the WordPress sample in Claude Code, time-lapsed, followed by the local UI](assets/modernizer-demo.gif)

*A full `/modernize` run on the WordPress sample (47 minutes, time-lapsed), then the local UI: the executive summary, the query flow, the cost per engine and the access pattern explorer.*

## What it answers

- **Which queries go where.** Every query pattern is scored against each candidate engine. The assignment is deterministic: the scores decide it, not a model.
- **What the target schemas look like.** DynamoDB tables, DocumentDB collections, OpenSearch mappings and the relational schema that stays, all designed from your workload.
- **What it costs and what the risks are.** The same run produces TCO projections and a risk analysis. Migration waves show a suggested step-by-step path next to the fully decomposed target.

See [Understand the results](understand-the-results.md) for how to read every deliverable.

--8<-- "README.md:overview"

!!! warning "Sample project"
    This is a sample project intended for educational and evaluation purposes. It requires proper review, testing, and modification before use in production environments. Use at your own risk.
