# Run-3 judge fixture (wordpress, job cf163e54)

Inputs for `tests/unit/ci/test_judge_inputs.py`. They come from the headless
wordpress run that the judge scored 3.0 (issue #224), and the tests rebuild
the judge prompt from them. In every file the 64-hex query ids are replaced by
`qNNN` aliases. Each id gets one alias, numbered in order of first appearance
in `report.json`. The real ids would trip the secret scanners.

| File | Provenance |
|---|---|
| `report.json` | The run's synthesis `report.json`, with ids aliased. |
| `llm_input.json` | The run's synthesis `llm_input.json`, trimmed to `effective_architecture` minus `recommended_engine_by_table`. The judge doesn't use that field, and detect-secrets flags `wp_woocommerce_api_keys` in it. |
| `*_decision-report_*.html`, `*_engineering-report_*.md` | The run's rendered deliverables, unchanged apart from the id aliases. |
| `pdf-pages.json` | `judge.extract_pdf_pages` output (title and raw text per page) for the run's `summary-executive-report.pdf`. The 1.2 MB PDF itself isn't checked in. |
| `assignment.json` | Reconstructed, because the headless run didn't keep its `assignment/v2/assignment.json`. See below. |

## assignment.json

Reconstructed as run 3's assignment v2.

1. Start from the deterministic pipeline's assignment v1 for the same
   collection. Its distribution (dynamodb 45, elasticache 31, aurora_mysql 22,
   documentdb 6, opensearch 3) is identical to run 3's
   `reality_check.before_distribution`.
2. Apply run 3's recorded reality-check moves (`report.json`
   `reality_check.consolidations` and the reality-check reasons in
   `ranking[].assignment_reason_summary`):
   - documentdb to dynamodb, full: 6 queries.
   - opensearch to aurora_mysql, full: 3 queries.
   - aurora_mysql to dynamodb, partial. The 7 `queries_retained` stay on
     aurora_mysql. The 11 queries that appear in dynamodb design groups
     (`query_groups`) move.
   - aurora_mysql to elasticache: the 3 queries on wp_posts and the order
     item tables (ranking reason "data synced for wp_posts,
     order_itemmeta, order_items").
   - aurora_mysql to dynamodb: the 1 remaining wp_postmeta query (ranking
     reason "data synced for wp_postmeta").

The result matches run 3's `after_distribution` exactly (dynamodb 63,
elasticache 34, aurora_mysql 10). The tables each engine's queries touch
match `llm_input.json` `effective_architecture.engines[].tables` for every
engine. Only `query_id`, `assigned_engine`, `source_tables` and `in_scope`
are kept.
