# FAQ / troubleshooting

## Does the tool read my data?

No row values. The collection scripts are read-only: they run `SELECT` on the catalog and statistics views, and the MySQL and PostgreSQL scripts also read the rows of tables of at most 1,024 MB to measure the longest value of each large-object column (`lob_scan_max_mb=0` turns that off). Only the length goes into the file, never a value. The collection holds the schema, query shapes and counters. <!-- claim: tests/unit/scripts/test_collection_scripts_rate_sample.py::test_each_script_stays_read_only, tests/unit/tools/database/test_mysql_lob_sizes.py::test_the_script_measures_without_writing_or_locking, tests/unit/tools/database/test_postgres_lob_sizes.py::test_the_script_measures_lob_columns_only_when_allowed_and_small_enough --> Query text and view, procedure and trigger definitions can still carry literal values, so review a collection before you share it. A collection made before this rule that still carries table rows has them dropped, with a count, from every copy the tool writes.

## Does it touch my database?

The assessment never connects to your database when you collect with a script: you run the read-only collection script yourself and give `/modernize` the file. `/test-routines` and `/move` never connect to your source database or your Aurora cluster either, and never run SQL on them. Step 1 writes the SQL your databases need (the DDL, the views, the routines and their read-only checks) as scripts you run with your own credentials, and `/move` reads the output you save. AWS DMS, which `/move` deploys in your account after your approval, reads the source and writes to Aurora with the secrets you give it. See [After the assessment](use-claude-code.md#after-the-assessment).

## What does the routine test cluster cost, and when is it deleted?

`/test-routines` creates one Aurora Serverless v2 cluster in your account, after your yes. It bills ACU-hours while it runs, plus its storage and its master secret. At 0 ACUs, on the versions that support it, it pauses after 5 idle minutes and then bills only storage and the secret; on the others it runs at 0.5 ACUs and never pauses. <!-- claim: tests/unit/tools/data_movement/test_proof_target_template.py::test_serverless_v2_scales_to_zero_or_half_an_acu, tests/unit/scripts/test_run_dms_step_proof_target.py::test_a_line_without_auto_pause_runs_at_half_an_acu --> It is deleted when you say yes to the teardown question at the end of the run, with every row in it and no final snapshot. If you decline, it stays and bills until you tear it down: run `/test-routines` again and it offers the teardown. <!-- claim: tests/unit/tools/data_movement/test_proof_target_template.py::test_it_is_throwaway_with_no_final_snapshot, tests/unit/scripts/test_test_routines_command.py::test_a_rerun_finds_the_deployed_cluster_and_never_asks_to_deploy_it_again --> It holds synthetic rows and the carried routines only, never your data.

## What if a routine fails its test?

It is held back. Step 1's routines script carries only the routines the test found no difference on, on the calls made, and names every other routine with its reason (`not_tested`, `not_proved`, `needs_attention`, or `depends_on_held` for a routine that calls a held one). A held routine never fails the step 1 gate: the gate names it held back, with the next step. <!-- claim: tests/unit/tools/routines/test_proof_gate.py::test_only_proved_routines_reach_the_deploy_script_and_the_rest_are_held_with_their_reason, tests/unit/tools/data_movement/test_dms_gate_routines.py::test_a_held_routine_is_named_held_back_and_never_fails_the_gate --> You can migrate it yourself, or let `/test-routines` offer a model repair of the routines that failed their checks; a repaired routine is deployed only once a test ran that same repair and found no difference. See [Test the routines](use-claude-code.md#test-the-routines-test-routines).

## Can I run it without a model?

Yes. `uv run python scripts/run_assessment.py --file <collection.json> --db <db> --llm-mode none --all -y` runs every phase with no model call and no credentials, and writes the same deliverables folder. Collect through Reality Check never needs a model: the engine assignment comes from the scores. Schema design is skipped, so the reports have no target schemas and no wave is suggested; the executive summary is built from the run's figures. <!-- claim: tests/e2e/test_pipeline.py::test_a_run_without_a_model_records_every_design_as_skipped_and_says_why, tests/unit/shared/test_wave_readiness.py::test_a_wave_with_no_design_is_neither_suggested_nor_held --> With no design there is no routine repair and nothing for `/wave-edit` to redesign. See [Other ways to run it](other-ways-to-run-it.md).

## Does the tool keep my stored procedure code?

For PostgreSQL, MySQL and MariaDB, yes, in one place of the job, its routine sources (`collector/routine_sources.json` and its compressed chunks), so that a move can carry each routine to Aurora as written (a MySQL routine read by the collection script, rather than the live collector, is marked when its rebuilt `CREATE` may not be exact). Every other file the tool writes holds the code with its string literals redacted. Each carried `CREATE` is checked by the repository's own credential scan (`src/shared/secret_scan.py`), whose rules always run: credential key-value pairs, AWS keys, private key headers, provider tokens and public IP addresses (and detect-secrets' plugins too, where it is installed). The routine sources' index names each routine it flags, with the kinds found and never the value. <!-- claim: tests/unit/agents/test_routine_sources_secret_scan.py::test_a_body_that_looks_like_it_holds_a_credential_is_flagged_by_kind, tests/unit/agents/test_routine_sources_secret_scan.py::test_the_own_rules_run_without_detect_secrets --> To keep no routine code at all, collect with `include_routine_bodies = 0` (see [Analyzing your own database](use-claude-code.md#analyzing-your-own-database)). <!-- claim: tests/unit/agents/test_routine_sources_wiring.py::test_run_assessment_keeps_bodies_only_in_the_routine_sources, tests/unit/agents/test_routine_sources_wiring.py::test_opting_out_writes_no_routine_sources_and_no_body -->

