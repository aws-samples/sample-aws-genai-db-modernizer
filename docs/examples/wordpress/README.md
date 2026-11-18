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

`sample-run/` (the help site's sample report, described in its own README) is the v0.5.0-beta.1 run of this 163-statement collection. `load-test-results/` is a synthetic DynamoDB design that `tests/fixtures/generate_wordpress_dynamodb_schema.py` writes from the contract, so it does not come from a collection.

## The routine set

The collection has no routines of its own, so `wordpress-routines.json` holds a small synthetic set (#1954), in the sections `scripts/collect-mysql.sql` writes (`procedures`, `routine_parameters`, `triggers`, `events` and `metadata.routine_capture`). Each is written for this schema, not copied from WordPress:

| Routine | Kind | Outcome |
|---|---|---|
| `wp_post_comment_count` | function | carried as it is |
| `wp_comments_count_after_insert` | trigger on `wp_comments` | carried as it is |
| `wp_purge_action_logs` | event, daily | carried as it is |
| `wp_refresh_comment_count` | procedure | rewritten: its `DEFINER` names the target user |
| `wp_bulk_import_mode` | procedure | can't be carried: it sets a global system variable, so it is the HIGH "migrate it yourself before wave 1" to-do |

The deterministic end-to-end run (`tests/e2e/pipeline.py`, and `scripts/build_docs_sample.py --deterministic`) adds the set to the collection before it collects, so the run goes through the routine phase like any collection with routines; `tests/e2e/test_sample_routines.py` checks each outcome in `report.json`, the engineering report and the results page, and the routine headline and to-do in the executive report and the final summary. <!-- claim: tests/e2e/test_sample_routines.py::test_each_synthetic_routine_gets_its_outcome, tests/e2e/test_sample_routines.py::test_the_routine_that_cannot_be_carried_is_a_high_to_do_that_holds_no_wave, tests/e2e/test_sample_routines.py::test_the_engineering_report_states_each_outcome, tests/e2e/test_ui.py::test_results_page_states_the_routine_outcomes, tests/e2e/test_sample_routines.py::test_the_executive_report_and_the_final_summary_print_the_routine_facts --> `tests/e2e/test_sample_routine_scripts.py` takes the same run on to step 1: the function and the rewritten procedure are in `routines-deploy.sql`, in dependency order with the target user as `DEFINER`; the trigger and the event are left to `triggers-enable.sql`; and `wp_bulk_import_mode` is not carried, named with its reason by the gate's routines check. <!-- claim: tests/e2e/test_sample_routine_scripts.py::test_each_routine_lands_in_its_step_1_bucket, tests/e2e/test_sample_routine_scripts.py::test_the_deploy_script_creates_the_carried_routines_in_order, tests/e2e/test_sample_routine_scripts.py::test_the_triggers_enable_script_turns_on_what_step_1_left_off, tests/e2e/test_sample_routine_scripts.py::test_the_instructions_and_the_gate_name_them --> A run of the zip alone, such as `/modernize docs/examples/wordpress/wordpress-collection.json`, has no routines. The recorded `sample-run/` predates the set, so the help site's sample report has no Routines section until the sample is refreshed.

### The set on a source database

For the step 1 rehearsal the set has to be on the shop's own MySQL source, so that a collection taken there holds it and step 1 carries it to the rehearsal Aurora. Step 1 deploys a carried routine only once `/test-routines` has tested it with no difference found, so run `/test-routines` before `/move`. <!-- claim: tests/e2e/test_sample_routine_scripts.py::test_each_routine_lands_in_its_step_1_bucket --> `wordpress-routines-install.sql` creates it there and `wordpress-routines-remove.sql` drops it again. Both are rendered from `wordpress-routines.json` by `uv run python scripts/render_sample_routines.py` (`--check` names a stale file). Run the install with the mysql client in the `wordpress` database, as the master user `admin`. Creating `wp_refresh_comment_count` with its `wp_user`@`localhost` definer needs the `SET_USER_ID` privilege. Once installed, the trigger adds 1 to a post's `comment_count` on each new comment, and the event, with the event scheduler on, deletes Action Scheduler log rows older than 30 days once a day, so remove the set when the rehearsal is done. Installed on a local MySQL and collected with `scripts/collect-mysql.sql`, the set comes back as the same routine sources, sessions included, that the JSON set gives the end-to-end run. <!-- claim: tests/unit/scripts/test_render_sample_routines.py::test_the_committed_scripts_are_the_rendered_ones, tests/integration/test_sample_routine_install_mysql.py::test_the_wordpress_install_collects_back_to_the_bundled_set -->
