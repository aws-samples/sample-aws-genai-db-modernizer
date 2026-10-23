# CDC-cache CDC Throughput Benchmark (single-lane path)

Measures the **CDC** path end-to-end (distinct from the initial-load benchmark):
Aurora binlog → Debezium Server (ECS) → broker Valkey Streams (single shard) →
Go sink (ECS) → target Valkey.

## Setup
- Source: Aurora MySQL `<source-mysql-cluster>`, table `orders`.
- Broker: `<broker-cluster>` (single shard, cluster-mode OFF).
- Sink: `<sink-service>` (ECS Fargate, autoscale min 1), passthrough + XDEL trim.
- Target: `<target-cluster>`.
- Method: generate N inserts into Aurora, measure until the target reflects all N.

## Result (1M)

| Phase | Value |
|---|---|
| Aurora insert generation | 1,000,000 in 23.9s (41,890 rows/s) |
| End-to-end propagation (Aurora → target) | 1,000,000 in **92.0s** |
| **CDC throughput** | **~10,869 events/sec** (steady ~10.8K/s) |

The broker stayed bounded throughout (XDEL-after-ack trim), no memory blow-up.

## Interpretation
~11K events/sec is the **single-lane CDC ceiling**: Debezium reads the binlog
sequentially and its Redis sink writes to **one** broker stream over a single
connection (the broker is single-shard because Debezium's Redis sink isn't
cluster-aware). The target is not the bottleneck here.

This is by design — the two paths serve different jobs:

| Path | Throughput | Use |
|---|---|---|
| Bulk loader (parallel, sharded, direct) | ~533K rows/s | one-time volume backfill |
| CDC (single-lane, ordered) | ~11K events/s | ongoing change delta |

~11K real changes/sec exceeds most systems' actual change rates, so the single-lane
path is sufficient for typical CDC. 

## Scaling CDC beyond the single lane
For higher CDC throughput, use the **partitioned path**: Debezium ContentBasedRouter
fans changes across N streams on a **cluster-mode broker**, consumed by N parallel
sink readers (the `go-multi` design). That removes the single-stream ceiling and is
the pending throughput enhancement.


---

# Sink-Drain Capacity — Plain vs Transforms (6-shard target)

Measures **sink capacity** (backlog/snapshot catch-up), Aurora/Debezium excluded:
a synthetic generator pre-loads the broker with N flat events, then the sink
drains them into the target. This is the number comparable to the historical
"222K" figure.

## Setup
- Host: c5.4xlarge (16 vCPU) — producer + sink.
- Broker: single Docker Valkey 8 (`io-threads=8`), one stream.
- Target: `<target-cluster-6-shards>` — 6 shards, cache.m6g.large, cluster-mode, TLS.
- Sink: Go, 16 workers, batch 20,000, cluster client, flat events (~95 B).
- Method: preload N → drain until consumer-group lag=0 & pending=0.
- Transforms run applies: column-select + rename + one computed field + a filter.

## Results

| Mode | 1M | 2M | 5M (steady) |
|---|---|---|---|
| Plain HSET | 190,038/s | 195,408/s | **200,100/s** |
| With transforms | 188,944/s | 190,829/s | **194,402/s** |

## Findings
- **~200K/s plain** at 5M reproduces the historical 222K-class result (same
  6-shard/16-worker shape; our target has 0 replicas and timing includes sink
  startup, hence ~200K vs 222K — same performance class).
- **Transforms cost ~3%** (200,100 → 194,402 at 5M). Filter + computed + rename +
  column-select is nearly free because the pipeline is **write-bound** (target
  HSET dominates), not CPU-bound on expression evaluation.
- **Denormalization (fan-out) is a different profile** — not measured here. Each
  child event adds an HMGET lookup (+ reverse-index SADD) and parent updates fan
  out, so it's round-trip-bound and much slower; it is not comparable to these
  write-bound numbers.

