
# /move

Walks the customer through step 1 of a finished assessment's migration: the homogeneous MySQL or PostgreSQL source into the Aurora engine the assessment chose, with AWS DMS (a full load, then ongoing replication). Four stages, each ending at a checkpoint: **Prepare**, **Check**, **Move**, **Prove**. The scripts do every deterministic step and every AWS call; you explain their output, collect the customer's inputs and ask for each approval. `infrastructure/cloudformation/data-movement/README.md` ("Step 1: DMS into your Aurora cluster") is the source of truth for what the customer does.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it. Write the inputs file with the Write tool. Never run `aws` yourself: every AWS call goes through `scripts/run_dms_step.py`, and the commands the customer runs on their own (creating the cluster, the secrets, importing a certificate) are theirs to run. Run each command exactly as shown, on its own, and read the one JSON line it prints (Prove's poll prints a progress line per poll first: its result line is the last).

## Arguments

- `step-1` — the step to move (optional). Without it, list the job's steps and ask which one (see Step 0). Only step 1 is supported.
- `--job-id <id>` and `--db <db>` — the job (optional). Without them, take `job_id` and `database_name` from `.modernizer-state.json` (the last `/modernize` run).
- `--inputs <file.json>` — the inputs file (optional, see Check). Without it, the inputs file is `artifacts/{db}/{job_id}/data-movement/step-1/inputs.json`, which Check writes. With it, pass `--inputs {inputs}` to every `scripts/run_dms_step.py` command below that is not `steps` or `summary`.
- `--auto` — headless: never ask anything (see Headless).
- `--approve-deploy` — only with `--auto` and `--inputs`: the customer's approval, given up front, for the three AWS writes of Move (create and execute the change set, test the endpoints, start the task).
- `--until ready|moving` — stop early (optional). `ready`: once the preflight passes, before any AWS write; end with `MOVE_RESULT: stopped stage=check reason=until ready`. `moving`: once the task reads `running` (Move's step 6); end with `MOVE_RESULT: stopped stage=move reason=until moving`. A later `/move step-1` without `--until` resumes at the stage the stack is in (Resume). The `--auto` rules are unchanged: `--until moving` still needs `--inputs` and `--approve-deploy`, and without them the run stops where it does today. When Resume finds the step already past that stage (Move or Prove for `ready`, Prove for `moving`), end at once with the same line, with no AWS write. Any other value ends with `MOVE_RESULT: stopped stage=select reason=--until must be ready or moving`.

## Credentials and secrets

Every AWS call uses the customer's own credentials: the ambient ones, or the named profiles in the inputs file, `read_profile` for the read-only calls (preflight, status, gate) and `write_profile` for the writes (deploy, execute, test, start, restart-validation, resume, retry, stop, teardown). Recommend a read-only profile for `read_profile`; the preflight and the gate only read (the README lists their permissions). The inputs file holds ids and ARNs only. Never ask for, write, print or repeat a password or a secret's value; if the customer pastes one, do not write it anywhere, tell them it is not needed (DMS reads it from the secret) and ask for the secret's ARN instead. The scripts never read a secret's value. An error line with `"kind": "credentials"` means the credentials are missing or have expired: tell the customer to refresh their credentials and run `/move step-1` again (it resumes where the step stands), and quote its `message`, which says whether the run had already changed anything. End with the stage's error line.

## Step 0: The job and the step

Resolve `{job_id}` and `{db}` (Arguments). If the customer named `step-1`, go to Prepare. Otherwise list the steps:

```bash
uv run python scripts/run_dms_step.py steps --job-id {job_id} --db {db}
```

An error line that says there are no step plans: run Prepare's first two commands (they make no AWS call), then this command again. Show each entry's `step`, `title` and `wave`, and ask which step to move. Under `--auto`, do not ask: take step 1. A step whose `supported` is false ends with `MOVE_RESULT: stopped stage=select reason=only step 1 is supported`.

**Resume.** When the inputs file exists, run `status` (Prove) once before Prepare and go straight to the stage its `phase` names: `no_stack` → Prepare; `stack_in_review` (a change set was created and never executed) → Move at step 1; `stack_in_progress`, `connections_not_tested`, `connections_testing`, `connections_failed`, `ready_to_start`, `stack_create_failed` or `stack_failed` → Move at the matching point (the last two: step 3's failed stack); `loading`, `validating`, `validation_stalled`, `caught_up` or `task_stopped` → Prove. Tell the customer in one sentence where the step stands. When the customer asked to end the step, go to Ending the step instead, whatever the `phase`.

## Stage 1: Prepare

Run these three commands in order, each on its own; none makes an AWS call:

```bash
uv run python scripts/run_transformation_contract.py --job-id {job_id} --db {db}
uv run python scripts/run_data_movement_plan.py --job-id {job_id} --db {db}
uv run python scripts/render_dms_step.py --job-id {job_id} --db {db}
```

Then print the step's figures:

```bash
uv run python scripts/run_dms_step.py summary --job-id {job_id} --db {db}
```

A line with `"status": "error"` (or a non-zero exit) ends with `MOVE_RESULT: stopped stage=prepare reason=<its message>`. `render_dms_step.py` printing `"status": "skipped"` means the step moves no table: end with `MOVE_RESULT: stopped stage=prepare reason=<its reason>`.

**Checkpoint.** From the `summary` line, and only from it (never compute or round a figure yourself):

- The step: `title`, `source`, `target_engine`, `table_count`.
- The contract: `status`, and `status_reason` when it has one; every table in `blocked_rules` (those tables do not move until the contract is fixed); `warning_count`, with its `warnings`.
- Not moved: every entry of the contract's `not_moved`, each `table` with its `reason`, as a list headed "Not moved". `step-1.ddl.sql` creates these tables, so the application works after cutover, but the DMS task does not move their rows: the customer rebuilds them after cutover, as the reason says. Say "Not moved: none." when the list is empty.
- The plan: the `replication_instance` (`instance_class`, `allocated_storage_gb`, `multi_az_default`, `reason`) and `monthly_cost_usd`, with its `cost_assumptions` and `notes`.
- Not moved: every entry of `not_moved`, each `kind` with its `count` (when it has one) and its `note`: what the source has that step 1 does not move (procedures, functions, triggers, views, events, sequence values). The customer recreates those objects on Aurora; on Aurora PostgreSQL, `step-1.sequences.sql` sets the sequence values at cutover. <!-- claim: tests/unit/scripts/test_run_dms_step.py::test_summary_lists_the_sequences_file_and_the_not_moved_counts_on_postgresql, tests/unit/scripts/test_run_dms_step.py::test_summary_lists_what_a_mysql_step_1_does_not_move -->
- The files in `files`: `aurora-target.yaml` and `step-1.ddl.sql` are the customer's to deploy and run, and so is `step-1.sequences.sql` (Aurora PostgreSQL only), which they run at cutover; `dms-step-1.yaml` is the stack this command deploys, with the customer's credentials.
- The disclaimer: quote the line's `disclaimer` field verbatim, as a block quote. Never shorten, reword or summarise it.

Then list what the customer does before Check, from the README's "Before you deploy the DMS stack" section (point them at it for the details):

1. Create the Aurora cluster from the suggested `aurora-target.yaml`, or give an existing cluster every setting it lists; every instance uses its DB parameter group (reboot after a parameter group changed).
2. Run `step-1.ddl.sql` on the cluster (on Aurora PostgreSQL it creates the extensions its column types need), and create the DMS user it describes, with its grants.
3. Turn on change capture on the source (MySQL: `binlog_format=ROW`, `binlog_row_image=FULL`, binary log retention; PostgreSQL: logical replication), and give the source user its replication grants (PostgreSQL 15 and later: also `CREATE` on the heartbeat schema, `public` by default).
4. Create two Secrets Manager secrets, one for the source user and one for the DMS user on the Aurora writer endpoint, each with the keys `username`, `password`, `host` and `port` (not the RDS-managed master secret).
5. Import the certificate authority bundles into DMS (`aws dms import-certificate`) and keep the two certificate ARNs: required on MySQL-family endpoints.
6. Make sure the account has the DMS roles `dms-vpc-role` and `dms-cloudwatch-logs-role`, and pick private subnets (two Availability Zones) that reach Secrets Manager and a security group with egress to both databases and to Secrets Manager.

Ask whether they are ready for Check (they may come back later: `/move step-1` resumes). Under `--auto`, do not ask: without `--inputs`, end with `MOVE_RESULT: stopped stage=check reason=needs customer inputs`; with `--inputs`, go on to Check.

## Stage 2: Check

**Inputs.** With `--inputs`, the file is given: go to Preflight. Without it, ask for the inputs one block at a time, in this order, and wait for each answer before the next block (under `--auto` you never get here without `--inputs`):

1. `region`, `aurora_cluster_id`, and `target_database_name` when it is not the source database's name.
2. `source_kind` (`rds_instance`, `aurora_cluster` or `self_managed`) and, unless `self_managed`, `source_id` (the RDS instance or Aurora cluster identifier).
3. `source_secret_arn` and `target_secret_arn` (the ARNs, never the values), and `source_secret_kms_key_arn` / `target_secret_kms_key_arn` only when a secret uses a customer managed key.
4. `source_certificate_arn` and `target_certificate_arn`.
5. `subnet_ids` and `security_group_ids` (lists), and `secrets_manager_endpoint_override` only when the Secrets Manager interface endpoint has no private DNS.
6. `confirmed_checks`: only the preflight check ids the customer says they checked themselves; never fill it in yourself. Leave it out at first: Preflight adds an id once the customer confirms it.
7. Optional: `multi_az` (`false` for a demo; the plan's cost prices `true`), `instance_class`, `allocated_storage`, `stack_name`, `read_profile`, `write_profile`, and `template_bucket` only for a large source (a stack template over 51,200 bytes): an S3 bucket the customer owns in the stack's region, where `deploy` uploads the template.

Write them with the Write tool to `artifacts/{db}/{job_id}/data-movement/step-1/inputs.json`, one JSON object with exactly these keys, plus any optional key of block 7 the customer gave (`multi_az`, `instance_class`, `allocated_storage`, `stack_name`, `read_profile`, `write_profile`, `template_bucket`); leave out what the customer did not give:

```json
{"region": "<region>", "aurora_cluster_id": "<cluster>", "source_kind": "rds_instance", "source_id": "<instance>",
 "source_secret_arn": "<source secret ARN>", "target_secret_arn": "<Aurora secret ARN>",
 "source_certificate_arn": "<source certificate ARN>", "target_certificate_arn": "<Aurora certificate ARN>",
 "subnet_ids": ["<subnet-a>", "<subnet-b>"], "security_group_ids": ["<sg>"], "multi_az": false, "confirmed_checks": ["<check id the customer checked>"]}
```

The scripts refuse any other key. When a command says a value is malformed, ask for that one value again and rewrite the file.

**Preflight.** Run the read-only preflight (it runs `scripts/check_dms_step_preflight.py` with the file's values and prints that script's line):

```bash
uv run python scripts/run_dms_step.py preflight --job-id {job_id} --db {db}
```

- `"status": "pass"`: show the `notes` (the checklist items to read) and go to Move.
- `"status": "fail"`: name the `first_failure` check and explain its `message`, which says what to fix. The customer fixes it (or gives a corrected input: rewrite the file), then run the preflight again. It stops at the first failure, so a rerun can find the next one.
- `"status": "needs_confirmation"`: for each id in `to_confirm`, show what to check (the `message`, and the README's list for that check) and ask the customer to confirm they checked it themselves. Rerun with one `--confirm <check_id>` per confirmed id, for example `--confirm source_cdc`, and add each confirmed id to `confirmed_checks` in the inputs file so a later run keeps it. Never confirm a row for them.
- `"status": "error"`: explain its `message`; fix the inputs file if it names one, then run again.

Under `--auto`, do not ask: run the preflight once, with no `--confirm` flag: it uses the file's `confirmed_checks`, and never adds to them. Anything but `pass` ends with `MOVE_RESULT: stopped stage=check reason=preflight <status>: <first_failure or to_confirm>`. Interactively, the customer may stop here: end with `MOVE_RESULT: stopped stage=check reason=<what is left to fix>`.

## Stage 3: Move

Nothing in this stage runs without the customer's explicit approval: a "yes" to the question asked, never an earlier or a general one. Under `--auto`, `--approve-deploy` stands for every approval of this stage; without it, show the plan below and end with `MOVE_RESULT: stopped stage=move reason=deploy needs --approve-deploy`. With `--until ready`, nothing in this stage runs, with or without `--approve-deploy`: end with `MOVE_RESULT: stopped stage=check reason=until ready` before step 1.

**1. Show the deploy.** No AWS call:

```bash
uv run python scripts/run_dms_step.py plan --job-id {job_id} --db {db}
```

Show the `stack_name`, the `region`, every entry of `parameters`, and the `command` line as the equivalent AWS CLI command. Say what the stack creates (the README's `dms-step-1.yaml` row: a replication subnet group, a provisioned replication instance, two endpoints that read the secrets, a role that may read those two secrets, one full-load-and-CDC task) and that it never touches the cluster. Ask: "Create the change set for this stack?" Only on a yes:

```bash
uv run python scripts/run_dms_step.py deploy --job-id {job_id} --db {db} --approve
```

An error line with `"kind": "stack_not_updatable"` means CloudFormation cannot update the stack as it is (its create failed, or it is stuck): no change set was sent; handle it as step 3's failed stack. An error that names `template_bucket` means the template is over CloudFormation's inline limit: ask for a bucket the customer owns in the stack's region, add `template_bucket` to the inputs file, and run `deploy` again (the same approval covers the upload of the template, and nothing else, to that bucket).

**2. Show the change set.** `"status": "change_set_ready"`: show every entry of `changes` (`action`, `logical_id`, `type`, `replacement`). Ask: "Execute this change set?" Only on a yes, with the line's `change_set_name`:

```bash
uv run python scripts/run_dms_step.py execute --job-id {job_id} --db {db} --change-set {change_set_name} --approve
```

`"status": "no_changes"`: the stack is already up to date; go on to step 3.

**3. Wait for the stack.** The replication instance takes several minutes. Run, on its own, with the `change_set_name` of step 2 (CloudFormation starts an executed change set's create a moment later, so the stack may still read `REVIEW_IN_PROGRESS`; with the name, that is waited on):

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until stack --change-set {change_set_name} --wait-seconds 540
```

When Resume brought you here (no change set in this run), leave out `--change-set {change_set_name}`. Its `reached` is false while the stack is still changing: tell the customer the `stack_status` in one line and run it again, in the same turn, up to 4 times. A `phase` of `stack_in_review` means the change set was never executed (declined, or not executed before the run ended): the stack holds no resource. Go back to step 1 once in this run: show the plan and ask again, since a new change set needs its own yes (under `--auto`, `--approve-deploy` covers it: this is the normal deploy path). A second `stack_in_review` in the same run ends with `MOVE_RESULT: stopped stage=move reason=stack REVIEW_IN_PROGRESS: change set not executed`.

**Failed stack.** A `phase` of `stack_create_failed` (the create failed: `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` or `CREATE_FAILED`) or `stack_failed` (a stack CloudFormation cannot update as it is, such as `UPDATE_ROLLBACK_FAILED`): show the line's `stack_failure`, the first resource that failed (its `logical_id`, `status` and `reason`, and its `hint` verbatim when it has one; a `hint` of null means the scripts do not know the fix: never guess one), or `stack_reason` when `stack_failure` is null; then quote its `stack_action` verbatim: what the customer can do. A failed create cannot be updated: once the customer has fixed the cause, they ask `/move step-1` to end the step (Ending the step deletes the failed stack after their yes), then run `/move step-1` again, which creates it anew. This run never deletes, rolls back or redeploys a failed stack, and neither does `--auto` (`--approve-deploy` covers only the normal deploy path, never a redeploy after a failed create). End with `MOVE_RESULT: stopped stage=move reason=stack <stack_status>: <stack_failure reason, or stack_reason>`. A `stack_status` of `UPDATE_ROLLBACK_COMPLETE` is no failed stack: the last update rolled back and the stack runs with its earlier settings, so its task is judged as usual; show its `stack_failure` and `stack_action` once, then go on at the stage its `phase` names, as in Resume.

**4. Test both endpoints.** Testing creates connection records in DMS, so it runs only after the deploy approval of step 1 (or `--approve-deploy`):

```bash
uv run python scripts/run_dms_step.py test --job-id {job_id} --db {db} --approve
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until connections --wait-seconds 300 --interval-seconds 15
```

`phase` `ready_to_start`: both connections are `successful`. `connections_failed`: show each connection's `message` and the README's TLS, secrets and network items; after the customer's fix, test again. A connection that passes says nothing about the replication grants: the task fails at change capture without them (the README says so).

**5. Start the task.** Remind the customer that the task creates no table, so `step-1.ddl.sql` must have run, and that the full load starts at once. Ask: "Start the replication task now?" Only on a yes:

```bash
uv run python scripts/run_dms_step.py start --job-id {job_id} --db {db} --approve
```

**6. With `--until moving` only: wait for the task to run.** Read-only; without `--until moving`, go straight to Prove:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until running --wait-seconds 300 --interval-seconds 15
```

`reached` true with `task_status` `running`: tell the customer the full load has started and that `/move step-1` resumes at Prove, then end with `MOVE_RESULT: stopped stage=move reason=until moving`. `reached` false (still `starting`): run it again, in the same turn, up to 4 times, then end with `MOVE_RESULT: stopped stage=move reason=task still starting ({dms_reports}): run /move step-1 again to resume`. A `phase` of `task_stopped` ends as in Prove.

Any error line in this stage ends with `MOVE_RESULT: stopped stage=move reason=<its message>` unless the customer fixes the cause and you rerun the same command.

## Stage 4: Prove

**Poll.** Run, on its own:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until caught_up --wait-seconds 540 --interval-seconds 60
```

It prints one `"status": "progress"` line per poll, then its result line, always last (the one with `reached`). Relay each progress line in one short sentence, from its fields only: `time`, `full_load_percent`, `tables_loaded`, `inserts_applied` and `updates_applied`, `capture_lag_seconds` and `apply_lag_seconds` (null until CloudWatch has a datapoint), and `validated_tables`, `pending_tables` and `not_enabled_tables`, and `source_retention` (the source's change-log level: `ok`, `warning`, `critical` or `not_measured`) with `source_warning`: when the level is `warning`, add `source_warning` verbatim so the customer sees the risk to their source before it is dangerous; the first time it is `not_measured`, show the line's `source_retention` `reason`, and its `check_on_source` when it has one (a read-only command the customer runs on their own source, never you). Read every result line for `stopped_for` first: a result line with `stopped_for` `source_retention` (whatever its `reached`) means the source's change log or free storage crossed a critical threshold (the README's "Watching the source while it moves"): show its `source_retention` `message` first and say the scripts never touch the source. Then, when its `phase` is `caught_up`, go on to the Gate; otherwise never keep polling: with a `phase` of `task_stopped`, also quote its `source_retention` `stopped_task` verbatim, and end with `MOVE_RESULT: stopped stage=prove reason=source retention critical: <source_retention message>`. That is the run's one ending: a `task_stopped` line with `stopped_for` ends with this line only, never also with the `task_stopped` ending below. Unless the line has `stopped_for`: after a result line with `reached` false, give the same one sentence from its fields (`validation` holds its table counts) and run it again in the same turn. Never end the turn while it is still loading: a turn that ends waits on nothing (see Waiting Rule). After 12 runs (about 2 hours) still not caught up, tell the customer where it stands and end with `MOVE_RESULT: stopped stage=prove reason=still loading: run /move step-1 again to resume`. A `phase` of `task_stopped`: quote its `source_retention` `stopped_task` verbatim (what the stopped task costs the source), then go to Stopped or failed task below, which ends with `MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>` unless the task runs again. A `phase` of `validation_stalled` means the full load is done but DMS validation is not running (tables still `Not enabled` although the task turns validation on, or pending with no progress over several polls); never keep polling it: show its `validation` counts and go to Validation stalled below. A task that turns validation off (`validation_enabled` false) is never reported this way.

**Validation stalled.** Restarting the replication task usually restarts the DMS validator. Offer it at most once per run (it counts as the run's one recovery: see Stopped or failed task), and never in a run that already resumed or retried the task. Tell the customer, in plain words, what a restart does: it stops the task and starts it again, so replication pauses for a minute or two; change data capture resumes from its checkpoint, so nothing reloads, nothing is deleted and no change on the source is lost. Then ask: "Restart the replication task of stack {stack_name} to restart DMS validation now?" Only on a yes, with the status line's `stack_name`; on a no, end with `MOVE_RESULT: stopped stage=prove reason=validation not running: <validation_check>`, with the line's `validation_check` (what to check). Under `--auto`, never run `restart-validation` and never ask: `--approve-deploy` does not cover it, so end with that same line. On a yes, run, on its own:

```bash
uv run python scripts/run_dms_step.py restart-validation --job-id {job_id} --db {db} --approve
```

It stops the task, waits until it has stopped, resumes it and waits until it runs, up to 540 seconds in all; it refuses any stack that is not this job's step 1 stack. `"status": "restarted"`: tell the customer `{dms_reports}` and run the Poll again. `"status": "starting"` (the wait ran out after the resume): wait with the read-only `status --until running` of Move's step 6, in the same turn, up to 4 times, then run the Poll again. `"status": "stopping"` (the task had not stopped by the deadline, so it was not resumed): run the same `restart-validation` once more, under the same yes; it waits for the stop and then resumes. `"status": "resume_failed"`: end with `MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>`. An error line ends with `MOVE_RESULT: stopped stage=prove reason=<its message>`. When the Poll after the restart stalls again, do not offer another restart: show its `validation` counts and end with `MOVE_RESULT: stopped stage=prove reason=validation not running: <validation_check>`, as before the restart.

**Stopped or failed task.** First show its `hint` verbatim when it has one: the fix for a failure DMS describes in a known way. A `hint` of null means the failure is not one the scripts know: show `last_failure` and `stop_reason`, and never guess a fix. Then offer a recovery at most once per run, counting a restart from Validation stalled (a run that restarted the task offers neither of these), by the line's `recovery`, which names a command only when that command would run (the scripts decide it with the same rule the commands use):

- `recovery` `resume` (DMS reports the task stopped after its full load finished): when the line has a `hint`, or its `stop_reason` is not `Stop Reason NORMAL`, say what stopped the task and ask whether the customer has fixed it first. Tell the customer, in plain words, what a resume does: it starts the stopped task again, and change data capture resumes from its checkpoint, so nothing reloads and nothing is deleted; the changes the source made since the stop are applied as long as the source still keeps its change log (`stopped_task` says what the stop costs the source). The script checks again before it writes. Ask: "Resume the replication task of stack {stack_name} now?" Only on a yes, with the status line's `stack_name`, run, on its own:

```bash
uv run python scripts/run_dms_step.py resume --job-id {job_id} --db {db} --approve
```

- `recovery` `retry` (a failed task, or a stopped one whose full load did not finish): ask whether the customer has fixed what stopped the task (the `hint`, or `last_failure` and `stop_reason`): a retry before the fix fails the same way. Tell them, in plain words, what a retry does: it reads DMS's statistics, and only when they show the full load never started and no table counter (`FullLoadRows`, inserts, updates, deletes, DDLs) above 0 does it reload the task from the start, writing nothing over earlier rows and deleting nothing. In any other case it refuses and changes nothing, and gives the customer's own steps instead, because the Aurora tables may already hold rows. Ask: "Retry the replication task of stack {stack_name} now?" Only on a yes, run, on its own:

```bash
uv run python scripts/run_dms_step.py retry --job-id {job_id} --db {db} --approve
```

- A `recovery` of null with `customer_steps` (a stopped or failed task neither command may reload, because Aurora may already hold rows; `recovery_refused` says what showed it): offer nothing; show the steps as for `target_may_hold_rows` below and end with its line.
- A `recovery` of null without `customer_steps` (for example `failed-move`): nothing to offer.

Both wait until the task runs, up to 540 seconds, and refuse any stack that is not this job's step 1 stack. `"status": "resumed"` or `"retried"`: tell the customer `{dms_reports}` and run the Poll again. `"status": "starting"`: wait with the read-only `status --until running` of Move's step 6, in the same turn, up to 4 times, then run the Poll again. `"status": "resume_failed"` or `"retry_failed"`: show its `hint` (or `last_failure`) and end with `MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>`. An error line with `"kind": "target_may_hold_rows"` (from either command): quote its `message`, list `tables_to_empty`, then show every entry of `customer_steps` verbatim, each on its own (an SQL entry is SQL only, so the customer can copy it as is). Emptying those tables on Aurora and reloading the task are the customer's to do: never run them yourself (the modernizer never connects to the customer's databases or runs SQL on them, and never runs `aws`). End with `MOVE_RESULT: stopped stage=prove reason=task may have written rows to Aurora: empty the step's tables on Aurora, then reload (customer_steps)`.

On a no, with nothing to offer, or when the Poll after a resume or a retry ends at `task_stopped` again (never offer a second one: show its `hint`), end with `MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>`. Under `--auto`, never run `resume` or `retry` and never ask: `--approve-deploy` does not cover them, so end with that same line.

**Routines.** When `phase` is `caught_up`, and before the gate, write step 1's routine scripts (read-only in AWS; the modernizer never connects to a database):

```bash
uv run python scripts/run_dms_step.py routines --job-id {job_id} --db {db}
```

`"status": "nothing_to_deploy"`: say so in one line (list `not_carried_routines`, each with its `reasons`, if any) and go to the Gate. `"status": "ready"`: show `carried`, `in_script`, `deferred` (Aurora MySQL triggers and events, left to the cutover script), every entry of `not_carried_routines` and `refused_routines` with its reasons, and the `label` verbatim; point the customer at `routines.md` in `files`. The customer runs `routines-deploy.sql`, then `routines-check.sql`, saving its output as `routines-check.tsv` next to them, as that file says. Ask them to say when the check output is there; under `--auto`, never wait: go to the Gate (its routines check stays `pending`). Then run (no AWS call):

```bash
uv run python scripts/run_dms_step.py routines-check --job-id {job_id} --db {db}
```

Show its `status` (`pass` or `fail`), `confirmed` of `carried`, every entry of `mismatches` (each `routine_id` with its `problem`) and of `not_carried_routines`, and the `label`. `"status": "no_output"`: the file is not at `expected` yet; ask again. An error line (a refused file) names what is wrong: tell the customer to run `routines-check.sql` again. Then go to the Gate.

**Gate.** When `phase` is `caught_up` (the full load is at 100% and validation caught up), run the read-only gate (it runs `scripts/check_dms_step_gate.py` on the stack and prints that script's line):

```bash
uv run python scripts/run_dms_step.py gate --job-id {job_id} --db {db}
```

Show the evidence from that line only: its `status` (`pass`, `pending` or `fail`), every entry of `reasons` (prefixed `load:`, `lag:`, `parity:` or `routines:`), every named entry of `exceptions` (each `table` with its `kind`: what the parity check left out), and `gate` (the path of `gate.json`, with the full load, lag and parity evidence). `pending` usually means lag datapoints or validation are still arriving: run the gate again in a few minutes. Then quote the line's `cutover_message` field verbatim, as a block quote: never shorten, reword or summarise it, and never recommend a time to cut over. When to cut over is the customer's decision (`cutover_decision` is `customer`); this command never cuts over.

**Verify (optional).** Then offer, in one line, an independent read-only comparison of the source and Aurora (per moved table, a row count and a checksum); say it is optional and does not change the gate. Under `--auto`, never offer it. On a yes, run (no AWS call; the modernizer never connects to a database):

```bash
uv run python scripts/run_dms_step.py verify-scripts --job-id {job_id} --db {db}
```

Point the customer at `verify.md` in its `files` (they run both scripts with read-only sessions, twice each, as it says); once they say the outputs are there, run:

```bash
uv run python scripts/run_dms_step.py verify --job-id {job_id} --db {db}
```

Show its `status`, the `match`, `changing`, `differ`, `not_compared` and `missing` counts, every entry of `not_matched` and its `gate` verbatim, never a value; `"status": "no_output"` means the files are not at `expected` yet, and an error line (a refused file) names the file to produce again.

Then tell the customer in one line how the step ends: when you have cut over, or to abandon the move, ask `/move step-1` to end the step; it stops the task and then deletes the DMS stack (CloudFormation cannot delete a running task). End with `MOVE_RESULT: complete job_id={job_id} step=1 gate=<the gate line's status>`.

## Ending the step

Ending the step is a run of its own: it starts only when the customer asks to end the step, in a later message or with `/move step-1` again (Resume sends that request here), never inside a run that is still proving. That run ends with exactly one result line, a `stage=end` one, and never prints `MOVE_RESULT: complete`. Say what it deletes and what stays, from the README's "Ending the step" section: the stack's six resources go; the Aurora cluster and its data stay (they are the customer's), and so do the source, the secrets and the certificates. Stopping the task ends ongoing replication, so changes on the source no longer reach Aurora. Both commands refuse any stack that is not this job's step 1 stack. Under `--auto`, never run `stop` or `teardown`: ending a step is the customer's own request, and `--approve-deploy` does not cover it.

**What DMS reports.** Every line these commands print about the task carries `dms_reports`: DMS's own task status in words, for example `DMS reports the task stopped (not running)`, `DMS reports the task failed (not running): <DMS's reason>`, `DMS reports the task never started (ready, not running)` or `DMS reports the task stopping (still running)`. Quote it verbatim in every question and result line below (`{dms_reports}`): never shorten it to a generic "not running". When `task_status` is `failed`, also show the line's `last_failure` and `stop_reason`. A failed task cannot be stopped, so a failed or never-started task counts as not running and the step can still be ended. A stack whose create failed (`stack_create_failed`), or that never got its resources (`stack_in_review`), has no task: the `status` read below is reached at once, and the delete question follows; `teardown` ends such a stack only when its own `modernizer:job-id` tag names this job and it holds nothing but resources of `dms-step-1.yaml`.

First show which stack this is (no AWS call), then read what DMS reports for its task (read-only, no wait):

```bash
uv run python scripts/run_dms_step.py plan --job-id {job_id} --db {db}
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until task_stopped --wait-seconds 0
```

`reached` true: tell the customer `{dms_reports}` and go straight to the delete question; there is nothing to stop. Otherwise ask: "{dms_reports}. Stop the replication task of stack {stack_name} in {region} now?" Only on a yes, with the `plan` line's `stack_name` and `region`; on a no, end with `MOVE_RESULT: stopped stage=end reason=customer declined to stop the task ({dms_reports})`:

```bash
uv run python scripts/run_dms_step.py stop --job-id {job_id} --db {db} --approve
```

`"status": "stopped"`, `"no_task"` or `"no_stack"`: tell the customer the line's `dms_reports`. `"status": "stopping"` (the wait ran out): never rerun `stop`; wait with the read-only status instead, in the same turn, up to 4 times:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until task_stopped --wait-seconds 540
```

`reached` true: DMS reports the task stopped, failed or never started, or the task is gone (`task_status` `deleted`); the wait reads the task whatever the stack's state. Tell the customer the line's `dms_reports`. A `task_status` of `running` or `starting` (the task started again): stopping it again needs a new yes, so ask the stop question again. Still `stopping` after the 4 waits: end with `MOVE_RESULT: stopped stage=end reason=task still stopping ({dms_reports}): run /move step-1 again to end the step`. Ask the delete question only once that is proven: a `stop` line with `"status"` `"stopped"`, `"no_task"` or `"no_stack"`, or a `status --until task_stopped` line with `reached` true. Never on `stopping`, and never from a stack status alone.

Ask: "{dms_reports}. Delete the stack {stack_name} in {region} now?" Only on a yes; on a no, end with `MOVE_RESULT: stopped stage=end reason=customer declined to delete the stack ({dms_reports})`:

```bash
uv run python scripts/run_dms_step.py teardown --job-id {job_id} --db {db} --approve
```

Each `teardown` run deletes at most once, so it runs once per yes. `"status": "deleted"`: show every entry of the line's `source_followups` (what is left on the source, such as the replication slot DMS made on PostgreSQL; the customer runs them, never you), then end with `MOVE_RESULT: stopped stage=end reason=step 1 ended: stack <stack_name> deleted ({dms_reports}); on the source: <source_reminder>`, with the `teardown` line's `dms_reports` (what DMS reported when the delete was decided) and `source_reminder`. `"status": "delete_failed"`: handle it as the `DELETE_FAILED` below, with its `stack_reason`. `"status": "deleting"` (the wait ran out): never rerun `teardown`; wait with the read-only status instead, in the same turn, up to 4 times:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until stack --wait-seconds 540
```

`phase` `no_stack`: the stack is gone; end as for `deleted`. Still `DELETE_IN_PROGRESS` after the 4 waits: end with `MOVE_RESULT: stopped stage=end reason=stack still deleting ({dms_reports}): run /move step-1 again to end the step`. `stack_status` `DELETE_FAILED`: show its `stack_reason`, then ask again: "{dms_reports}. Delete the stack {stack_name} in {region} again?" Only on a yes, run `teardown` once more; on a no, end with `MOVE_RESULT: stopped stage=end reason=customer declined to delete the stack again: <stack_reason> ({dms_reports})`. Any error line (for example a stack that is not this job's step 1 stack, `DMS reports the task running ...: run stop first`, or a stack with termination protection on) ends with `MOVE_RESULT: stopped stage=end reason=<its message>`. Termination protection is the customer's: never turn it off, and never offer to; the error line says how they can. In the result lines `{dms_reports}` is the latest one a command printed.

## Headless (`--auto`)

`--auto` never asks anything:

- Step 0 takes step 1; Prepare runs in full.
- Without `--inputs`: no AWS call at all. End with `MOVE_RESULT: stopped stage=check reason=needs customer inputs`.
- With `--inputs` and without `--approve-deploy`: the preflight (read-only) and `plan` (no AWS call) run, then the run ends before any AWS write with `MOVE_RESULT: stopped stage=move reason=deploy needs --approve-deploy`.
- With `--inputs` and `--approve-deploy`: Move runs every step without its question, then Prove.
- `--auto` never stops the task or deletes the stack (Ending the step), never restarts the task when validation stalls, and never resumes or retries a stopped or failed task (Prove).
- `--auto` writes the routine scripts (Prove) but never waits for the customer to run them: the gate's routines check stays `pending`.
- The writes refuse to run under `MODERNIZER_CI_SANDBOX=1` (the CI headless runs), whatever the flags.

## Result line

End every run with exactly one line, also when not `--auto`:

- `MOVE_RESULT: complete job_id=<id> step=1 gate=<status>` — the gate ran; `<status>` is its `status`.
- `MOVE_RESULT: stopped stage=<stage> reason=<one line>` — `<stage>` is `select`, `prepare`, `check`, `move`, `prove` or `end` (Ending the step).

A run that ends the step (Ending the step) is its own run and prints only its `stage=end` line, never a `complete` one.

A question the customer answers no ends the run with `MOVE_RESULT: stopped stage=<the stage that asked> reason=customer declined <what was asked>`: in Move, for example, `stage=move reason=customer declined the change set`; Ending the step names its own three lines.

## Waiting Rule

Never end a turn waiting unless the customer has a question to answer. The polling commands wait inside the script (`--wait-seconds`), so a long wait is several runs of the same command in one turn, never a turn that ends with nothing running. Do not dispatch subagents: this command runs in one session.