## Does the database name have to match the collection?

No. `/modernize` names the run from the file name (`shop-collection.json` becomes `shop`), and `scripts/run_assessment.py` from `--db`, but the tables are keyed by the database name the collection records, so any name gives the same plan. When the two differ, the `collect` line also shows the collected name as `collected_database_name`. One case stops the run with an error: a collection that records no database name and whose statements belong to several databases, none of them the name you gave; name the run after the database you collected.

## Amazon Bedrock model access

An `AccessDeniedException` or a similar error from `--llm-mode bedrock` usually means the account can't invoke the Anthropic models yet: **Claude Sonnet** (used for Reality Check validation and for Synthesis), **Claude Opus** (used for Schema Design) and **Claude Haiku** (used to validate free-text answers). Most Bedrock model access is on by default, but Anthropic models need a use-case form, submitted once per account. Select the model in the Bedrock console's model catalog for the same region as `AWS_DEFAULT_REGION`/`AWS_REGION`, or call `PutUseCaseForModelAccess`. See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html). Access is per-account and can take a few minutes to propagate after you submit the form.

The default model IDs are `global.` cross-Region inference profiles, so the same error can also come from IAM: a policy that allows only `us-*` foundation models, or an SCP that denies Regions, rejects the Region-less foundation-model ARN a global profile is authorized against. See **IAM for the global profiles** in [Other ways to run it](other-ways-to-run-it.md) for the three resources each model needs. <!-- claim: tests/unit/shared/test_models.py::test_every_model_id_is_a_global_profile -->

## Claude Code permissions

`/modernize` and the other commands run `uv run python scripts/...` and start a local uvicorn/npm process for the UI. If Claude Code keeps asking to approve the same command, check `.claude/settings.json` (or `.claude/settings.local.json`) for an allowlist entry, and see [`.claude/settings.ci.json`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/.claude/settings.ci.json) for the full list the headless CI run uses as a reference.

## The UI didn't start

`/modernize` falls back to chat-only and keeps going if the UI can't start (port 3000 or 8000 already in use, a failed Node build, or no Node at all). The chat still shows every key number. Check for another process on those ports, confirm Node 22 is installed (`node --version`), and look for a UI build error in the command's own output before re-running. The UI keeps running after the run so you can browse the results; stop it with `uv run python scripts/start_local_ui.py --stop`.

## Collector connection issues

- **PostgreSQL:** `collect-postgresql.sql` needs the `pg_stat_statements` extension enabled on the database (`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`, needs superuser or `rds_superuser` on RDS) and `SELECT` on `information_schema`.
- **MySQL/MariaDB:** `collect-mysql.sql` needs `performance_schema` enabled (on by default since MySQL 5.6) and `SELECT` on `information_schema` and `performance_schema`.
- **SQL Server:** `collect-sqlserver.sql` needs SQL Server 2016 or later, `SELECT` on the `sys` catalog views and DMVs, and `VIEW SERVER STATE` for the query section (without it the queries section is empty but everything else is collected).
- **Oracle:** `collect-oracle.sql` needs Oracle 12.2 or later and `SELECT` on the `V$` views, the `ALL_*` catalog views and `DBA_SEGMENTS` (grant `SELECT_CATALOG_ROLE` for full visibility). It reports one query per statement shape: `V$SQL` rows that differ only in literal values share a `FORCE_MATCHING_SIGNATURE`, so their counters are summed (a collection made with an older script is merged by the offline parser).
- Every script is read-only. If a plain `SELECT` works but the collection script fails, the cause is usually a missing grant on one of those views, not the credentials.

## Something else

Check [open issues](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues) first, then file a [bug report](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/new?template=bug_report.md) with the steps to reproduce. See [Contribute](contribute.md) for the template conventions.
