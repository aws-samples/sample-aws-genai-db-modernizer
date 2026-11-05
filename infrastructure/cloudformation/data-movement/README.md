# Data movement stacks: DMS into Aurora and Debezium on Amazon MSK Connect

Data movement has two kinds of stack. Step 1 moves the whole source database into your Aurora cluster with AWS DMS (`scripts/render_dms_step.py`, from `src/tools/data_movement/dms_step.py`). Later steps read each source table's changes into one compacted Kafka topic with a Debezium MySQL or PostgreSQL source connector on Amazon MSK Connect. This folder holds the stacks for both. Nothing in the project deploys them, and rendering a template makes no AWS call.

| File | What it is |
|---|---|
| `debezium-plugin-bucket.yaml` | Static stack: a versioned, encrypted, private S3 bucket for the plugin ZIP, plus its access-log bucket |
| `examples/*.input.json` | Connector inputs shaped like a wordpress wave (the `source`, `cdc` and `hub` sections of a data movement plan). A hub plan's `source`, `cdc` and `topics` (as `hub`) feed the connector from #1802 on |
| `examples/*.yaml` | The connector stacks rendered from them (the snapshot tests compare against these files) |
| `examples/dms-step-1/` | Step 1's golden examples, one folder per engine (`wordpress-mysql`, `postgresql-shop`): `input.json` is a step 1 plan, its transformation contract and the Aurora DDL in one file (from a recorded wordpress run, or a small fixture); the files rendered from it (`aurora-target.yaml`, `step-1.ddl.sql`, `dms-step-1.yaml`, and `step-1.sequences.sql` for PostgreSQL) are regenerated with `scripts/render_dms_step.py --examples` and checked with `--examples --check` |

## Step 1: DMS into your Aurora cluster

Step 1 moves the whole source database into the Aurora engine the assessment chose: AWS DMS runs a full load, then ongoing replication. `scripts/render_dms_step.py --job-id <id> --db <db>` writes three files (four on Aurora PostgreSQL) under `artifacts/<db>/<job>/data-movement/step-1/` from the step's plan, its transformation contract and the Aurora design. Rendering makes no AWS call.

| File | Who runs it | What it is |
|---|---|---|
| `aurora-target.yaml` | You | A **suggested** Aurora cluster template. Creating and running the cluster is your responsibility; its header and `Metadata` list every setting the cluster must have, with the reason. Cluster-level settings sit in a cluster parameter group; instance-level ones (`sql_mode`, `local_infile` on Aurora MySQL) sit in a DB parameter group, and every instance of the cluster must use it, because a failover makes another instance the writer. When the source can hold zero dates, `sql_mode` drops `NO_ZERO_DATE` and `NO_ZERO_IN_DATE`: Aurora MySQL 8.0's default strict mode rejects zero dates, and DMS records them as NULL on a target that does not accept them |
| `step-1.ddl.sql` | You | Every wave 1 table the transformation contract moves (the full step 1 target set), with instructions. On Aurora MySQL it starts with `CREATE DATABASE IF NOT EXISTS` and `USE`. The DMS user and its grants follow the tables, commented out for you to adapt and run. It lists the tables left out, the fields that are unsupported, and what the source has that step 1 does not move, with the collection's counts: procedures, functions, triggers, views, MySQL events (not collected: `SHOW EVENTS`) and sequence values. <!-- claim: tests/unit/tools/data_movement/test_dms_step_sequences.py::test_the_ddl_header_counts_what_step_1_does_not_move_from_the_collection, tests/unit/tools/data_movement/test_dms_step_sequences.py::test_a_mysql_source_names_its_events_as_not_collected --> On Aurora PostgreSQL it creates the extensions the column types need (`CREATE EXTENSION IF NOT EXISTS`, for example `vector` for `vector`, `halfvec` and `sparsevec` columns) before the tables, and says which version the Aurora line offers <!-- claim: tests/unit/tools/data_movement/test_dms_step_sequences.py::test_vector_and_halfvec_columns_create_the_vector_extension_before_the_tables, tests/unit/tools/data_movement/test_dms_step_sequences.py::test_an_extension_the_target_line_does_not_list_is_named_in_the_header -->; it recommends `REPLICA IDENTITY FULL` per table, commented out: your decision, at the cost of more WAL |
| `step-1.sequences.sql` | You, at cutover | Aurora PostgreSQL only: one `SELECT setval(pg_get_serial_sequence(...), COALESCE(MAX(<column>), 0) + 1, false)` per identity column of a moved table. DMS writes each row's id but never advances the column's sequence, so without it the first insert on Aurora without an explicit id collides with a loaded row. Aurora MySQL needs no such file: an `AUTO_INCREMENT` counter moves past every value inserted into its column <!-- claim: tests/unit/tools/data_movement/test_dms_step_sequences.py::test_a_postgresql_job_gets_one_setval_per_moved_identity_column, tests/unit/tools/data_movement/test_dms_step_sequences.py::test_only_moved_tables_get_a_setval, tests/unit/tools/data_movement/test_dms_step_sequences.py::test_a_mysql_target_gets_no_sequences_file_and_the_header_says_why --> |
| `dms-step-1.yaml` | Us, with your credentials | A replication subnet group, a provisioned replication instance (`MultiAZ` defaults to `true`), source and target endpoints that read Secrets Manager secrets, a role that lets DMS read those two secrets, and one full-load-and-CDC task |

