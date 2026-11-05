
# /move

Walks the customer through step 1 of a finished assessment's migration: the homogeneous MySQL or PostgreSQL source into the Aurora engine the assessment chose, with AWS DMS (a full load, then ongoing replication). Four stages, each ending at a checkpoint: **Prepare**, **Check**, **Move**, **Prove**. The scripts do every deterministic step and every AWS call; you explain their output, collect the customer's inputs and ask for each approval. `infrastructure/cloudformation/data-movement/README.md` ("Step 1: DMS into your Aurora cluster") is the source of truth for what the customer does.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it. Write the inputs file with the Write tool. Never run `aws` yourself: every AWS call goes through `scripts/run_dms_step.py`, and the commands the customer runs on their own (creating the cluster, the secrets, importing a certificate) are theirs to run. Run each command exactly as shown, on its own, and read the one JSON line it prints.

## Arguments

- `step-1` — the step to move (optional). Without it, list the job's steps and ask which one (see Step 0). Only step 1 is supported.
- `--job-id <id>` and `--db <db>` — the job (optional). Without them, take `job_id` and `database_name` from `.modernizer-state.json` (the last `/modernize` run).
- `--inputs <file.json>` — the inputs file (optional, see Check). Without it, the inputs file is `artifacts/{db}/{job_id}/data-movement/step-1/inputs.json`, which Check writes. With it, pass `--inputs {inputs}` to every `scripts/run_dms_step.py` command below that is not `steps` or `summary`.
- `--auto` — headless: never ask anything (see Headless).
- `--approve-deploy` — only with `--auto` and `--inputs`: the customer's approval, given up front, for the three AWS writes of Move (create and execute the change set, test the endpoints, start the task).

## Credentials and secrets

Every AWS call uses the customer's own credentials: the ambient ones, or the named profiles in the inputs file, `read_profile` for the read-only calls (preflight, status, gate) and `write_profile` for the writes (deploy, execute, test, start). Recommend a read-only profile for `read_profile`; the preflight and the gate only read (the README lists their permissions). The inputs file holds ids and ARNs only. Never ask for, write, print or repeat a password or a secret's value; if the customer pastes one, do not write it anywhere, tell them it is not needed (DMS reads it from the secret) and ask for the secret's ARN instead. The scripts never read a secret's value.

## Step 0: The job and the step

Resolve `{job_id}` and `{db}` (Arguments). If the customer named `step-1`, go to Prepare. Otherwise list the steps:

```bash
uv run python scripts/run_dms_step.py steps --job-id {job_id} --db {db}
```

An error line that says there are no step plans: run Prepare's first two commands (they make no AWS call), then this command again. Show each entry's `step`, `title` and `wave`, and ask which step to move. Under `--auto`, do not ask: take step 1. A step whose `supported` is false ends with `MOVE_RESULT: stopped stage=select reason=only step 1 is supported`.

**Resume.** When the inputs file exists, run `status` (Prove) once before Prepare and go straight to the stage its `phase` names: `no_stack` → Prepare; `stack_in_progress`, `connections_not_tested`, `connections_testing`, `connections_failed`, `ready_to_start` or `stack_failed` → Move at the matching point; `loading`, `validating`, `caught_up` or `task_stopped` → Prove. Tell the customer in one sentence where the step stands.

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
- The three files in `files`: `aurora-target.yaml` and `step-1.ddl.sql` are the customer's to deploy and run; `dms-step-1.yaml` is the stack this command deploys, with the customer's credentials.
- The disclaimer: quote the line's `disclaimer` field verbatim, as a block quote. Never shorten, reword or summarise it.

Then list what the customer does before Check, from the README's "Before you deploy the DMS stack" section (point them at it for the details):

