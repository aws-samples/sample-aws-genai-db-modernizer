# Understand the results

This describes the deliverables the pipeline produces today. It will be updated as [Epic #340](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/340) changes recommendations.

## The pipeline, in plain terms

1. **Collect** parses the source database's schema and query patterns.
2. **Triage** looks at workload signals (key-value lookups, text search, time-series, etc.) and picks which engines are even worth analyzing.
3. **Analyze** scores every query against each candidate engine, deterministically, with an optional model advisor.
4. **Assign** resolves each query to the best-fit engine, resolving co-dependencies between queries that touch the same tables.
5. **Reality Check** consolidates engines that only picked up a handful of low-confidence queries into stronger neighbors; a model can optionally validate that the move actually works, but the deterministic consolidation stands on its own without it — unservable queries redirect to Aurora, the relational safety net.
6. **Schema Design** produces concrete schemas per engine: DynamoDB table definitions, DocumentDB collections, OpenSearch mappings, the relational DDL that stays.
7. **Synthesis** produces the final report: ranking, TCO, risks, and an executive summary.

Collect through Reality Check never needs a model. Schema Design and the Synthesis executive summary do.

## The deliverables

- **Decision report (HTML):** the top-level recommendation, ranking, and reasoning, for someone deciding whether to proceed.
- **Engineering report (Markdown):** the same facts with implementation-level detail — schemas, migration waves, risks — for the team that will build it.
- **Executive summary (PDF/PPTX deck):** a short deck version of the decision report for stakeholders.
- **Interactive analysis report (HTML):** per-engine analysis detail, including query journeys through the context graph.

All four are rendered from one `report.json` by `src/report`, so a fact that changes changes in every deliverable at once — there is no separate copy to keep in sync.

## How to read an assignment

Assignments map **queries** to engines, not tables. A single table can be served by several engines at once — one engine's `table_mappings` entry naming it as the "recommended engine" is not the same as that engine owning the table. Each engine's confidence score is the mean fit of the queries actually routed to it, not every table it was evaluated against; a score with no table-level evidence behind it is labelled "signal only."

## The cache layer

ElastiCache is recommended only for genuinely hot reads, as a cache-aside layer. It never owns a query and never holds the only copy of data — if the cache is lost, the system of record (Aurora, DynamoDB, or DocumentDB) still has every row.

## OpenSearch as a read model

OpenSearch serves search and analytics queries, but every table it serves keeps a durable owner elsewhere. OpenSearch stays in sync with that owner rather than holding the only copy — losing the index means re-indexing, not losing data.

## Migration waves

The deliverables lay out a suggested incremental path, relational first — for example: Wave 1 moves the source database to Aurora with no data model changes yet; Wave 2 adds a cache layer in front of the hot reads; Wave 3 moves key-value queries to DynamoDB; Wave 4 adds OpenSearch as a read model for search and analytics queries. This sits next to the fully decomposed target architecture, offered as the direct, single-step alternative. The wave path is a suggestion, not the only valid order; the end-state schema is the same either way.

## Risks

The engineering report and the decision report both carry a risk section: anything the deterministic scoring flagged as low-confidence, any query that reality check had to redirect, and any assumption the model made that depends on data this tool couldn't see (traffic patterns, peak load, compliance requirements). Read it before treating the wave plan as a commitment.

## See it on real data

[Sample report](sample.md) renders every one of these deliverables for the WordPress sample, deterministically, so you can see the output before running anything yourself.