Nothing we deploy changes your cluster's configuration or runs inside it. `dms-step-1.yaml` only makes DMS connect as the user you create and write rows. The task creates no table (`TargetTablePrepMode: DO_NOTHING`): run the DDL first. Its table mappings select exactly the contract's tables by name, with no wildcard, no transformation rules and no per-table full LOB rule (DMS rejects it; `test_no_mapping_rule_carries_lob_settings`), so do not change the schema of the source or of Aurora until the step is cut over. The task uses full LOB mode (never truncates, slower) when any table's LOB size is unknown, otherwise limited LOB mode at the plan's size (`test_the_task_runs_in_full_lob_mode_when_a_table_needs_it`, `test_the_task_runs_in_limited_lob_mode_at_the_plans_size_when_no_table_is_full`). From a PostgreSQL source it always uses limited LOB mode, at most at the catalog's 64 KB unless every LOB table needs more; a table whose measured longest values need more gets its own limit, a `table-settings` rule with `lob-settings` (mode `limited`), and the replication instance is sized for both the task's size and the largest table limit; a table the collection did not measure runs at 64 KB and the plan says so. <!-- claim: tests/unit/tools/data_movement/test_dms_step_lob.py::test_a_postgresql_source_runs_limited_lob_mode_even_with_a_full_table_in_the_plan, tests/unit/tools/data_movement/test_dms_lob_per_table.py::test_one_large_table_leaves_the_task_at_the_catalogs_size, tests/unit/tools/data_movement/test_dms_lob_per_table.py::test_the_task_mapping_gives_only_the_larger_table_its_own_limited_lob_rule, tests/unit/tools/data_movement/test_dms_lob_per_table.py::test_the_plan_sizes_the_class_for_the_largest_table_limit_and_says_which, tests/unit/tools/data_movement/test_dms_lob_per_table.py::test_a_table_limit_is_checked_against_the_class_s_table_limit_not_its_task_limit, tests/unit/tools/data_movement/test_dms_lob_measured.py::test_the_plan_says_when_the_collection_did_not_measure --> DMS validation compares a character array column (`varchar[]`, `text[]`) as text, since its own comparison fails on arrays. <!-- claim: tests/unit/tools/data_movement/test_dms_lob_per_table.py::test_a_character_array_column_is_validated_as_text_on_both_sides --> A larger value is truncated (the endpoint's `FailTasksOnLobTruncation` did not stop a live full load), so check the size `step-1.ddl.sql` states; DMS validation reports a truncated row. <!-- claim: tests/unit/tools/data_movement/test_dms_step_lob.py::test_the_postgresql_ddl_header_says_the_lob_size_is_not_measured_and_how_to_check_it --> A table with a `tsvector` column is created by `step-1.ddl.sql` but not moved (DMS does not migrate `tsvector`): rebuild it after cutover, for example with the application's search reindex. <!-- claim: tests/unit/tools/data_movement/test_dms_step_not_moved.py::test_the_dms_task_leaves_a_not_moved_table_out, tests/unit/tools/data_movement/test_dms_step_not_moved.py::test_the_ddl_still_creates_a_not_moved_table_and_names_it --> Transactional apply (batch apply off) keeps each key's changes in order for the later steps; data validation is on.

The instance class and storage come from `src/catalog/dms_sizing.toml` (the collector's database size and write rate); the parameter descriptions in `dms-step-1.yaml` and the plan's `replication.sizing.reason` say why. Override them with the `InstanceClass` and `AllocatedStorage` parameters. The plan's step 1 cost prices the sized class Multi-AZ, as the stack deploys it by default, and states the Single-AZ figure next to it; it also prices the storage beyond the instance's included GB. The Aurora cluster's own cost is in the assessment's total cost of ownership, not here.

In Claude Code, `/move step-1` walks you through this section after `/modernize`: it runs the scripts below, asks for your inputs and for an explicit approval before each AWS write, and deploys, tests and starts the stack with your credentials through `scripts/run_dms_step.py` (see `docs/use-claude-code.md`). A template over CloudFormation's 51,200-byte inline limit (a source with a few hundred tables) needs `template_bucket` in the inputs file, an S3 bucket you own in the stack's region: the deploy uploads the template there (`<stack name>/dms-step-1.yaml`, encrypted) and creates the change set from its URL, the same as `aws cloudformation deploy --s3-bucket <bucket>`. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_deploy_uploads_a_large_template_to_the_bucket_and_creates_the_change_set_from_its_url, tests/unit/scripts/test_run_dms_step.py::test_deploy_of_a_large_template_without_a_bucket_names_template_bucket_and_calls_nothing, tests/unit/scripts/test_run_dms_step.py::test_plan_of_a_large_template_with_a_bucket_shows_the_s3_bucket_option -->

### Before you deploy the DMS stack

- The cluster has every setting `aurora-target.yaml` lists, and every instance uses its DB parameter group. Reboot the instances after a parameter group changed.
- The source can be read for change capture. MySQL: `binlog_format=ROW`, `binlog_row_image=FULL` and binary log retention long enough to cover an outage (on RDS or Aurora MySQL, `CALL mysql.rds_set_configuration('binlog retention hours', 24);`). PostgreSQL: logical replication enabled (`rds.logical_replication=1` on RDS and Aurora). The stack does not check these.
- Two Secrets Manager secrets, one for the source and one for the Aurora writer endpoint, each with the keys `username`, `password`, `host` and `port`, for a user DMS may replicate with (the source user) and write with (the DMS user of the DDL file). Do not use the RDS-managed master secret: it has no `host` or `port`.
- The source user's grants: on MySQL, `SELECT` on the source tables plus `REPLICATION CLIENT` and `REPLICATION SLAVE` (on Amazon RDS for MySQL, automated backups on, so the binary log exists); on PostgreSQL, `SELECT` on the source tables and the `rds_replication` role (or superuser on a self-managed server). `aws dms test-connection` passes without the replication grants, and the task then fails when change capture starts.
- On a PostgreSQL source, the task turns on the DMS heartbeat (`HeartbeatEnable`), which creates a heartbeat table in the source (schema `public` by default) so an idle replication slot keeps advancing. From PostgreSQL 15, `public` grants `CREATE` to its owner only, so give the source user `CREATE` on it (`GRANT CREATE ON SCHEMA public TO <source user>;`), or the task fails when it starts. The preflight's `source_cdc` checklist names it. <!-- claim: tests/unit/tools/data_movement/test_dms_preflight_run.py::test_a_postgresql_source_names_the_heartbeat_schemas_create_grant --> If a secret uses a customer managed KMS key, pass its ARN as `SourceSecretKmsKeyArn` or `TargetSecretKmsKeyArn`.
- The account has the DMS service roles `dms-vpc-role` and `dms-cloudwatch-logs-role` (the DMS console creates them; with the CLI, create them with the managed policies `AmazonDMSVPCManagementRole` and `AmazonDMSCloudWatchLogsRole`).
- TLS. MySQL-family endpoints connect with `verify-ca`, so both certificates are required: the source server must serve TLS with a certificate authority you can import, and for Aurora use the RDS CA bundle. Import each bundle with `aws dms import-certificate --certificate-identifier <name> --certificate-pem file://<bundle>.pem` and pass the ARNs as `SourceCertificateArn` and `TargetCertificateArn`. PostgreSQL endpoints default to `require`.
- The private subnets reach Secrets Manager, because DMS reads the endpoint secrets from them: a Secrets Manager interface VPC endpoint with private DNS, or a NAT route. The security group needs egress on 443 to it and on the database ports to the source and the cluster, and the databases' security groups must allow the replication instance. If the interface endpoint has no private DNS, pass its DNS name as `SecretsManagerEndpointOverride`.

### Preflight

`scripts/check_dms_step_preflight.py` checks the list above; run it before you deploy. It reads the job's step 1 plan and contract, runs eight checks in a fixed order, stops at the first failure and writes `<db>/<job>/data-movement/step-1/preflight.json`.

The checks are: the Aurora cluster and its version line, its parameter groups (including pending reboots), the two secrets, the replication instance class offered in the region, the DMS service roles, the source's change capture settings, the DMS certificates, and whether the private subnets reach Secrets Manager. The source check reads what RDS can tell it and leaves the rest as a checklist; confirm it with `--confirm source_cdc` once you have checked it yourself (`--confirm` repeats and takes any check id, and an unknown id is refused with one JSON error line and exit 1: `test_confirm_takes_every_check_id`, `test_confirm_repeats`, `test_an_unknown_confirm_id_is_one_error_line`).

It reads metadata only and never a secret's value (`tests/unit/tools/data_movement/test_dms_preflight_read_only.py::test_the_preflight_only_reads`, `tests/unit/tools/data_movement/test_dms_preflight_run.py::test_every_aws_call_is_a_read_and_never_a_secret_value`). Use read-only credentials (`AWS_PROFILE`) with these permissions: `rds:DescribeDBClusters`, `rds:DescribeDBInstances`, `rds:DescribeDBClusterParameters`, `rds:DescribeDBParameters`, `secretsmanager:DescribeSecret`, `secretsmanager:GetResourcePolicy`, `kms:DescribeKey`, `dms:DescribeOrderableReplicationInstances`, `dms:DescribeCertificates`, `iam:GetRole`, `iam:ListAttachedRolePolicies`, `ec2:DescribeSubnets`, `ec2:DescribeVpcEndpoints` and `ec2:DescribeRouteTables`.

```bash
uv run python scripts/check_dms_step_preflight.py --job-id <id> --db <db> --region <region> \
  --aurora-cluster-id <cluster> --source-secret-arn <arn> --target-secret-arn <arn> \
  --source-kind rds_instance --source-id <instance> \
  --source-certificate-arn <arn> --target-certificate-arn <arn> --subnet-ids <subnet-a>,<subnet-b>
```

Use `--source-kind aurora_cluster` for an Aurora source; `self_managed`, the default, turns the source check into a checklist only.

It prints one JSON line with `status` `pass`, `fail` or `needs_confirmation`, `first_failure` and `message` for a failure (the check and what to do about it), `to_confirm` for the rows you must check yourself, and `notes` for checklist items to read.

### Deploy

The role in the stack has no explicit name, so `CAPABILITY_IAM` is enough. Use the parameter names of your rendered template (this example is MySQL; `TargetDatabaseName` defaults to the source database):

```bash
aws cloudformation deploy --template-file dms-step-1.yaml \
  --stack-name dm-<job-id>-step-1 --capabilities CAPABILITY_IAM \
  --tags modernizer:job-id=<job-id> modernizer:step=1 \
  --parameter-overrides SubnetIds=<subnet-a>,<subnet-b> SecurityGroupIds=<sg> \
    SourceSecretArn=<source secret ARN> TargetSecretArn=<Aurora secret ARN> \
    SourceCertificateArn=<source certificate ARN> TargetCertificateArn=<Aurora certificate ARN>
```

Add `InstanceClass`, `AllocatedStorage`, `MultiAZ=false` (for a demo), `TargetDatabaseName`, the KMS key ARNs or `SecretsManagerEndpointOverride` when you need them. You can deploy the stack before the DDL has run, but the task is not started by the stack: start it only after the DDL ran. First test both endpoints, then read the result: The replication instance runs AWS DMS engine 3.6.1, because on the default 3.5.4 the data validator can retry one endpoint secret read forever and stall validation on large sources; set `EngineVersion` to run another version. <!-- claim: tests/unit/tools/data_movement/test_dms_step_templates.py::test_the_replication_instance_runs_the_catalogs_engine_version -->

```bash
aws dms test-connection --replication-instance-arn <ReplicationInstanceArn output> --endpoint-arn <SourceEndpointArn output>
aws dms test-connection --replication-instance-arn <ReplicationInstanceArn output> --endpoint-arn <TargetEndpointArn output>
aws dms describe-connections --filter Name=replication-instance-arn,Values=<ReplicationInstanceArn output>
aws dms start-replication-task --replication-task-arn <ReplicationTaskArn output> \
  --start-replication-task-type start-replication
```

To remove the stack, stop the task first: see [Ending the step](#ending-the-step).

### Gate and cutover

Once the stack is deployed and the task started, run the gate. It only makes these read calls (DMS `DescribeReplicationTasks`, `DescribeTableStatistics`, `DescribeReplicationInstances`, `ListTagsForResource`; CloudWatch `ListMetrics`, `GetMetricData`; CloudFormation `DescribeStacks`; `tests/unit/tools/data_movement/test_dms_gate_read_only.py::test_the_gate_only_reads`), so run it with read-only credentials, for example through `AWS_PROFILE`, and it writes `data-movement/step-1/gate.json`:

```bash
uv run python scripts/check_dms_step_gate.py --job-id <id> --db <db> --region <region> --stack-name dm-<job-id>-step-1
```

- **Load**: the task runs in CDC, the full load is at 100%, every table of the step's contract is "Table completed" without errors, no other table is loaded, and the task was rendered from this step's contract.
- **Lag**: every `CDCLatencySource` and `CDCLatencyTarget` datapoint of the last 15 minutes (`--window-minutes`) is under the plan's `max_lag_seconds`, and the newest datapoint of each metric is at most 5 minutes old (`--stale-seconds`); the age is in the evidence.
- **Parity**: every table with a primary or unique key DMS can validate is `Validated`. Tables without a key, tables whose key DMS cannot validate, and views are named as exceptions ("not validated") with their row counts; pending validation records are totalled in the evidence.

The result is `pass`, `pending` or `fail`, in the JSON status line and in `gate.json`; the status line also lists `reasons` (each prefixed with its check) and the named `exceptions`. The script exits 0 whenever the gate ran, whatever the result (`test_the_status_line_carries_the_decision_and_the_message` for `pending`, `test_a_fail_result_still_exits_zero` for `fail`), and 1 when it could not (for example, no `ReplicationTaskArn` output on the stack: `test_a_stack_without_the_task_output_is_an_error`).

The gate only calls AWS APIs and never connects to either database, so it cannot check the sequences: the cutover steps below are yours. <!-- claim: tests/unit/tools/data_movement/test_dms_gate_read_only.py::test_the_gate_only_reads -->

The status line carries `cutover_decision: "customer"` and this message (`gate.json` carries the decision and the message's id):

> When to cut over is your decision. The data movement for this step keeps your Aurora copy in sync with your source and reports the evidence below: replication lag, rows loaded, and validation per table (with the tables DMS could not validate, and why). These figures help you decide; they are not a recommendation to switch. Choosing the moment to cut over, and preparing your applications for it, remains entirely your responsibility.

### At cutover

1. Stop your applications' writes to the source, then wait until the gate passes again (DMS applied the last change).
2. On Aurora PostgreSQL, run `step-1.sequences.sql` on the writer endpoint as the administrator (`psql ... -f step-1.sequences.sql`). It sets each identity column's sequence to the column's maximum plus one; running it again is safe. Aurora MySQL needs nothing here. <!-- claim: tests/unit/tools/data_movement/test_dms_step_sequences.py::test_a_postgresql_job_gets_one_setval_per_moved_identity_column -->
3. Recreate on Aurora what step 1 does not move: the procedures, functions, triggers, views and events that `step-1.ddl.sql`'s header counts from the collection.
4. Only then point your applications at Aurora.

### Ending the step

End the step when you have cut over, or to abandon the move: stop the task, then delete the stack. CloudFormation cannot delete a running replication task: deleting the stack while the task runs ends in `DELETE_FAILED` on `ReplicationTask`, and the replication instance keeps running (and billing) until you stop the task. Stopping the task ends ongoing replication, so changes on the source no longer reach Aurora from then on: stop it only once your applications use Aurora, or when you abandon the move.

```bash
aws dms stop-replication-task --replication-task-arn <ReplicationTaskArn output>
aws dms wait replication-task-stopped --filters Name=replication-task-arn,Values=<ReplicationTaskArn output>
aws cloudformation delete-stack --stack-name <stack_name>
aws cloudformation wait stack-delete-complete --stack-name <stack_name>
```

`<stack_name>` is the stack you deployed: the inputs file's `stack_name`, by default `dm-<job-id>-step-1`.

In Claude Code, `/move` does the same with `scripts/run_dms_step.py stop` and `teardown`, each after your explicit approval and with your `write_profile`; both are refused without `--approve` and under `MODERNIZER_CI_SANDBOX=1`. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_every_write_is_refused_without_approve_before_any_aws_call, tests/unit/scripts/test_run_dms_step.py::test_every_write_is_refused_under_the_ci_sandbox_even_with_approve, tests/unit/scripts/test_run_dms_step.py::test_ending_uses_the_write_profile -->

- `stop` stops a running task and waits up to 540 seconds for it to stop, printing a progress line per poll; when the wait runs out it says `stopping`, and running it again waits without stopping the task twice. A task that has stopped, failed or never started is left as it is. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_stop_stops_a_running_task_and_waits_until_it_has_stopped, tests/unit/scripts/test_run_dms_step.py::test_stop_reports_a_task_that_has_not_stopped_by_the_deadline, tests/unit/scripts/test_run_dms_step.py::test_stop_on_a_task_already_stopping_waits_without_stopping_it_again, tests/unit/scripts/test_run_dms_step.py::test_stop_on_a_task_that_is_not_running_is_a_no_op --> `status --until task_stopped` waits for the same thing read-only: it reads the task whatever the stack's state (a rolled-back or failed stack included), and is reached only when DMS reports the task stopped, failed or never started, or the task is gone (a deleted task counts as stopped, not as an error). <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_the_task_stopped_wait_reads_the_task_in_every_stack_state, tests/unit/scripts/test_run_dms_step.py::test_the_task_stopped_wait_is_not_reached_while_the_task_still_runs, tests/unit/scripts/test_run_dms_step.py::test_a_task_that_is_gone_counts_as_stopped_not_as_an_error, tests/unit/scripts/test_run_dms_step.py::test_the_task_stopped_wait_through_the_cli_prints_one_line --> Every line about the task names what DMS itself reports, in `dms_reports`: for example `DMS reports the task stopped (not running)`, `DMS reports the task failed (not running): <DMS's LastFailureMessage; StopReason>` or `DMS reports the task never started (ready, not running)`, with `last_failure` and `stop_reason` as separate fields; `/move` quotes it in every question and result line. A failed or never-started task counts as not running, because a failed task cannot be stopped and the step must still end. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_the_task_stopped_wait_names_what_dms_reports, tests/unit/scripts/test_run_dms_step.py::test_a_gone_task_is_named_as_gone, tests/unit/scripts/test_run_dms_step.py::test_stop_names_what_dms_reports_for_a_task_it_leaves_as_is, tests/unit/scripts/test_run_dms_step.py::test_teardown_names_what_dms_reported_when_it_deleted, tests/unit/scripts/test_run_dms_step.py::test_status_names_what_dms_reports, tests/unit/scripts/test_move_command.py::test_every_question_and_result_line_names_what_dms_reports -->
- `teardown` deletes the stack only when its task is stopped, failed or never started, and refuses while it runs; then it waits up to 540 seconds for the stack to be gone. A stack that is already gone is a no-op. Each run calls `DeleteStack` at most once, on the stack id it checked (so a stack re-created under the same name is not the one deleted): a stack already being deleted (`DELETE_IN_PROGRESS`) is only waited on, and a `DELETE_FAILED` it sees while waiting is reported as `delete_failed` with CloudFormation's reason, so another delete takes another run (in `/move`, another approval). <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_teardown_deletes_the_steps_stack_once_its_task_has_stopped_and_waits, tests/unit/scripts/test_run_dms_step.py::test_teardown_refuses_while_the_task_runs, tests/unit/scripts/test_run_dms_step.py::test_teardown_of_a_stack_that_is_already_gone_is_a_no_op, tests/unit/scripts/test_run_dms_step.py::test_teardown_of_a_stack_it_is_deleting_waits_without_deleting_it_again, tests/unit/scripts/test_run_dms_step.py::test_a_delete_failed_after_a_timed_out_wait_needs_a_new_run_to_delete_again, tests/unit/scripts/test_run_dms_step.py::test_teardown_reports_a_delete_that_failed_as_a_state_not_an_error -->
- Both act only on this job's step 1 stack, and refuse anything else before any write. "This job" is the job id the command is given (`--job-id`): the checks below compare AWS tags and names with it, and do not read the job's artifacts. The stack's resources, read page by page, must be exactly the six logical ids and types of `dms-step-1.yaml`, so an empty stack, a stack of other resources (such as your Aurora cluster's) or one with an extra resource is refused. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_an_empty_stack_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_stack_holding_only_an_iam_role_is_refused, tests/unit/scripts/test_run_dms_step.py::test_the_customers_aurora_stack_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_resource_on_a_later_page_is_still_checked, tests/unit/scripts/test_run_dms_step.py::test_the_six_resources_across_pages_are_this_jobs_stack, tests/unit/scripts/test_run_dms_step.py::test_the_resources_ending_accepts_are_the_rendered_stacks --> Its task's identifier must be this job's (`dm-<job-id>-step-1`) and its `modernizer:job-id` tag this job's id, so another job's step stack is refused. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_another_jobs_stopped_step_stack_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_task_whose_job_tag_does_not_name_this_job_is_refused --> A resource list or a task that cannot be read is an error. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_a_resource_list_that_cannot_be_read_is_an_error_and_nothing_is_written, tests/unit/scripts/test_run_dms_step.py::test_a_task_that_cannot_be_read_is_an_error_and_nothing_is_written, tests/unit/scripts/test_run_dms_step.py::test_a_task_resource_without_a_physical_id_is_refused --> A stack whose own `modernizer:job-id` tag (which `/move` deploy sets, and the deploy example above passes) names another job is refused. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_a_stack_tagged_for_another_job_is_refused, tests/unit/scripts/test_run_dms_step.py::test_deploy_creates_and_describes_a_change_set_and_executes_nothing, tests/unit/scripts/test_run_dms_step.py::test_plan_maps_the_inputs_to_the_template_parameters_with_no_aws_call --> Once the task itself is gone (deleted, or unknown to DMS), ownership is proven from AWS only, never from a local file: every replication instance and endpoint of the stack that still exists must carry this job's `modernizer:job-id` tag and at least one must, or else the stack itself must carry it; otherwise `teardown` refuses (`stop` has nothing to stop). <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_once_the_task_is_gone_the_instance_and_endpoint_tags_prove_the_stack_is_this_jobs, tests/unit/scripts/test_run_dms_step.py::test_a_resource_dms_no_longer_has_is_skipped_and_the_others_prove_it, tests/unit/scripts/test_run_dms_step.py::test_once_the_task_is_gone_the_stacks_own_tag_proves_it, tests/unit/scripts/test_run_dms_step.py::test_a_stack_whose_task_is_gone_and_whose_resources_are_not_this_jobs_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_stack_whose_task_and_dms_resources_are_all_gone_and_has_no_tag_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_stack_whose_dms_resources_dms_no_longer_has_and_with_no_tag_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_delete_in_progress_of_another_jobs_stack_is_refused, tests/unit/scripts/test_run_dms_step.py::test_a_forged_marker_never_lets_teardown_delete_another_jobs_stack, tests/unit/scripts/test_run_dms_step.py::test_a_task_dms_no_longer_has_is_nothing_to_stop_and_teardown_proves_ownership_from_tags --> A stack these checks refuse (for example one whose create rolled back) is yours to delete by hand.

**What is deleted:** the stack's six resources, and nothing else: the replication task, the source and target endpoints (with their connection test results), the replication instance, the replication subnet group and the role that reads the two secrets. None of them has a `DeletionPolicy` that keeps it. `teardown` makes no call but CloudFormation and DMS ones, and its only write is `DeleteStack` on the step's stack. <!-- claim: tests/unit/tools/data_movement/test_dms_step_templates.py::test_the_stack_is_exactly_the_six_resources_and_no_iam_wildcard, tests/unit/scripts/test_run_dms_step.py::test_no_resource_of_the_step_stack_is_retained_on_delete, tests/unit/scripts/test_run_dms_step.py::test_teardown_deletes_the_steps_stack_once_its_task_has_stopped_and_waits, tests/unit/scripts/test_run_dms_step.py::test_teardown_deletes_the_steps_stack_once_its_task_has_stopped_and_waits -->

**What stays** (none of it is in the stack): <!-- claim: tests/unit/tools/data_movement/test_dms_step_templates.py::test_the_stack_is_exactly_the_six_resources_and_no_iam_wildcard -->

- Your Aurora cluster, its instances and parameter groups, and every row the task wrote. They are yours: delete the cluster yourself if you abandon the move.
- The source database and its settings: change capture, the users and their grants. On a PostgreSQL source, the heartbeat table the task created stays; check `pg_replication_slots` too and drop an inactive slot the task left behind, or the source keeps WAL for it.
- The two Secrets Manager secrets, the certificates you imported into DMS, your KMS keys, the account-wide roles `dms-vpc-role` and `dms-cloudwatch-logs-role`, the subnets, the security groups and the Secrets Manager endpoint.
- The CloudWatch Logs log group DMS writes the task's logs to, which DMS creates outside the stack.
- The step's files under `artifacts/<db>/<job>/data-movement/step-1/` (`inputs.json`, `preflight.json`, `gate.json` and the rendered templates).

## Debezium connector stack

The connector stack is rendered per wave by `scripts/render_debezium_connector.py` from `src/tools/data_movement/debezium_connector.py`. It creates the custom plugin, the worker configuration (JSON converters, Secrets Manager config provider), a least-privilege service execution role, a KMS key and CloudWatch Logs group for worker logs, and the connector. Every resource is tagged `modernizer:job-id` and `modernizer:wave`. Account, region, endpoints, network and credentials are stack parameters with no defaults. Names two waves of one job must not share (PostgreSQL slot and publication, heartbeat and schema history topics, the schema history consumer group) carry the wave number.

## What the connector does

- Message keys are the plan's key columns for each table, or its primary key when the plan gives none (`message.key.columns`). A table with neither is rejected when the stack is rendered.
- `tombstones.on.delete=true`, so compaction removes deleted rows.
- Kafka Connect creates each table's topic on its first record ([KIP-158](https://cwiki.apache.org/confluence/display/KAFKA/KIP-158%3A+Kafka+Connect+should+allow+source+connectors+to+set+topic-specific+settings+for+new+topics) `topic.creation.*` settings) with `cleanup.policy=compact`, the plan's partitions and the `TopicReplicationFactor` parameter. If you create the topics yourself, use the same settings.
- Heartbeats every `cdc.heartbeat_interval_ms` to `__debezium-heartbeat.wave-<n>.<topic prefix>`. On PostgreSQL, a quiet captured database next to busy ones can hold WAL in the slot; Debezium's optional `heartbeat.action.query` fixes that, but it needs a write grant on a heartbeat table, so the generator does not set it.
- MySQL keeps its schema history in Kafka (`<topic prefix>.schema-history.wave-<n>`, IAM authentication, read with consumer group `<topic prefix>-wave-<n>-schemahistory`). Debezium creates that topic with delete retention; do not compact it. It is the only topic the execution role may read. PostgreSQL has no schema history topic.
- Incremental snapshots through the `source` signal channel (`signal.data.collection`).
- Credentials come from the Secrets Manager secret `DatabaseSecretArn` (keys `username` and `password`) through the [MSK config providers](https://github.com/aws-samples/msk-config-providers), which take the URL-encoded ARN; they never appear in the template or the connector configuration.

## Before you deploy

- An Amazon MSK provisioned cluster with IAM access control, and its IAM bootstrap brokers (port 9098).
- Private subnets and a security group for the connector that reach the brokers, the database and Secrets Manager (a VPC endpoint or a NAT gateway; MSK Connect workers have no internet access otherwise).
- Source settings. MySQL: `binlog_format=ROW`, `binlog_row_image=FULL` and binlog retention long enough to cover an outage (on Aurora MySQL, `CALL mysql.rds_set_configuration('binlog retention hours', 24);`). PostgreSQL: `rds.logical_replication=1` in the cluster parameter group, and a user with the `rds_replication` role that may create the publication (`publication.autocreate.mode=filtered`).
- A replication user stored in a Secrets Manager secret with `username` and `password` keys. MySQL needs `SELECT, RELOAD, SHOW DATABASES, REPLICATION REPLICA, REPLICATION CLIENT` (and `LOCK TABLES` for the initial snapshot).
- The signalling table in the source database (MySQL) or the `public` schema (PostgreSQL):

  ```sql
  CREATE TABLE debezium_signal (id VARCHAR(42) PRIMARY KEY, type VARCHAR(32) NOT NULL, data VARCHAR(2048) NULL);
  ```

  On PostgreSQL, if you create the publication yourself instead of letting the connector create it, add this table to it.

- PostgreSQL: every table keyed on anything but its primary key (a table with no primary key and `key_columns` in the plan, or `key_columns` that differ from the primary key) needs a replica identity **before** you deploy: `ALTER TABLE <table> REPLICA IDENTITY USING INDEX <unique index on the key columns>;` (or `REPLICA IDENTITY FULL`). Once the connector creates the publication, UPDATE and DELETE on a published table with no replica identity fail, so the application breaks. The generator refuses such a table unless its input says `replica_identity` (`index` or `full`) is set, and lists these tables in the stack description and in the render script's output (`replica_identity_before_deploy`).

## Deploy

1. Deploy the bucket stack:

   ```bash
   aws cloudformation deploy \
     --template-file infrastructure/cloudformation/data-movement/debezium-plugin-bucket.yaml \
     --stack-name <job-id>-debezium-plugin \
     --parameter-overrides JobId=<job-id>
   ```

2. Build the plugin ZIP from public sources only, and verify both parts.
   - The Debezium connector plugin archive from Maven Central, verified against its OpenPGP signature (`.asc`), not only the `.sha1` file. Debezium 2.7.4.Final is the version these templates were written against; Debezium 3.x needs Java 17, so check the MSK Connect runtime before choosing it. Its release artefacts are signed by the "Debezium CI Builder" key, fingerprint `F01F 864A BD9A D456 8F8C  B744 D420 4D74 8DCD C40D`:

     ```bash
     V=2.7.4.Final; C=debezium-connector-mysql   # or debezium-connector-postgres
     base=https://repo1.maven.org/maven2/io/debezium/$C/$V/$C-$V-plugin.tar.gz
     curl -fO "$base" && curl -fO "$base.asc"
     gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys F01F864ABD9AD4568F8CB744D4204D748DCDC40D
     gpg --verify "$C-$V-plugin.tar.gz.asc" "$C-$V-plugin.tar.gz"   # must say "Good signature" from that fingerprint
     ```

   - The MSK config providers, built from the tagged source commit rather than downloaded as a release binary: tag `v0.5.0`, commit `e96ade524dee42e4ab4a76fd928b10e064880b04` (this version decodes URL-encoded secret ARNs, which the connector configuration uses).

     ```bash
     git clone https://github.com/aws-samples/msk-config-providers.git && cd msk-config-providers
     git checkout v0.5.0 && test "$(git rev-parse HEAD)" = e96ade524dee42e4ab4a76fd928b10e064880b04
     mvn clean package   # target/msk-config-providers-0.5.0.jar and target/lib/*.jar
     ```

   Extract the Debezium archive, copy the config provider jar and its `target/lib` jars next to it, and zip the directory (`zip -r debezium-<engine>-<version>.zip <dir>`).

3. Upload it and keep the version id:

   ```bash
   aws s3api put-object --bucket <PluginBucketName> \
     --key plugins/debezium-<engine>-<version>.zip --body debezium-<engine>-<version>.zip \
     --query VersionId --output text
   ```

4. Render the connector stack from the wave's input:

   ```bash
   uv run python scripts/render_debezium_connector.py --input <input.json> --output connector.yaml
   ```

5. Deploy it:

   ```bash
   aws cloudformation deploy --template-file connector.yaml \
     --stack-name <job-id>-debezium-wave-<n> --capabilities CAPABILITY_IAM \
     --parameter-overrides \
       MskClusterArn=<cluster ARN> BootstrapServers=<IAM bootstrap brokers> \
       SubnetIds=<subnet-a>,<subnet-b> SecurityGroupIds=<sg> \
       DatabaseHost=<writer endpoint> DatabaseSecretArn=<secret ARN> \
       PluginBucketStackName=<job-id>-debezium-plugin PluginFileKey=plugins/<file>.zip \
       PluginObjectVersion=<version id> \
       DatabaseServerId=<unique number, MySQL only>
   ```

   The connector stack imports the bucket ARN from the bucket stack's `<stack name>-PluginBucketArn` export, so the plugin can only come from that bucket in the same account and region. The identity that deploys it also needs `iam:PassRole` on the execution role, `s3:GetObject` on the plugin object, the CloudWatch Logs delivery permissions MSK Connect documents (`logs:CreateLogDelivery` and related), and the EC2 network-interface permissions of the MSK Connect service-linked role.

6. Start an incremental snapshot of a table when you need one:

   ```sql
   INSERT INTO debezium_signal (id, type, data)
   VALUES ('snapshot-1', 'execute-snapshot', '{"data-collections": ["wordpress.wp_posts"], "type": "incremental"}');
   ```

## TLS to the database

`DatabaseSslMode` allows only `required` (MySQL) or `require` (PostgreSQL) for now: the connection is encrypted, but the server certificate is not checked. The verify modes need the Amazon RDS certificate authorities in the plugin and a trust store setting the generator does not write yet, so they come with #1421.

## Remove

Delete the connector stack, then the bucket stack (its buckets are retained; empty and delete them yourself). On PostgreSQL, drop the replication slot the connector created (`SELECT pg_drop_replication_slot('<slot.name>');`), or the database keeps WAL for it and its storage grows.
