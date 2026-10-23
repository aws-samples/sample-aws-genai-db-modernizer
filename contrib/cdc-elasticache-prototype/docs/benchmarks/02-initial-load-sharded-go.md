# CDC-cache Initial-Load Benchmark — Sharded Target + Go Loader

Applies the four throughput levers over the original single-shard Python loader:
1. **Sharded target** — 3 primary shards (writes fan out across 3 nodes).
2. **More parallelism** — 32–48 goroutine workers over PK ranges.
3. **Larger pipelines** — batch 3000 HSET per round-trip.
4. **Go loader** — lower per-row CPU than Python; cluster-aware (go-redis routes each key to its shard).

## Environment
- Source: Aurora MySQL `<source-mysql-cluster>`, table `orders`.
- Target: **`<target-cluster-3-shards>`** — 3 shards × (1 primary + 2 replicas), `cache.t3.medium`, cluster-mode ON, Multi-AZ, TLS.
- Loader: `tools/go-loader` (Go, static binary), run in-VPC on the EC2 build host.
- Region `<region>`, account `<account-id>`.

## Results

| Rows | Loaded | Time | Throughput |
|-----:|-------:|-----:|-----------:|
| 1M | 987,749 | 1.85s | **533,189 rows/sec** |
| 2M | 1,951,469 | 3.57s | **546,989 rows/sec** |
| 5M | 4,871,324 | 7.98s | **610,306 rows/sec** |

(Row counts are the actual rows present in each PK range; small gaps come from earlier delete-tests.)

## Comparison vs single-shard Python loader

| Rows | Single-shard Python | 3-shard Go | Speedup |
|-----:|--------------------:|-----------:|--------:|
| 1M | 143K/s (7.9s) | 533K/s (1.85s) | ~3.7× |
| 2M | 147K/s (13.6s) | 547K/s (3.57s) | ~3.7× |
| 5M | 143K/s (34.9s) | 610K/s (7.98s) | ~4.3× |

5M initial load dropped from ~35s to ~8s.

## Why it scaled
The single-shard result was bounded by one Valkey primary's write ceiling, not by binlog/CDC (Debezium is bypassed entirely for initial load). Spreading writes across 3 primaries lifted that ceiling ~linearly; the Go loader + larger batches kept the client side from becoming the new bottleneck. Headroom remains: more shards and/or more loader hosts would push it further.

## Notes
- `WORKERS`/`BATCH`/`PK_LO`/`PK_HI` are env-configurable on the Go loader.
- The broker stays single-shard by necessity (Debezium's Redis sink isn't cluster-aware); this sharding applies to the **target** only.
- Recommended default for new targets: **3 shards × 2 replicas** (set in the infra templates).
- `<target-cluster-3-shards>` is a benchmark cluster (9 nodes) — delete when done to avoid cost.
