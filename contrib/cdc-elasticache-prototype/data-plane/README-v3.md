# CDC-cache Data Plane — Debezium Server + Valkey Streams broker (no Kafka)

## Architecture

```
Aurora / RDS (MySQL or PostgreSQL)
        │  binlog / WAL
        ▼
Debezium Server (ECS Fargate)          debezium.sink.type=redis
        │  writes data:<table> streams
        ▼
Broker — ElastiCache for Valkey (Redis Streams)   ← cluster mode DISABLED (see below)
        │  also holds Debezium offsets + schema history (Option B)
        ▼
Sink Worker (ECS Fargate)              consumes streams (XReadGroup), applies
        │  transforms, idempotent HSET, XACK + XDEL trim
        ▼
Target — ElastiCache for Valkey        ← the application reads here
```

This is the **"no Kafka"** design: Apache Kafka / MSK / Zookeeper / Kafka Connect
are replaced by a small **Valkey Streams broker**. It is *not* broker-less —
the Valkey Streams broker is the durable buffer between Debezium and the sink,
and it also stores Debezium's offsets and schema history (so no SQLite/EFS).

## Why the broker is cluster-mode DISABLED (important)

Deploy the broker with **`infra/broker-single.yaml`** (single shard,
cluster mode disabled, 1 primary + 1 replica for HA). Two reasons:

1. **Debezium's Redis sink is single-connection and not cluster-aware.** It
   writes to one primary endpoint and cannot follow cluster MOVED redirects, so
   it needs a **non-cluster** endpoint.
2. **A single Valkey Stream is one key → one slot → one shard → one main thread.**
   Cluster-mode-*enabled* does **not** shard or parallelize a single stream, so
   it buys nothing here. Benchmark 04 confirms the ceiling is the broker's single
   main thread (read + XACK).

> `infra/broker.yaml` (cluster-mode enabled) is **not** appropriate for this
> Debezium→broker path — see the note in that file / the top-level docs.

### Scaling past the single-broker wall
You don't cluster-enable one stream — you **partition into N streams across N
independent brokers** (by PK hash), each with its own sink workers, all writing
to the (sharded) target. Benchmark 04 §7: 1 stream ≈213K/s → 3 partition streams
≈271K/s. Live CDC is usually capped earlier by Debezium's single binlog reader
(~66K events/s), so partitioning mainly helps snapshot/backlog drain.

## Two sink implementations

| Sink | How it gets events | Status |
|---|---|---|
| **`tools/go-sink/`** (Go) | Consumes the broker's Valkey **Streams** (`XReadGroup`), the same streams Debezium writes to | **Canonical** — matches the Debezium `sink.type=redis` config and the benchmarks |
| `data-plane/sink-worker-v3/` (Python Flask) | Receives Debezium **HTTP** POSTs directly (broker-less variant) | Alternate/experimental — requires switching Debezium to the HTTP sink; not the default config |

The default `debezium-server/application.properties` uses the **redis (streams)**
sink, so the **Go sink is the path that matches the shipped config.**

## Components

```
data-plane/
├── debezium-server/
│   ├── Dockerfile
│   ├── application.properties        # MySQL source, redis(streams) sink, offsets+schema in broker
│   ├── application-pg.properties      # PostgreSQL variant
│   └── application-cdc.properties      # env-specific overrides
├── sink-worker-v3/                    # ALTERNATE Python HTTP-sink variant
└── bulk-loader/                       # parallel initial load (PK-range read -> target)
```

The canonical Go sink lives at `tools/go-sink/`.

## How it works

### Initial load, then CDC (two-phase)
1. `debezium.source.snapshot.mode=schema_only` (default) — Debezium captures
   schema only and starts streaming from the current binlog/WAL position.
2. The **parallel bulk loader** (`bulk-loader/`) does the fast initial data load
   straight into the target. Overlapping changes reconcile via idempotent HSET.
   (Set `SNAPSHOT_MODE=initial` to let Debezium do a full snapshot instead.)

### Steady state
1. Debezium reads the change log and writes each event to `data:<table>` on the broker.
2. The Go sink `XReadGroup`s a batch, applies transforms (filter → computed →
   columns → rename; denormalization merges child streams into the parent object),
   writes idempotent `HSET table:<pk>`, then `XACK` + `XDEL`-trims the stream so
   broker memory stays bounded.
3. Offsets flush to the broker every 1s; on task restart Debezium resumes from
   the stored offset — no data loss, idempotent replays.

## Configuration

All connection settings are environment variables (Quarkus `${VAR:default}`),
nothing hardcoded:

- Source: `SOURCE_DB_HOST/PORT/NAME`; `SOURCE_DB_USER/PASSWORD` from the DB secret, `TABLE_INCLUDE_LIST`, `TOPIC_PREFIX`
- Broker: `BROKER_HOST/PORT`, `BROKER_SSL`, `SINK_BATCH_SIZE`
- Offsets/schema keys: `OFFSET_KEY`, `SCHEMA_HISTORY_KEY`
- DB user and password are injected by ECS from the Secrets Manager secret (`DBSecretArn`).

## Deploy

Deploy `infra/v3-debezium.yaml` (ECS cluster + Debezium + sink services +
target). Deploy the broker separately with `infra/broker-single.yaml`.

## Cost (vs the old Kafka stack)

| Component | v1 (Kafka) | This design |
|---|---|---|
| Kafka/MSK + Zookeeper + Connect | ~$600–800/mo | $0 |
| Broker (small managed Valkey, e.g. cache.t4g.small) | (was a Valkey broker too) | ~small node |
| Debezium Server (Fargate) | — | ~$18/mo |
| Sink Worker (Fargate) | ~$18/mo | ~$18/mo |
| Target Valkey | required | required |

Replacing Kafka/MSK with a small Valkey Streams broker is the bulk of the
savings; see `docs/benchmarks/04-sink-drain-and-kafka-comparison.md` for the
throughput/latency comparison (Valkey Streams tie Kafka on throughput at ~90x
lower tail latency for this pipeline).

## Monitoring

- **Debezium health:** `:8080/q/health`
- **Sink metrics/logs:** CloudWatch Logs `/ecs/{stack}/debezium` and `/ecs/{stack}/sink-worker`
- **Broker/target:** ElastiCache CloudWatch metrics (engine CPU, `SetTypeCmds` = HSET rate on target; stream length / consumer lag on broker)
