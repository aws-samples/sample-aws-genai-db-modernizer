# Data movement stacks: DMS into Aurora and Debezium on Amazon MSK Connect

Data movement has two kinds of stack. Step 1 moves the whole source database into your Aurora cluster with AWS DMS (`scripts/render_dms_step.py`, from `src/tools/data_movement/dms_step.py`). Later steps read each source table's changes into one compacted Kafka topic with a Debezium MySQL or PostgreSQL source connector on Amazon MSK Connect. This folder holds the stacks for both. Nothing in the project deploys them, and rendering a template makes no AWS call.

| File | What it is |
|---|---|
| `debezium-plugin-bucket.yaml` | Static stack: a versioned, encrypted, private S3 bucket for the plugin ZIP, plus its access-log bucket |
| `examples/*.input.json` | Connector inputs shaped like a wordpress wave (the `source`, `cdc` and `hub` sections of a data movement plan). A hub plan's `source`, `cdc` and `topics` (as `hub`) feed the connector from #1802 on |
| `examples/*.yaml` | The connector stacks rendered from them (the snapshot tests compare against these files) |
| `examples/dms-step-1/` | Step 1's golden examples, one folder per engine (`wordpress-mysql`, `postgresql-shop`): `input.json` is a step 1 plan, its transformation contract and the Aurora DDL in one file (from a recorded wordpress run, or a small fixture); the three files rendered from it (`aurora-target.yaml`, `step-1.ddl.sql`, `dms-step-1.yaml`) are regenerated with `scripts/render_dms_step.py --examples` and checked with `--examples --check` |

## Step 1: DMS into your Aurora cluster

Step 1 moves the whole source database into the Aurora engine the assessment chose: AWS DMS runs a full load, then ongoing replication. `scripts/render_dms_step.py --job-id <id> --db <db>` writes three files under `artifacts/<db>/<job>/data-movement/step-1/` from the step's plan, its transformation contract and the Aurora design. Rendering makes no AWS call.

| File | Who runs it | What it is |
|---|---|---|
| `aurora-target.yaml` | You | A **suggested** Aurora cluster template. Creating and running the cluster is your responsibility; its header and `Metadata` list every setting the cluster must have, with the reason. Cluster-level settings sit in a cluster parameter group; instance-level ones (`sql_mode`, `local_infile` on Aurora MySQL) sit in a DB parameter group, and every instance of the cluster must use it, because a failover makes another instance the writer. When the source can hold zero dates, `sql_mode` drops `NO_ZERO_DATE` and `NO_ZERO_IN_DATE`: Aurora MySQL 8.0's default strict mode rejects zero dates, and DMS records them as NULL on a target that does not accept them |
| `step-1.ddl.sql` | You | Every wave 1 table the transformation contract moves (the full step 1 target set), with instructions. On Aurora MySQL it starts with `CREATE DATABASE IF NOT EXISTS` and `USE`. The DMS user and its grants follow the tables, commented out for you to adapt and run. It lists the tables left out and the fields that are unsupported. On Aurora PostgreSQL it recommends `REPLICA IDENTITY FULL` per table, commented out: your decision, at the cost of more WAL |
| `dms-step-1.yaml` | Us, with your credentials | A replication subnet group, a provisioned replication instance (`MultiAZ` defaults to `true`), source and target endpoints that read Secrets Manager secrets, a role that lets DMS read those two secrets, and one full-load-and-CDC task |

Nothing we deploy changes your cluster's configuration or runs inside it. `dms-step-1.yaml` only makes DMS connect as the user you create and write rows. The task creates no table (`TargetTablePrepMode: DO_NOTHING`): run the DDL first. Its table mappings select exactly the contract's tables by name, with no wildcard and no transformation rules, so do not change the schema of the source or of Aurora until the step is cut over. Transactional apply (batch apply off) keeps each key's changes in order for the later steps; data validation is on.