## Relationship to live CDC
This is drain/catch-up capacity, not sustained live CDC. Sustained CDC is bounded
by the source: Aurora Serverless v2 with binlog on emits ~tens of K row-changes/s
(single client measured ~40K/s), so end-to-end live CDC cannot approach 200K
regardless of downstream sizing.


---

# End-to-End CDC — Parallel Source Writers (real Aurora → target)

Real end-to-end (Aurora binlog → Debezium → broker → sink → target), pushing the
source as hard as possible with parallel writers to find the sustained ceiling.

## Setup
- Source: Aurora MySQL Serverless v2, table `orders`, binlog on.
- Writers: 24 parallel processes, batched inserts (2,000 rows/batch), 2M total.
- Debezium: ECS Fargate, 4 vCPU. Broker: single-shard `<broker-cluster>`.
- Sink: ECS Fargate, 2 tasks × 1 vCPU. Target: `<target-cluster>`.

## Result (2M)

| Metric | Value |
|---|---|
| Aurora source production (24 writers) | **96,086 rows/sec** |
| End-to-end propagation to target | 2,000,000 in **59.9s** |
| **Sustained end-to-end CDC** | **~33,400 events/sec** |

## Progression (end-to-end, same pipeline shape)

| Config | End-to-end CDC |
|---|---|
| Debezium 0.5 vCPU (single client source) | ~11K/s |
| Debezium 2 vCPU | ~22K/s |
| Debezium 4 vCPU + 24 parallel writers | **~33K/s** |

## Where the bottleneck is now
- **Not the source:** Aurora produced 96K rows/s (parallel) — 3× the pipeline rate.
- **Not the sink:** sink-drain capacity is ~200K/s (6-shard) / plenty at single-shard.
- **The limiter is the single-lane CDC path:** one Debezium binlog reader + one
  broker stream (single-shard broker, single Redis sink connection). ~34K/s is its
  ceiling at 4-vCPU Debezium.

## To scale past ~34K
Partition the CDC path downstream (Debezium ContentBasedRouter → N streams on a
cluster-mode broker → N parallel sink readers, `go-multi`) **only helps if the
limiter is downstream of the reader.** The 2026-07-14 CPU-profiling test (see
"Direct Confirmation" section below) shows it is **not**: Debezium's single decode
core is pegged, so live CDC is **read-bound**. Downstream fan-out therefore does
**not** raise live CDC — it only speeds backlog/snapshot drain. The real levers
are faster per-core compute for Debezium (→ ~66K) or **sharding the source**.


---

# CDC Ceiling — Official Statement (single-lane, no source sharding)

> **Without sharding the source, CDC is capped at one log-reader's throughput:**
> **~37K events/sec on Fargate**, up to **~66K events/sec on fast dedicated EC2 cores.**
> **More than that requires sharding the source (independent logs).**

## Why (the fundamentals)
- A relational database serializes committed changes into a **single ordered log**
  (binlog / WAL / redo / transaction log). Correct CDC must read it **in order**,
  so it is inherently **single-lane per log** — true for MySQL, PostgreSQL, Oracle,
  and SQL Server alike. This is not a Debezium or CDC-cache limitation.
- The reader/decoder is a **single thread**, so its throughput is bound by
  **per-core speed**, not by total vCPUs. Adding vCPUs does not help.

## Evidence (Debezium CPU scaling, 2M end-to-end, this deployment)

| Debezium vCPU | End-to-end CDC |
|---|---|
| 0.5 | ~11K/s |
| 2 | ~22K/s |
| 4 | ~33K/s |
| 8 | ~32K/s (flat — plateau confirmed) |

4→8 vCPU produced no gain: the single-threaded binlog reader is per-core bound on
Fargate. The historical **~66K** figure was the same single reader on a dedicated
c5.4xlarge (faster cores).

## What is NOT the bottleneck
- **Aurora source:** produced **96K rows/s** (24 parallel writers); can reach
  ~200K on large instances. Not the limit.
