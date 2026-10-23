# CDC-cache Initial-Load Benchmark (Parallel Bulk Loader)

End-to-end **initial load** throughput for the CDC-to-ElastiCache prototype
parallel bulk loader: **source table (SELECT) → parallel PK-range read → pipelined
HSET → target Valkey**. This is the one-time snapshot path that runs *before*
Debezium CDC takes over for steady state.

> Companion doc: ongoing CDC / sink throughput is in `04-sink-drain-and-kafka-comparison.md` and `03-cdc-throughput-and-ceiling.md`.
> This file covers the **initial load** only.

---

## 1. Test Environment

| Component | Detail |
|---|---|
| Loader host | EC2 `<instance-id>` (c5.4xlarge), in-VPC |
| Loader | Python, `ProcessPoolExecutor`, 16 worker processes, pipelined HSET |
| Source | Aurora MySQL `<source-mysql-cluster>`, table `orders` (numeric PK `id`) |
| Target | ElastiCache for Valkey `<target-cluster>` — **single shard, cache.t3.medium, cluster mode, TLS** |
| Key pattern | `orders:{id}` (Hash per row) |
| Region / account | `<region>` / `<account-id>` |

**Method (per size):**
1. Ensure source has exactly N rows (fast `INSERT ... SELECT` doubling).
2. Flush the target cluster.
3. Discover PK bounds (`MIN/MAX(id)` — index lookup, instant), split `[min..max]`
   into 16 contiguous ranges.
4. 16 processes each `SELECT * WHERE id BETWEEN lo AND hi` and pipeline `HSET`
   rows to the target.
5. Verify target key count == N.

---

## 2. Results (verified exact)

| Rows | Load time | Throughput | Verified |
|---|---|---|---|
| 1,000,000 | 7.9 s | 127,251 rows/sec | ✅ exact |
| 2,000,000 | 13.6 s | 147,222 rows/sec | ✅ exact |
| 5,000,000 | 34.9 s | 143,089 rows/sec | ✅ exact |

Every run verified: loaded row count == target key count.

---

## 3. Interpretation

- **5 million rows loaded into cache in ~35 seconds** — the initial load is
  minutes-scale, not hours. This removes the "great at CDC, but initial load
  takes hours" concern.
- These numbers are on a **single small burstable shard (t3.medium)**. The sink
  benchmark (`04-sink-drain-and-kafka-comparison.md`) shows 3–6 shard targets reaching
  200K+/sec, so a right-sized multi-shard target scales initial load down
  further. Throughput rises with target shards.
- Correctness is proven, not assumed — exact count verification on every run.

### Why the loader is fast (and CDC isn't, for bulk)
Reading a table is **embarrassingly parallel** (independent PK-range SELECTs),
so it scales with worker + shard count. The MySQL binlog is a **single
sequential log**, so steady-state CDC is single-threaded (~66K/sec) — fine for
change volume, wrong tool for a big one-time load. CDC-cache uses the right tool per
phase: parallel loader for the snapshot, Debezium for the deltas.

---

## 4. Two-Phase Load (how this fits the pipeline)

1. **Capture** the source binlog position.
2. **Parallel bulk load** (this benchmark) — fast, may be slightly stale per
   chunk; that's fine.
3. **Start Debezium** from the captured position (`snapshot.mode=schema_only`) —
   replays every change since; idempotent `HSET` by PK overwrites any stale row.
4. **Safe-to-read gate** — do not let the app read the cache until CDC lag ≈ 0
   (and optional source-vs-cache parity). Correctness comes from CDC catch-up +
   idempotency, not from snapshot point-in-time consistency.

---

## 5. Prerequisite (flag in Assess)

The loader chunks on an **indexed, numeric PK** (`MIN/MAX` + range scan use the
index). This is a **performance** prerequisite, not a correctness one — without
an index the load still completes, just slower (full scans). Non-numeric PKs
(UUID/string) would use hash/percentile chunking instead. Validate "indexed
chunking key present" during pre-flight assessment.

---

## 6. Artifacts

- Loader: `data-plane/bulk-loader/bulk_loader.py` (source-pluggable: MySQL/PostgreSQL concrete; Oracle/SQL Server drop-in)
- Benchmark runner: `data-plane/bulk-loader/initial_load_benchmark.py`