The instance class and storage come from `src/catalog/dms_sizing.toml` (the collector's database size and write rate); the parameter descriptions in `dms-step-1.yaml` and the plan's `replication.sizing.reason` say why. Override them with the `InstanceClass` and `AllocatedStorage` parameters. The plan's step 1 cost prices the sized class Multi-AZ, as the stack deploys it by default, and states the Single-AZ figure next to it; it also prices the storage beyond the instance's included GB. The Aurora cluster's own cost is in the assessment's total cost of ownership, not here.

### Before you deploy the DMS stack

- The cluster has every setting `aurora-target.yaml` lists, and every instance uses its DB parameter group. Reboot the instances after a parameter group changed.
- The source can be read for change capture. MySQL: `binlog_format=ROW`, `binlog_row_image=FULL` and binary log retention long enough to cover an outage (on RDS or Aurora MySQL, `CALL mysql.rds_set_configuration('binlog retention hours', 24);`). PostgreSQL: logical replication enabled (`rds.logical_replication=1` on RDS and Aurora). The stack does not check these.
- Two Secrets Manager secrets, one for the source and one for the Aurora writer endpoint, each with the keys `username`, `password`, `host` and `port`, for a user DMS may replicate with (the source user) and write with (the DMS user of the DDL file). Do not use the RDS-managed master secret: it has no `host` or `port`.
- The source user's grants: on MySQL, `SELECT` on the source tables plus `REPLICATION CLIENT` and `REPLICATION SLAVE` (on Amazon RDS for MySQL, automated backups on, so the binary log exists); on PostgreSQL, `SELECT` on the source tables and the `rds_replication` role (or superuser on a self-managed server). `aws dms test-connection` passes without the replication grants, and the task then fails when change capture starts.
- On a PostgreSQL source, the task turns on the DMS heartbeat (`HeartbeatEnable`), which creates a heartbeat table in the source (schema `public` by default) so an idle replication slot keeps advancing. If a secret uses a customer managed KMS key, pass its ARN as `SourceSecretKmsKeyArn` or `TargetSecretKmsKeyArn`.
- The account has the DMS service roles `dms-vpc-role` and `dms-cloudwatch-logs-role` (the DMS console creates them; with the CLI, create them with the managed policies `AmazonDMSVPCManagementRole` and `AmazonDMSCloudWatchLogsRole`).
- TLS. MySQL-family endpoints connect with `verify-ca`, so both certificates are required: the source server must serve TLS with a certificate authority you can import, and for Aurora use the RDS CA bundle. Import each bundle with `aws dms import-certificate --certificate-identifier <name> --certificate-pem file://<bundle>.pem` and pass the ARNs as `SourceCertificateArn` and `TargetCertificateArn`. PostgreSQL endpoints default to `require`.
- The private subnets reach Secrets Manager, because DMS reads the endpoint secrets from them: a Secrets Manager interface VPC endpoint with private DNS, or a NAT route. The security group needs egress on 443 to it and on the database ports to the source and the cluster, and the databases' security groups must allow the replication instance. If the interface endpoint has no private DNS, pass its DNS name as `SecretsManagerEndpointOverride`.

### Deploy

The role in the stack has no explicit name, so `CAPABILITY_IAM` is enough. Use the parameter names of your rendered template (this example is MySQL; `TargetDatabaseName` defaults to the source database):

```bash
aws cloudformation deploy --template-file dms-step-1.yaml \
  --stack-name dm-<job-id>-step-1 --capabilities CAPABILITY_IAM \
  --parameter-overrides SubnetIds=<subnet-a>,<subnet-b> SecurityGroupIds=<sg> \
    SourceSecretArn=<source secret ARN> TargetSecretArn=<Aurora secret ARN> \
    SourceCertificateArn=<source certificate ARN> TargetCertificateArn=<Aurora certificate ARN>
```

Add `InstanceClass`, `AllocatedStorage`, `MultiAZ=false` (for a demo), `TargetDatabaseName`, the KMS key ARNs or `SecretsManagerEndpointOverride` when you need them. You can deploy the stack before the DDL has run, but the task is not started by the stack: start it only after the DDL ran. First test both endpoints, then read the result:

```bash
aws dms test-connection --replication-instance-arn <ReplicationInstanceArn output> --endpoint-arn <SourceEndpointArn output>
aws dms test-connection --replication-instance-arn <ReplicationInstanceArn output> --endpoint-arn <TargetEndpointArn output>
aws dms describe-connections --filter Name=replication-instance-arn,Values=<ReplicationInstanceArn output>
aws dms start-replication-task --replication-task-arn <ReplicationTaskArn output> \
  --start-replication-task-type start-replication
```

Removing the stack removes everything of ours. It never touches your cluster.

### Gate and cutover

Once the stack is deployed and the task started, run the gate. It only makes these read calls (DMS `DescribeReplicationTasks`, `DescribeTableStatistics`, `DescribeReplicationInstances`, `ListTagsForResource`; CloudWatch `ListMetrics`, `GetMetricData`; CloudFormation `DescribeStacks`; `tests/unit/tools/data_movement/test_dms_gate_read_only.py::test_the_gate_only_reads`), so run it with read-only credentials, for example through `AWS_PROFILE`, and it writes `data-movement/step-1/gate.json`:

```bash
uv run python scripts/check_dms_step_gate.py --job-id <id> --db <db> --region <region> --stack-name <job-id>-dms-step-1
```

- **Load**: the task runs in CDC, the full load is at 100%, every table of the step's contract is "Table completed" without errors, no other table is loaded, and the task was rendered from this step's contract.
- **Lag**: every `CDCLatencySource` and `CDCLatencyTarget` datapoint of the last 15 minutes (`--window-minutes`) is under the plan's `max_lag_seconds`, and the newest datapoint of each metric is at most 5 minutes old (`--stale-seconds`); the age is in the evidence.
- **Parity**: every table with a primary or unique key DMS can validate is `Validated`. Tables without a key, tables whose key DMS cannot validate, and views are named as exceptions ("not validated") with their row counts; pending validation records are totalled in the evidence.

The result is `pass`, `pending` or `fail`, in the JSON status line and in `gate.json`; the status line also lists `reasons` (each prefixed with its check) and the named `exceptions`. The script exits 0 whenever the gate ran, whatever the result (`test_the_status_line_carries_the_decision_and_the_message` for `pending`, `test_a_fail_result_still_exits_zero` for `fail`), and 1 when it could not (for example, no `ReplicationTaskArn` output on the stack: `test_a_stack_without_the_task_output_is_an_error`).

The status line carries `cutover_decision: "customer"` and this message (`gate.json` carries the decision and the message's id):

> When to cut over is your decision. The data movement for this step keeps your Aurora copy in sync with your source and reports the evidence below: replication lag, rows loaded, and validation per table (with the tables DMS could not validate, and why). These figures help you decide; they are not a recommendation to switch. Choosing the moment to cut over, and preparing your applications for it, remains entirely your responsibility.

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
