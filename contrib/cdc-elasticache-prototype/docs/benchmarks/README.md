# CDC-cache Benchmarks — Index

All performance benchmarks for the CDC-to-ElastiCache prototype pipeline, grouped
by what they measure. Account `<account-id>`, `<region>`.

| # | Doc | What it measures | Headline result |
|---|-----|------------------|-----------------|
| 01 | [initial-load-single-shard](01-initial-load-single-shard.md) | Parallel bulk loader (Python) into a **single-shard** target | 5M rows in ~35s (~143K rows/s) |
| 02 | [initial-load-sharded-go](02-initial-load-sharded-go.md) | Bulk loader (**Go**) into a **3-shard** target | 5M rows in ~8s (**~610K rows/s**) |
| 03 | [cdc-throughput-and-ceiling](03-cdc-throughput-and-ceiling.md) | **Live end-to-end CDC** (Aurora→Debezium→broker→sink→target) + the single-lane ceiling | **~37K/s** sustained (Fargate); ceiling statement |
| 04 | [sink-drain-and-kafka-comparison](04-sink-drain-and-kafka-comparison.md) | **Sink-drain capacity** (pre-loaded broker→target) + Valkey-vs-Kafka | ~200–222K/s drain; Valkey ~90× lower p99 latency |
| 05 | [denormalization-throughput](05-denormalization-throughput.md) | **Denorm (fan-out)** sink-drain — embed + parent fan-out | embed ~8K/s; fan-out ~71K child-writes/s |
| 06 | [postgresql-source](06-postgresql-source.md) | **PostgreSQL source** (Aurora PG Serverless) — proves source-agnostic | init load ~238K rows/s; CDC ~46K/s (on fast cores) |

## The four benchmark categories

| Category | Doc(s) | Result |
|---|---|---|
| 1. Pure pipeline initial load | 01, 02 | ~143K rows/s (1 shard) → ~610K rows/s (3-shard, Go) |
| 2. Sink worker capacity ceiling | 03, 04 | ~200K/s plain HSET (6-shard) |
| 3. With simple transformations | 03 | ~194K/s (filter+computed+rename+select; ~3% cost) |
| 4. With denormalization transforms | 05 | embed ~8K/s; fan-out ~71K child-writes/s (round-trip-bound) |

## The numbers at a glance (and what each one means)

Two independent data paths, benchmarked separately:

**Initial load** (one-time backfill; source → target directly, no Debezium/broker)
- Single-shard target, Python loader: **~143K rows/s** (5M in ~35s) — doc 01
- 3-shard target, Go loader: **~610K rows/s** (5M in ~8s) — doc 02
- Bottleneck was the single target shard; sharding + Go + parallelism scales it ~linearly.

**CDC** (steady-state change stream)
- **Sustained live CDC (end-to-end from Aurora): ~37K/s** on Fargate — doc 03
  - Bounded by the **single log-reader** (per-core speed), NOT Aurora (96K produced) or the sink (200K).
  - **Ceiling statement:** without sharding the source, CDC caps at one log-reader's throughput — **~37K/s on Fargate, ~66K/s on fast dedicated EC2 cores**; beyond that requires **sharding the source**.
- **Sink-drain / backlog-catch-up capacity: ~200K/s** (6-shard, plain), **~194K/s** with transforms (~3% cost) — docs 03 & 04
  - This is how fast the sink empties a *pre-loaded* broker (Debezium/Aurora excluded) — relevant for snapshots and recovery, NOT sustained live CDC.

## Key distinctions to avoid confusion
- **Initial-load rate (610K) ≠ CDC rate (37K).** Different tools, different jobs.
- **Sink-drain capacity (200K) ≠ sustained live CDC (37K).** The sink can drain far
  faster than a single source log can produce; live CDC is source/reader-bound.
- **Transforms** (filter + computed + rename + column-select) cost only ~3% — write-bound, not CPU-bound.
- **Denormalization (fan-out)** is a separate, round-trip-bound profile — not in these throughput numbers.

## Reproduce these benchmarks

Run from an in-VPC host with the environment set (either `./quickstart.sh deploy`
first, or export `SOURCE_DB_HOST` / `SOURCE_DB_SECRET_ARN` / `VALKEY_HOST` for BYO).

**Initial-load throughput (docs 01, 02)** — needs only source + target:
```bash
./quickstart.sh benchmark                     # small default sizes (100k, 500k)
CDC_CACHE_BENCH_SIZES=1000000,2000000,5000000 ./quickstart.sh benchmark   # reproduce the doc numbers
# or directly:
cd data-plane/bulk-loader && python3 initial_load_benchmark.py --sizes 5000000 --workers 16
```
Note: this flushes the target and loads the `orders` table.

**End-to-end CDC throughput (docs 03, 04)** — needs the full pipeline running
(`./quickstart.sh cdc`, or deploy `infra/broker-single.yaml` and `infra/cdc-ecs.yaml`):
```bash
./quickstart.sh benchmark-cdc                 # default n=200k
CDC_CACHE_BENCH_CDC_N=1000000 ./quickstart.sh benchmark-cdc
# or directly:
cd data-plane/bulk-loader && python3 cdc_throughput_bench.py --n 1000000 --batch 1000
```

All benchmark scripts read the same env contract (`SOURCE_DB_*`, `VALKEY_HOST`)
and print rows/sec; compare your numbers against the tables above (hardware and
target shard count will shift the absolute figures).