- **Sink:** ~200K/s drain capacity (6-shard). Not the limit.
- **Broker:** single-stream main-thread peaked ~64% at 37K. Not the limit.

## Levers to exceed the single-lane ceiling
1. **Faster per-core compute for Debezium** (EC2 c5/c6i/c7 instead of Fargate) →
   toward ~66K single-lane. No architecture change, no sharding.
2. **Shard the source** (multiple DB instances → independent logs → independent
   readers) → additive N × single-lane. The only path beyond ~66K.

Do **not** parallelize reading of a single log (multiple readers on one binlog):
it re-reads the whole log N times and breaks ordering — an anti-pattern.


---

# Direct Confirmation — Read-bound vs Sink-bound (CPU profiling, 2026-07-14)

The plateau above (4→8 vCPU flat) proves *something* single-lane binds the rate,
but "flat with more vCPUs" is consistent with **two** different bottlenecks:
(a) the binlog **read/decode** thread is CPU-saturated (read-bound), or
(b) Debezium's **single Redis sink connection** to one broker stream is the limit
(sink-bound). This test settles which.

## Method
- Live pipeline: Aurora MySQL → Debezium (ECS, **8 vCPU**) → single-shard broker
  `<broker-cluster>` → Go sink (ECS, 1 task × 1 vCPU) → target `<target-cluster>`.
- Load: 6.96M rows into `Inventory.orders` from 24 parallel writers on an in-VPC
  EC2 box (**Aurora produced 136K rows/s** — 4× the pipeline rate).
- Measured via CloudWatch during the streaming window:
  - **Debezium ECS `CPUUtilization`** — the discriminator. On 8 vCPU, one pegged
    core ≈ 12.5%. Read-bound → ~1 core busy; sink-bound → CPU idles below 1 core
    while blocked on writes.
  - Target `<target-cluster>` `SetTypeCmds` (HSET/min) — end-to-end rate.

## Result

| Component | Utilization at steady state | Reading |
|---|---|---|
| **Debezium (8 vCPU)** | **~13% = ~1.04 cores (one core pegged)** | decode thread CPU-saturated → **read-bound** |
| End-to-end rate | target HSET ~1.9M/min ≈ **~32K events/sec** | matches the ~33K ceiling |
| Broker `<broker-cluster>` (t4g.small) | engine CPU ~90% | near-saturated **in this small config** |
| Sink (1 task × 1 vCPU) | ~96% | near-saturated **at 1 task** |

## Verdict: **read-bound**
Debezium held a **full single core** (13% of 8 vCPU) at the ceiling. A sink-bound
process would sit *idle* waiting on writes; instead Debezium burns one core on
decode and cannot go faster. Combined with the 4→8 vCPU flat result, the
single-threaded binlog reader/decoder is the wall — exactly as the fundamentals
predict. Adding vCPUs cannot add decode threads.

## Caveat (honest scope of this run)
This was a **co-constrained** point: the small t4g.small broker (~90% engine CPU)
and the single 1-vCPU sink task (~96%) also had little headroom at 32K. That does
not change the verdict — a pegged decode core caps the rate regardless of
downstream size — but a clean "Debezium is the *sole* limit" demonstration would
give the broker + sink ample headroom (bigger broker, more sink tasks) and show
Debezium's core still capping while downstream sits idle.

## Consequence for the `go-multi` partitioned path
Because live CDC is **read-bound at Debezium's single decode core**, downstream
fan-out (`go-multi`: N broker streams + N sink readers) **cannot raise live CDC
throughput** — Debezium caps first. `go-multi` only accelerates *backlog drain /
snapshot catch-up* (the ~200K/s sink-drain number in doc 04), which is a different,
order-relaxed job. The only levers for higher *live* CDC remain: faster per-core
compute for Debezium (→ ~66K), or **sharding the source** into independent logs.