1. Create the Aurora cluster from the suggested `aurora-target.yaml`, or give an existing cluster every setting it lists; every instance uses its DB parameter group (reboot after a parameter group changed).
2. Run `step-1.ddl.sql` on the cluster, and create the DMS user it describes, with its grants.
3. Turn on change capture on the source (MySQL: `binlog_format=ROW`, `binlog_row_image=FULL`, binary log retention; PostgreSQL: logical replication), and give the source user its replication grants.
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
7. Optional: `multi_az` (`false` for a demo; the plan's cost prices `true`), `instance_class`, `allocated_storage`, `stack_name`, `read_profile`, `write_profile`.

Write them with the Write tool to `artifacts/{db}/{job_id}/data-movement/step-1/inputs.json`, one JSON object with exactly these keys (leave out what the customer did not give):

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

Nothing in this stage runs without the customer's explicit approval: a "yes" to the question asked, never an earlier or a general one. Under `--auto`, `--approve-deploy` stands for every approval of this stage; without it, show the plan below and end with `MOVE_RESULT: stopped stage=move reason=deploy needs --approve-deploy`.

**1. Show the deploy.** No AWS call:

```bash
uv run python scripts/run_dms_step.py plan --job-id {job_id} --db {db}
```

Show the `stack_name`, the `region`, every entry of `parameters`, and the `command` line as the equivalent AWS CLI command. Say what the stack creates (the README's `dms-step-1.yaml` row: a replication subnet group, a provisioned replication instance, two endpoints that read the secrets, a role that may read those two secrets, one full-load-and-CDC task) and that it never touches the cluster. Ask: "Create the change set for this stack?" Only on a yes:

```bash
uv run python scripts/run_dms_step.py deploy --job-id {job_id} --db {db} --approve
```

**2. Show the change set.** `"status": "change_set_ready"`: show every entry of `changes` (`action`, `logical_id`, `type`, `replacement`). Ask: "Execute this change set?" Only on a yes, with the line's `change_set_name`:

```bash
uv run python scripts/run_dms_step.py execute --job-id {job_id} --db {db} --change-set {change_set_name} --approve
```

`"status": "no_changes"`: the stack is already up to date; go on to step 3.

**3. Wait for the stack.** The replication instance takes several minutes. Run, on its own:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until stack --wait-seconds 540
```

Its `reached` is false while the stack is still changing: tell the customer the `stack_status` in one line and run it again, in the same turn, up to 4 times. A `phase` of `stack_failed` ends with `MOVE_RESULT: stopped stage=move reason=stack <stack_status>: <stack_reason>`.

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

Any error line in this stage ends with `MOVE_RESULT: stopped stage=move reason=<its message>` unless the customer fixes the cause and you rerun the same command.

## Stage 4: Prove

**Poll.** Run, on its own:

```bash
uv run python scripts/run_dms_step.py status --job-id {job_id} --db {db} --until caught_up --wait-seconds 540 --interval-seconds 60
```

After each line with `reached` false, give one progress line from it (`full_load_percent`, `tables_loaded`, the `validation` counts) and run it again in the same turn. Never end the turn while it is still loading: a turn that ends waits on nothing (see Waiting Rule). After 12 runs (about 2 hours) still not caught up, tell the customer where it stands and end with `MOVE_RESULT: stopped stage=prove reason=still loading: run /move step-1 again to resume`. A `phase` of `task_stopped` ends with `MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>`.

**Gate.** When `phase` is `caught_up` (the full load is at 100% and validation caught up), run the read-only gate (it runs `scripts/check_dms_step_gate.py` on the stack and prints that script's line):

```bash
uv run python scripts/run_dms_step.py gate --job-id {job_id} --db {db}
```

Show the evidence from that line only: its `status` (`pass`, `pending` or `fail`), every entry of `reasons` (prefixed `load:`, `lag:` or `parity:`), every named entry of `exceptions` (each `table` with its `kind`: what the parity check left out), and `gate` (the path of `gate.json`, with the full load, lag and parity evidence). `pending` usually means lag datapoints or validation are still arriving: run the gate again in a few minutes. Then quote the line's `cutover_message` field verbatim, as a block quote: never shorten, reword or summarise it, and never recommend a time to cut over. When to cut over is the customer's decision (`cutover_decision` is `customer`); this command never cuts over.

End with `MOVE_RESULT: complete job_id={job_id} step=1 gate=<the gate line's status>`.

## Headless (`--auto`)

`--auto` never asks anything:

- Step 0 takes step 1; Prepare runs in full.
- Without `--inputs`: no AWS call at all. End with `MOVE_RESULT: stopped stage=check reason=needs customer inputs`.
- With `--inputs` and without `--approve-deploy`: the preflight (read-only) and `plan` (no AWS call) run, then the run ends before any AWS write with `MOVE_RESULT: stopped stage=move reason=deploy needs --approve-deploy`.
- With `--inputs` and `--approve-deploy`: Move runs every step without its question, then Prove.
- The writes refuse to run under `MODERNIZER_CI_SANDBOX=1` (the CI headless runs), whatever the flags.

## Result line

End every run with exactly one line, also when not `--auto`:

- `MOVE_RESULT: complete job_id=<id> step=1 gate=<status>` — the gate ran; `<status>` is its `status`.
- `MOVE_RESULT: stopped stage=<stage> reason=<one line>` — `<stage>` is `select`, `prepare`, `check`, `move` or `prove`.

## Waiting Rule

Never end a turn waiting unless the customer has a question to answer. The polling commands wait inside the script (`--wait-seconds`), so a long wait is several runs of the same command in one turn, never a turn that ends with nothing running. Do not dispatch subagents: this command runs in one session.
