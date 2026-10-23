# CDC-cache Benchmark 06 — PostgreSQL Source (Aurora PostgreSQL Serverless v2)

Proves the pipeline is **source-agnostic**: swapping MySQL → PostgreSQL required
only the Debezium connector + Postgres CDC enablement. Everything downstream
(broker, sink, target, offset-in-broker, transforms) was **unchanged**.

## Setup
- Source: **Aurora PostgreSQL Serverless v2** (`<source-postgres-cluster>`), engine 16.6,
  MinACU 1 / MaxACU 16, `wal_level=logical`, publication `dbz_pub`,
  `REPLICA IDENTITY FULL`.
- Debezium: `PostgresConnector` (pgoutput, slot `cdc_cache_slot`), run as Docker on the
  c5.4xlarge host, offset in the broker (Option B). No schema history (PG logical
  decoding carries schema).
- Broker: single Docker Valkey (io-threads=8) on the host. Target: `<target-cluster-3-shards>`.
- Loader: Python bulk loader, PostgreSQL adapter.

## Results

| Benchmark | Result |
|---|---|
| **Initial load** (bulk loader, PG → 3-shard target) | 2,016,000 rows in **8.5s = 238,072 rows/s** |
| **PG source production** (24 parallel writers) | **71,053 rows/s** |
| **CDC end-to-end** (Aurora PG → Debezium → broker → sink → target) | 2,000,000 in **43.2s = 46,326 events/s** (steady ~49.7K) |

## Key findings
1. **Source-agnostic confirmed.** Only two things changed vs MySQL: the connector
   class + Postgres CDC prerequisites (logical replication, publication, slot,
   REPLICA IDENTITY). The bulk loader's PostgreSQL adapter worked as-is; broker,
   sink, transforms, offset handling — all identical.
2. **Single-lane ceiling holds for Postgres too.** WAL logical decoding is
   single-slot / single-reader, same fundamental limit as MySQL's binlog.
3. **~46K vs MySQL's ~37K is NOT a clean engine comparison — different runtime.**
   > ⚠️ The **architecture is identical**, but the **Debezium runtime differed**:
   > this PG Debezium ran as Docker on the c5.4xlarge host (fast dedicated cores),
   > while the MySQL CDC number was on ECS Fargate (slower per-core). So the 46K vs
   > 37K gap is mostly **host-vs-Fargate per-core speed**, NOT PostgreSQL being
   > faster than MySQL. Do **not** read this as "Postgres CDC > MySQL CDC."
   What it *does* confirm (consistent with doc 03): the single-lane CDC ceiling is
   bound by the reader's **per-core speed** — ~37K on Fargate, ~46K on fast host
   cores. For a clean engine comparison, both must run on the same runtime.

## What it took to add Postgres (effort notes)
- Provision Aurora PG Serverless v2 + a cluster parameter group with
  `rds.logical_replication=1` (applied at creation — no reboot needed here).
- Open port 5432 to the app/host SG (mirror the 3306 rule).
- `pg_setup.py`: tables, `REPLICA IDENTITY FULL`, `CREATE PUBLICATION`, grant
  `rds_replication`.
- Swap Debezium config to `application-pg.properties` (connector + pgoutput + slot).
- Postgres gotcha: the **replication slot** retains WAL until consumed — monitor it
  so a stopped Debezium doesn't fill source disk.


---

## Fargate run — full ECS pipeline to a 6-shard target (2026-07-14)

Re-ran the PostgreSQL source live end-to-end with **everything on the managed
pipeline** (no EC2-host Debezium this time), at 1M / 2M / 5M:

- Source: Aurora PostgreSQL `<source-postgres-cluster>`, table `public.orders`, publication
  `dbz_pub`, slot `cdc_cache_slot`, snapshot `no_data` (pure live CDC).
- Debezium: **ECS Fargate, 4 vCPU** (`<pg-debezium-service>`, image
  `cdc-cache/debezium-server-pg`).
- Broker: **ElastiCache Valkey `<broker-cluster>`** (single shard, cluster-mode OFF).
- Sink: **ECS Fargate, 1 task × 1 vCPU, 16 workers** (`<pg-sink-service>`).
- Target: **ElastiCache Valkey `<target-cluster-6-shards>` — 6 shards**, cluster-mode, TLS.
- Load: `pg_parallel_insert.py`, 24 parallel writers, from an in-VPC EC2 host;
  target flushed before each run; end-to-end timed from insert start until the
  target reflects all N.

### Results

| Rows | PG source insert | End-to-end (blended, incl. insert) | Steady-state CDC |
|---|---|---|---|
| 1M | 52,308 rows/s | 1,000,000 in 34.2s = **29,276/s** | ~39K/s |
| 2M | 111,596 rows/s | 2,000,000 in 54.0s = **37,030/s** | ~39K/s |
| 5M | 117,719 rows/s | 5,000,000 in 129.7s = **38,559/s** | ~39K/s |

