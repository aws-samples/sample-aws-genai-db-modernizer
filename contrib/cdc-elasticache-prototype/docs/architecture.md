# CDC-cache Architecture (as deployed)

Account `<account-id>` · `<region>` · VPC `<vpc-id>`

![CDC-cache architecture](./architecture.svg)

> Rendered view above (`architecture.svg`). The editable source is
> `architecture.drawio` (open with the Draw.io Integration extension or
> [app.diagrams.net](https://app.diagrams.net)). A text-based version follows.

```mermaid
flowchart LR
    subgraph SRC["Source"]
        AUR[(Aurora MySQL\n<source-mysql-cluster>\nInventory.*)]
    end

    subgraph INIT["Initial Load (one-time)"]
        BL[bulk_loader.py\nparallel PK-range SELECT\n~143K rows/s]
    end

    subgraph CDC["CDC (steady state) — ECS Fargate"]
        DBZ[Debezium Server\n<debezium-service>\n1 task]
        SINK[Go Sink\n<sink-service>\n2 tasks · autoscale 1-10\n8 workers/task]
    end

    subgraph BROKER["Broker — ElastiCache Valkey (single shard, cluster-mode OFF, Multi-AZ)"]
        STREAM[["data:cdc-cache.Inventory.orders\n(Streams)"]]
        META[["cdc-cache:metadata:offsets\ncdc-cache:metadata:schema_history\n(Option B, volatile-lru)"]]
    end

    subgraph TGT["Target — ElastiCache Valkey (cluster-mode ON, 1 shard + 2 replicas, TLS)"]
        CACHE[(<target-cluster>\nHash per row: table:pk)]
    end

    APP([Application reads])
    SEC[[Secrets Manager\nsource DB secret]]

    AUR -- "SELECT * (PK ranges)" --> BL
    BL -- "pipelined HSET" --> CACHE

    AUR -- "binlog (TLS)" --> DBZ
    DBZ -- "XADD (Redis stream)" --> STREAM
    DBZ <-- "offset + schema (read/write)" --> META
    SEC -. "DB credentials (ECS secrets)" .-> DBZ
    STREAM -- "XREADGROUP" --> SINK
    SINK -- "ack + XDEL (trim)" --> STREAM
    SINK -- "HSET / DEL (idempotent by PK)" --> CACHE
    CACHE --> APP

    classDef managed fill:#e8f0fe,stroke:#4285f4;
    classDef ecs fill:#fef7e0,stroke:#f9ab00;
    class AUR,STREAM,META,CACHE,SEC managed;
    class DBZ,SINK ecs;
```

## Two data paths

1. **Initial load** — `bulk_loader.py` reads the source directly via parallel PK-range
   `SELECT`s and pipelines `HSET` straight into the target. Bypasses Debezium/broker.
   Volume tool (verified 1M/2M/5M @ ~127–147K rows/s).
2. **CDC (steady state)** — Aurora binlog → Debezium (ECS) → broker Streams → Go sink (ECS)
   → target. Offsets + schema history live in the broker (Option B, no SQLite/EFS).

## Key properties
- **Idempotent target writes** (`HSET`/`DEL` keyed by `table:pk`) → safe replays.
- **Bounded broker memory** — sink `XACK`s then `XDEL`s processed entries.
- **Offset safety** — `volatile-lru` never evicts the no-TTL offset key.
- **Scaling** — per-task `WORKERS` + ECS autoscaling; multiple sink tasks share one stream
  via the consumer group (unique `CONSUMER_PREFIX`).
- **Secrets** — DB user and password from the Secrets Manager secret, injected into the Debezium task by ECS (`secrets`, `valueFrom`).

## Current transformation behavior
- 1:1 mapping: each source row → one Valkey Hash (`table:pk`), all columns as string fields.
- Debezium applies routing (`RegexRouter` → `data:` prefix) + flat JSON only.
- Rich transforms (row filters, computed fields, denormalize, column selection) are **not**
  yet in the Go sink/bulk loader — gap to close if needed.

## Not yet deployed / known gaps
- Multi-shard partitioned throughput path (high-rate CDC).
- Multi-table (one sink service per stream today).
- DLQ, pipeline CloudWatch alarms + CDC-lag alarm, reconciliation job.
- Retire old Kafka/MSK stack (still live, untouched).

> The EC2 host (`<benchmark-host>`) is **not** in the architecture — it is only the
> container-image builder now; the pipeline it used during bring-up is stopped.
