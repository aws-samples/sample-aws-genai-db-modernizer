# Understand the results

This describes the deliverables the pipeline produces today.

## The pipeline, in plain terms

1. **Collect** parses the source database's schema and query patterns.
2. **Triage** looks at workload signals (key-value lookups, text search, time-series, etc.). Every engine that can own a query is analyzed and scored; the signals are evidence for an engine, not a filter. The ElastiCache cache layer, which owns no query, is analyzed only when one of its signals fires.
3. **Analyze** scores every query against each engine triage selected, deterministically.
4. **Assign** routes each query to its best-scoring engine. Co-dependent queries (joins across the same tables) move together, and all of a table's writes land on one engine, the table's source of truth; another engine can still serve the table's reads from a copy kept in sync.
5. **Reality Check** removes an engine once the engines that remain can serve its queries just as well, and moves each of its queries onto its own best-fit remaining engine. A model's flag on a moved query re-routes it only among the engines that remain; it never brings a removed engine back. The source-compatible Aurora engine survives this consolidation unless another engine scores better, or is at least 40% and $100/month cheaper. A model can check that each move still works; the consolidation itself is deterministic and does not need it.
6. **Schema Design** produces concrete schemas per engine: DynamoDB table definitions, DocumentDB collections, OpenSearch mappings, the relational DDL that stays.
7. **Synthesis** produces the final report: ranking, TCO, the migration roadmap, risks, and an executive summary.

Collect through Reality Check never needs a model. Schema Design and the Synthesis executive summary do.

## The deliverables

Every run puts its deliverables in one folder, `artifacts/<db>/<job>/deliverables/`, under the same names every time:

<!-- deliverables-list -->
| File | What it is | Who it is for |
| --- | --- | --- |
| `executive-report.pdf` | Executive report (PDF): a landscape briefing with the recommendation, the numbers, why, the migration waves, the risks, the decisions needed and the next steps; no table names, totals only | Executives and stakeholders deciding whether to proceed |
| `engineering-report.md` | Engineering report (Markdown): the same facts with implementation detail (schemas, migration waves, risks) | The team that will build it |
| `assessment-data.json` | Assessment Data (JSON): the complete machine-readable assessment the other files are rendered from | Tools and scripts |
| `index.md` | This list for the run, with the assignment version the files come from | Anyone opening the folder |

`deliverables/` always holds the latest assignment version. The job's other folders hold the pipeline's working files (per-phase outputs, versioned assignments, model inputs and traces), which you don't need to read the assessment.
<!-- /deliverables-list -->

All of them are rendered from one `report.json` by `src/report`, so a changed fact changes in every deliverable at once. The local UI's results page reads the same `report.json` (see [The results page](#the-results-page)).

## The results page

`/modernize` links each job's results page at the end of the run (`http://localhost:3000/analysis/results/<job_id>`). It leads with the headline and keeps the detail one click away. From the top:

1. **Three download buttons** for the files above: Executive Summary (`executive-report.pdf`), Engineering Report (`engineering-report.md`) and Assessment Detail (`assessment-data.json`).
2. **Executive summary**: the recommendation, the number of queries, the current and projected cost and the saving.
3. **Query flow**: a Sankey diagram of the queries from the source to their engines.
4. **Cost breakdown**: the projected monthly cost per engine.
5. **Migration roadmap**: the waves, each with its gate; a table list in a gate opens in place.
6. **Banners**, when they apply: queries on a fallback engine, and close calls (queries whose engine scored less than 5 points above another engine the plan keeps).
7. **Query routing**, collapsed: every query and its engine, with search and filters; open a query to see its history and why it ended on its engine.
8. **Access pattern explorer**: the target data model, by access pattern or by source table, filtered by engine, operation or table (on a run with access patterns).
9. **Sources of truth and replicas**, collapsed: each written table's source of truth, its replicas, the CDC direction and the reads a replica serves.
10. **Risk assessment** (the open risks by severity, and the risks the assignment already resolved in a collapsed list), **Trade-offs and design decisions**, and **Table design**: the Aurora design (its tables, DDL, optimizations and the queries routed to it), each part in a collapsed section; it appears once schema design has run. The other engines' designs are in the access pattern explorer.

## How to read an assignment

Assignments map **queries** to engines, not tables. Several engines can serve the same table. The "recommended engine" in a `table_mappings` entry does not by itself make that engine the owner of the table; what counts is which queries are routed to each engine. Each engine's confidence score is the mean fit of the queries actually routed to it, not every table it was evaluated against; a score with no table-level evidence behind it is labelled "signal only."

## The cache layer

ElastiCache is recommended only for hot reads, as a cache-aside layer. It never owns a query and never holds the only copy of the data. If the cache is lost, the system of record (Aurora, DynamoDB or DocumentDB) still has every row.

## OpenSearch as a read model

OpenSearch serves search and analytics queries, but every table it serves keeps a durable owner elsewhere. OpenSearch is kept in sync with that owner and never holds the only copy. If the index is lost, you re-index; no data is lost.

## Migration waves

The deliverables lay out a suggested step-by-step path that starts with the relational move. For example: Wave 1 moves the source database to Aurora with no data model changes yet; Wave 2 adds a cache layer in front of the hot reads; Wave 3 moves key-value queries to DynamoDB; Wave 4 adds OpenSearch as a read model for search and analytics queries. The fully decomposed target architecture appears next to it as the direct, single-step option. The wave order is a suggestion; other orders work too, and the end-state schema is the same either way.

Each wave lists every table its queries use, not only the tables its engine owns, and the engineering report shows both numbers as "N source tables (M to move)". The tables that move go to the wave's engine. The rest stay owned by their current engine (usually Aurora) and are kept in sync from that owner via CDC. Each wave ends with a gate: what to validate before the next wave starts. For a SQL Server or Oracle source, wave 1 converts the whole schema to the chosen Aurora engine, and the plan says that this wave is not yet optimized for heterogeneous migrations.

## Sources of truth and replicas

All of a table's writes land on one engine, its source of truth. When another engine serves some of the table's reads, it reads a copy kept in sync from the source of truth via CDC, so those reads are eventually consistent. The engineering report and the results page list each written table's source of truth, its replicas, the CDC direction and how many reads run on a replica, and the risk register carries the consistency risk.

## Close calls

A close call is a query whose engine scored less than 5 points above another engine the plan keeps, which could serve it too. The assessment still decides, deterministically; the engineering report and the results page list them so you can move any of them yourself in the assignment review. Exact ties a routing rule settled are counted apart from them.

## Risks

The engineering report carries the risk posture (the overall risk is the highest severity among the open risks), a risk register of the open risks by engine, each with its mitigation and the tables it affects, the risks the assignment already resolved, and the mitigation strategies, wave by wave. The executive report's Risks page gives the same open risks without table names. Risks cover anything the deterministic scoring flagged as low-fit, the reads that run on a replica, close calls, schema design that was not run, and any assumption the model made that depends on data this tool couldn't see (traffic patterns, peak load, compliance requirements). Read them before treating the migration roadmap as a commitment.

## See it on real data

[Sample report](sample.md) shows each of these deliverables for the WordPress sample, built without a model, so you can see the output before you run anything.
