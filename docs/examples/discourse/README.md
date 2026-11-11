# Discourse on PostgreSQL 16: the PostgreSQL sample

`discourse.zip` holds `discourse-collection.json`, a PostgreSQL 16 collection of a Discourse forum: 311 tables and 1,654 queries in the `discourse` database. The load and the data are synthetic; there is no customer data.

## The routine set

The collection was taken before routine code was collected: it lists 269 functions, aggregates and triggers by name only, most of them owned by the `hstore`, `citext`, `pg_trgm`, `unaccent` and `vector` extensions. The routine phase reads none of those rows, so they are neither carried nor a routine to migrate yourself; the run log counts them (#2124). `discourse-routines.json` adds a small synthetic set with bodies (#1954), in the sections `scripts/collect-postgresql.sql` writes (`procedures`, `triggers` and `metadata.routine_capture`). Each is written for this schema, not copied from Discourse:

| Routine | Kind | Outcome |
|---|---|---|
| `topic_visible_post_count(integer)` | SQL function | carried as it is |
| `prune_read_notifications(integer)` | PL/pgSQL procedure | carried as it is |
| `posts_bump_topic` | trigger on `posts` | carried as it is |
| `posts_bump_topic()` | PL/pgSQL trigger function | rewritten: its owner becomes the target user |
| `begin_upload_snapshot(text)` | PL/pgSQL procedure | can't be carried: it starts a base backup, which Aurora PostgreSQL does not let a session do, so it is the HIGH "migrate it yourself before wave 1" to-do |

The deterministic end-to-end run (`tests/e2e/pipeline.py`, and `scripts/build_docs_sample.py --deterministic --sample discourse`) adds the set to the collection before it collects, so the run goes through the routine phase like any collection with routines; `tests/e2e/test_sample_routines.py` checks each outcome in `report.json`, the engineering report and the results page. <!-- claim: tests/e2e/test_sample_routines.py::test_each_synthetic_routine_gets_its_outcome, tests/e2e/test_sample_routines.py::test_the_routine_that_cannot_be_carried_is_a_high_to_do_that_holds_no_wave, tests/e2e/test_sample_routines.py::test_the_engineering_report_states_each_outcome, tests/e2e/test_ui.py::test_results_page_states_the_routine_outcomes --> A run of the zip alone has no routine to carry or migrate.
