# Discourse on PostgreSQL 16: the PostgreSQL sample

`discourse.zip` holds `discourse-collection.json`, a PostgreSQL 16 collection of a Discourse forum: 312 tables and 2,859 queries in the `discourse` database. Its members, topics, posts and chat messages are generated test data; there is no customer data. `tests/fixtures/discourse_pre_2378/discourse.zip` keeps the collection bundled before #2378 (311 tables, 1,654 queries) for the tests that need shapes only it has.

## How it was collected

- **Date:** 2026-11-19, collected at 22:41 UTC.
- **Collector:** `scripts/collect-postgresql.sql` at commit 0ee61a6 (collection version 1.0), with the default `min_executions` of 10.
- **Workload:** statement statistics were reset in full first (`pg_stat_statements_reset()` for every database, so `stats_reset` moved too). Then a seeded k6 business day drove the forum through its web and JSON API in ten phases, one per simulated hour (seeds 6001 to 6010, 2 to 35 virtual users, 6,640 iterations and 92,352 HTTP requests in total), with 0 failed writes, 0 redirects and 0 rate-limited requests. The iterations read the forum's pages and JSON endpoints, and make these writes. After each phase, 65 of Discourse's scheduled jobs ran once by hand.
- **Writes by kind, from the k6 summaries:** 2,440 replies, 1,992 read timings, 1,173 chat channel joins, 1,123 topics, 978 chat reads, 876 likes, 759 chat messages, 654 topic votes, 611 bookmarks, 514 members registered and 205 deleted, 452 post edits, 383 poll votes, 335 notification reads, 327 drafts and 184 draft deletes, 282 private messages, 239 group adds and 142 removes, 196 tagged topics, 195 chat reactions, 191 bookmark deletes, 189 poll topics, 167 invites, 164 private message replies, 163 silences, 93 uploads, 91 profile edits, 88 chat direct messages, 81 topic timers, 79 accepted solutions, 66 flags with 66 review actions, and 48 private message archives.
- **Call rates:** the collector took its two counter snapshots 300 seconds apart during the busiest phase (35 virtual users), when the statements made 1,444.8 calls per second in all. 2,483 statements are rated over those 5 minutes, 495 of them at most one call because they made none between the snapshots, and the other 376 over the 6,757 seconds since the reset.
- **Costs:** the costs assume that peak-phase rate runs all month, so the deliverables label them a scenario at the measured rate. A deterministic run of the zip alone (the command below) projects $1,507 a month.
- **Not in this repository:** the k6 workload script, so the run cannot be repeated from here; the figures above describe it.
- **What it holds:** PostgreSQL 16.13, the `discourse` database (0.53 GB), 312 tables, 3 views (2 of them `pg_stat_statements`' own), 7 extensions, 2,859 statements, 6 PL/pgSQL trigger functions and 3 triggers.

## Run it

```bash
uv run python scripts/run_assessment.py --file docs/examples/discourse/discourse-collection.json --llm-mode none --all -y
```

Or in Claude Code: `/modernize docs/examples/discourse/discourse-collection.json`.

## The routine set

The collection holds Discourse's own routines with their code: 6 PL/pgSQL functions in the `discourse_functions` schema, each raising an error when a column Discourse marks read-only is written, and the 3 triggers on `category_settings` and `topic_timers` that call them. The 6 functions are rewritten (their owner becomes the target user) and the 3 triggers are carried as they are. <!-- claim: tests/e2e/test_sample_routines.py::test_each_collection_routine_gets_its_outcome --> `discourse-routines.json` adds a small synthetic set with bodies (#1954), in the sections `scripts/collect-postgresql.sql` writes (`procedures`, `triggers` and `metadata.routine_capture`). Each is written for this schema, not copied from Discourse:

| Routine | Kind | Outcome |
|---|---|---|
| `topic_visible_post_count(integer)` | SQL function | carried as it is |
| `prune_read_notifications(integer)` | PL/pgSQL procedure | carried as it is |
| `posts_bump_topic` | trigger on `posts` | carried as it is |
| `posts_bump_topic()` | PL/pgSQL trigger function | rewritten: its owner becomes the target user |
| `begin_upload_snapshot(text)` | PL/pgSQL procedure | can't be carried: it starts a base backup, which Aurora PostgreSQL does not let a session do, so it is the HIGH "migrate it yourself before wave 1" to-do |

The deterministic end-to-end run (`tests/e2e/pipeline.py`, and `scripts/build_docs_sample.py --deterministic --sample discourse`) adds the set to the collection before it collects, so the run goes through the routine phase like any collection with routines; `tests/e2e/test_sample_routines.py` checks each outcome in `report.json`, the engineering report and the results page, and the routine headline and to-do in the executive report and the final summary. <!-- claim: tests/e2e/test_sample_routines.py::test_each_synthetic_routine_gets_its_outcome, tests/e2e/test_sample_routines.py::test_the_routine_that_cannot_be_carried_is_a_high_to_do_that_holds_no_wave, tests/e2e/test_sample_routines.py::test_the_engineering_report_states_each_outcome, tests/e2e/test_ui.py::test_results_page_states_the_routine_outcomes, tests/e2e/test_sample_routines.py::test_the_executive_report_and_the_final_summary_print_the_routine_facts --> `tests/e2e/test_sample_routine_scripts.py` takes the same run on to step 1: the 13 carried routines (4 of the set's 5 and the collection's 9) are in `routines-deploy.sql`, in one transaction with the triggers created disabled, and `triggers-enable.sql` enables them; on a local PostgreSQL standing in for Aurora, both scripts and their read-only checks run and every carried routine and trigger is confirmed, while the gate's routines check still fails on `begin_upload_snapshot`, not carried. <!-- claim: tests/e2e/test_sample_routine_scripts.py::test_the_deploy_script_creates_the_carried_routines_in_order, tests/e2e/test_sample_routine_scripts.py::test_the_triggers_enable_script_turns_on_what_step_1_left_off, tests/e2e/test_sample_routine_scripts.py::test_the_postgresql_scripts_deploy_and_check_on_a_stand_in_target --> A run of the zip alone has the collection's 9 as its only routines.

### The set on a source database

For the step 1 rehearsal the set has to be on the forum's own PostgreSQL source, so that a collection taken there holds it and step 1 carries it to the rehearsal Aurora. Step 1 deploys a carried routine only once `/test-routines` has tested it with no difference found, so run `/test-routines` before `/move`. <!-- claim: tests/e2e/test_sample_routine_scripts.py::test_each_routine_lands_in_its_step_1_bucket --> `discourse-routines-install.sql` creates it there, in one transaction, and `discourse-routines-remove.sql` drops it again. Both are rendered from `discourse-routines.json` by `uv run python scripts/render_sample_routines.py` (`--check` names a stale file). Run the install with psql in the `discourse` database, after the schema is loaded, as a user that may give routines to the `postgres` and `discourse` roles. Once installed, the trigger sets a topic's `bumped_at` on each new post, so remove the set when the rehearsal is done. Installed on a local PostgreSQL and collected, the set comes back as the same routine sources, owners included, that the JSON set gives the end-to-end run. <!-- claim: tests/unit/scripts/test_render_sample_routines.py::test_the_committed_scripts_are_the_rendered_ones, tests/unit/scripts/test_render_sample_routines.py::test_the_discourse_install_collects_back_to_the_bundled_set -->
