# Debezium source connectors on Amazon MSK Connect

The data movement decided on #1412 (to become ADR-033) reads each source table's changes into one compacted Kafka topic. A Debezium MySQL or PostgreSQL source connector, running on Amazon MSK Connect, writes them. This folder holds the stacks for it. Nothing in the project deploys them, and rendering a template makes no AWS call.

| File | What it is |
|---|---|
| `debezium-plugin-bucket.yaml` | Static stack: a versioned, encrypted, private S3 bucket for the plugin ZIP, plus its access-log bucket |
| `examples/*.input.json` | Connector inputs shaped like a wordpress wave (the `source`, `cdc` and `hub` sections of a data movement plan) |
| `examples/*.yaml` | The connector stacks rendered from them (the snapshot tests compare against these files) |

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
