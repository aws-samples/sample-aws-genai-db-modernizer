# WordPress with WooCommerce on MySQL: the MySQL sample

`wordpress.zip` holds `wordpress-collection.json`, a MySQL 8.0 collection from a synthetic WordPress shop with the WooCommerce plugin: 50 tables and 163 queries in the `wordpress` database. Its products, customers, orders and reviews are generated test data; there is no customer data. `sample-run/` is the recorded model run the help site's sample report renders, and `load-test-results/` a synthetic DynamoDB design the load-test tests read.

## How it was collected

- **Date:** 2026-11-06, collected at 12:04 UTC.
- **Collector:** `scripts/collect-mysql.sql` at commit 581ffa8 (collection version 1.0), run right after the workload with the default `min_executions` of 10. It is a single-read collection, taken before the two-snapshot sampling of #1908: each statement's call rate is its execution count over its own first-to-last-seen window, about 27 minutes of the workload.
- **Workload:** query statistics were reset first. Then a seeded k6 run with a shared-iterations profile (`fixed`) drove the site through its web pages and its REST API: seed 1001, 3,000 iterations shared by 40 virtual users, 16,071 HTTP requests in 1,643 seconds, with 0 failed requests, 0 failed writes and 0 redirects. The iterations browse the shop's pages and read through the REST API, and make these writes.
- **Writes by kind, from the k6 summary:** 1,202 orders placed, 841 order status changes, 511 order notes, 452 customers created, 195 product reviews, 176 product updates and 107 product drafts. The REST API's totals moved from 23,041 to 24,243 orders, 4,026 to 4,478 customers, 1,854 to 2,049 reviews and 1,079 to 1,186 products.
- **Not in this repository:** the k6 workload script, so the run cannot be repeated from here; the figures above describe it.
- **What it holds:** MySQL 8.0.45, the `wordpress` database (0.30 GB), 50 tables, 163 statements, no triggers and no stored procedures.

## Run it

```bash
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json --llm-mode none --all -y
```

Or in Claude Code: `/modernize docs/examples/wordpress/wordpress-collection.json`.

`sample-run/` (the help site's sample report, described in its own README) and `load-test-results/` were recorded from the earlier, 107-statement collection of the same shop.

## The routine set

The collection has no routines of its own, so `wordpress-routines.json` holds a small synthetic set (#1954), in the sections `scripts/collect-mysql.sql` writes (`procedures`, `routine_parameters`, `triggers`, `events` and `metadata.routine_capture`). Each is written for this schema, not copied from WordPress:

| Routine | Kind | Outcome |
|---|---|---|
| `wp_post_comment_count` | function | carried as it is |
| `wp_comments_count_after_insert` | trigger on `wp_comments` | carried as it is |
| `wp_purge_action_logs` | event, daily | carried as it is |
| `wp_refresh_comment_count` | procedure | rewritten: its `DEFINER` names the target user |
| `wp_bulk_import_mode` | procedure | can't be carried: it sets a global system variable, so it is the HIGH "migrate it yourself before wave 1" to-do |

The deterministic end-to-end run (`tests/e2e/pipeline.py`, and `scripts/build_docs_sample.py --deterministic`) adds the set to the collection before it collects, so the run goes through the routine phase like any collection with routines; `tests/e2e/test_sample_routines.py` checks each outcome in `report.json`, the engineering report and the results page. <!-- claim: tests/e2e/test_sample_routines.py::test_each_synthetic_routine_gets_its_outcome, tests/e2e/test_sample_routines.py::test_the_routine_that_cannot_be_carried_is_a_high_to_do_that_holds_no_wave, tests/e2e/test_sample_routines.py::test_the_engineering_report_states_each_outcome, tests/e2e/test_ui.py::test_results_page_states_the_routine_outcomes --> A run of the zip alone, such as `/modernize docs/examples/wordpress/wordpress-collection.json`, has no routines. The recorded `sample-run/` predates the set, so the help site's sample report has no Routines section until the sample is refreshed.