All rows delivered (measurement completes only when the 6-shard target reaches N).

### Findings
- **Steady-state CDC is a flat ~38–39K/s** regardless of volume — the single-lane
  ceiling. The blended rate rises toward it as N grows (the ~18–42s insert burst
  amortizes over a longer drain).
- **~38–39K on Fargate vs ~46K in the original host run** (doc 06 top): same
  architecture, different Debezium runtime (Fargate per-core < c5.4xlarge per-core).
  Consistent with doc 03's read-bound conclusion — PG WAL logical decoding is
  single-slot/single-reader, bound by per-core speed.
- **6-shard target was never the bottleneck** — the single-lane PG reader caps the
  rate long before a 6-shard target (which drains ~200K/s, per doc 04) is stressed.

### Gotcha caught during the run (worth remembering)
The first attempt delivered only **396K of 1M** rows. Root cause was **not** the
pipeline: leftover tasks from a prior session (two old `<pg-sink-service>:1` pointed
at the *1-shard* target, plus a second Debezium) were still running and **sharing
the `<pg-sink-container>` consumer group** — 48 consumers (16×3). The old sinks stole ~60%
of the messages and wrote them to the wrong target. Debezium had correctly produced
all 1,008,000 (`XINFO STREAM entries-added`), and the group's `entries-read` was
1,008,000 — the split across competing consumers was the tell. Fix: stop all stray
tasks, delete the stream + group, drop the slot, relaunch exactly one Debezium + one
sink. **Lesson: before a CDC benchmark, verify only the intended consumers are in the
group** (check `XINFO GROUPS` consumer count) and that no orphan `run-task` tasks
linger.

### PostgreSQL operational note
On teardown, the inactive `cdc_cache_slot` had retained **424 MB of WAL**. Always drop
the replication slot when stopping PG Debezium, or the source accumulates WAL
(disk-fill risk).


---

## MySQL source — same Fargate pipeline (engine comparison, 2026-07-14)

Ran the **identical** setup as the PG Fargate run above, only flipping the source
to **Aurora MySQL** (`<source-mysql-cluster>`), to isolate the effect of the source
engine. Same Debezium (ECS Fargate, **4 vCPU**), same single broker
`<broker-cluster>`, same Go sink (1 task × 1 vCPU), same **6-shard** target
`<target-cluster-6-shards>`.

### Results

| Rows | MySQL source insert | End-to-end (blended) | Steady-state CDC |
|---|---|---|---|
| 1M | 73,343 rows/s | 1,000,000 in 31.8s = **31,457/s** | ~37K/s |
| 2M | 206,673 rows/s | 2,000,000 in 54.9s = **36,448/s** | ~37–38K/s |
| 5M | 237,548 rows/s | 5,000,000 in 138.5s = **36,098/s** | **~37K/s (flat)** |

All rows delivered (measurement completes only when the 6-shard target reaches N).

### MySQL vs PostgreSQL on identical Fargate pipeline

| Source (Fargate 4-vCPU Debezium) | Steady-state live CDC |
|---|---|
| PostgreSQL (Aurora, pgoutput/logical) | ~38–39K/s |
| MySQL (Aurora, binlog) | ~37K/s |

**Conclusion: the source engine is essentially irrelevant to the live-CDC ceiling
(~37–39K/s on 4-vCPU Fargate).** Both are single-lane, single-log readers
(binlog vs WAL), so both are bound by one decode core — confirming the read-bound
result in doc 03. The tiny PG edge (~39K vs ~37K) is within noise / broker-thread
contention from co-tenant pipelines, not an engine property.

### Gotchas caught during this run (environment, not pipeline)
1. **Shared 1 GB broker contention.** `<broker-cluster>` (t4g.small) is shared by
   other live CDC pipelines (`cdc-cache`, and the `cdccacheq` query-proxy demo
   `<debezium-container>` on `<benchmark-host>`) that watch the **same** `Inventory.orders`
   table. A MySQL insert fans out to every connector, so ~1M inserts became ~3M
   events on one 1 GB broker → Debezium's `RedisMemoryThreshold` guard halted
   producers ("Stopped consuming records", used=maxmemory). PG didn't hit this
   because it reads a different source (`<source-postgres-cluster>`).
2. **Fix used: dedicated source table.** Pointed the benchmark's Debezium at a new
   `Inventory.bench_orders` table (fresh prefix `cdccachemy2`) that the other connectors
   don't include (they filter on `Inventory.orders`). No fan-out → broker stayed
   healthy → clean full-delivery runs, without stopping the live demo pipelines.
3. **Lesson:** on a shared broker, give each CDC benchmark its **own source table +
   own topic prefix + own consumer group**, and verify only the intended consumers
   are in the group (`XINFO GROUPS`).
