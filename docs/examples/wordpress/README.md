# WordPress on MySQL 8.0: the MySQL sample

`wordpress.zip` holds `wordpress-collection.json`, a MySQL 8.0 collection of a WordPress site with the WooCommerce store: 50 tables and 107 queries in the `wordpress` database. The load and the data are synthetic; there is no customer data. `sample-run/` is the recorded model run the help site's sample report renders, and `load-test-results/` a synthetic DynamoDB design the load-test tests read.

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
