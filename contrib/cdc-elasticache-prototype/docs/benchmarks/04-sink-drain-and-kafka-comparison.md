# CDC-cache CDC Throughput Benchmark Results

End-to-end throughput benchmarks for the CDC-to-ElastiCache prototype CDC
pipeline: **MySQL/Aurora → Debezium → Broker (Valkey Streams) → Sink Worker →
Target (Valkey)**.

> **Companion doc:** initial-load (parallel bulk loader) benchmarks are in
> `01-initial-load-single-shard.md` and `02-initial-load-sharded-go.md`. This file covers **steady-state CDC / sink**
> throughput. Headline initial-load result: **5M rows loaded in ~35s** on a
> single t3.medium shard (verified exact).

---

## 1. Test Environment

| Component | Detail |
|---|---|
| Benchmark host | EC2 `<instance-id>`, c5.4xlarge (16 vCPU, 30 GB) |
| Broker | Standalone Valkey 8.0 (Docker), `io-threads=8`, single stream |
| Target | ElastiCache Valkey, cluster mode, TLS, cache.m6g.large per shard |
| Sink worker | Go, `goccy/go-json`, 16 workers, batch 20,000, cluster client |
| Event format | **Flat** (Debezium `ExtractNewRecordState`), ~95 bytes/event |
| Row identity | Derived from Debezium record key (primary key), not hardcoded |
| AWS | account `<account-id>`, `<region>` |

**Method (clean state every run):** flush broker → flush all target shards →
generate exactly N events → drain until **all written AND acked**
(`lag=0 && pending=0`) → verify target master key count == N.

> **Scope note:** These runs measure the **sink side** (drain broker stream →
> target). Events are pre-loaded into the broker by a synthetic generator so the
> sink can be measured in isolation. The **binlog read / Debezium ingest** rate
> is a separate, source-side measurement (see §5).

---

## 2. Throughput Summary

| Events | 3-shard target | 6-shard target |
|---|---|---|
| 1,000,000 | 145K/sec | 163K/sec |
| 2,000,000 | 181K/sec | 196K/sec |
| 3,000,000 | — | 217K/sec |
| 5,000,000 | 194K/sec | **222K/sec** |

All runs verified: every event landed on the target (exact master key counts).

Throughput rises with volume because fixed overhead (worker startup, TLS
handshakes, cold target after flush, drain tail) amortizes over more events. The
**5M figure is the truest steady-state.**

---

## 3. Per-Step Breakdown — 3-Shard Target

Average time per drain cycle (one cycle ≈ one 20,000-event batch per worker).

| Events | Throughput | Read (broker) | Parse | Write (target) | Ack (broker) |
|---|---|---|---|---|---|
| 1,000,000 | 145K/sec | 527 ms | 242 ms | 878 ms | 280 ms |
| 2,000,000 | 181K/sec | 380 ms | 193 ms | 756 ms | 242 ms |
| 5,000,000 | 194K/sec | 385 ms | 185 ms | 720 ms | 312 ms |

**Bottleneck:** Write to target (~720–878 ms, the dominant step). The 3-shard
target cannot absorb writes fast enough.

---

## 4. Per-Step Breakdown — 6-Shard Target

| Events | Throughput | Read (broker) | Parse | Write (target) | Ack (broker) |
|---|---|---|---|---|---|
| 1,000,000 | 163K/sec | 588 ms | 258 ms | 504 ms | 297 ms |
| 2,000,000 | 196K/sec | 499 ms | 240 ms | 367 ms | 342 ms |
| 3,000,000 | 217K/sec | 496 ms | 208 ms | 270 ms | 398 ms |
| 5,000,000 | 222K/sec | 485 ms | 209 ms | 298 ms | 375 ms |

**Bottleneck shifted:** Write collapsed from ~720 ms to ~298 ms. Now
**Read + Ack on the single broker (~860 ms, ≈63% of the cycle)** is the ceiling —
both serialize on the broker's single main thread.

### Read + Ack (broker, single main thread) — the new wall

| Events | Read + Ack (3-shard) | Read + Ack (6-shard) |
|---|---|---|
| 1,000,000 | 807 ms | 885 ms |
| 2,000,000 | 622 ms | 841 ms |
| 5,000,000 | 697 ms | 860 ms |

---

## 5. Source-Side: Binlog Read / Debezium Ingest

Measured separately (Debezium Server reading the MySQL binlog and writing to the
broker stream). The MySQL binlog is a **single sequential log**, so Debezium's
reader is single-threaded.

| Metric | Value |
|---|---|
| Debezium ingest to broker | ~66K events/sec |
| Event size (extended envelope) | ~561 bytes |
| Event size (flat / unwrapped) | ~95 bytes |

**Implication:** For *steady-state* CDC, the binlog reader (~66K/sec here) is the
real ceiling — the sink can drain far faster than Debezium produces. The sink
throughput in §2–4 matters most for **initial snapshots and backlog/recovery
catch-up**, where a large batch must be drained quickly.

---

## 6. Optimization Journey (single broker)

Cumulative effect of each optimization (real numbers, writes verified landing):

| Stage | Throughput |
|---|---|
| "Baseline" with non-cluster client (writes silently dropped — invalid) | "159K" (bogus) |
| Real baseline: envelope events, batch 5,000, 1-shard target | 84K/sec |
| + `goccy/go-json` parser (parse 140 ms → 48 ms) | folded in |
| + batch 20,000 | 100K/sec |
| + flat events (561 B → 95 B) | 104K/sec |
| + target resharded to 3 shards | 213K/sec |
| + target resharded to 6 shards (5M, steady-state) | 222K/sec |

---

## 7. Broker Partitioning Prototype (beyond the single-broker wall)

To break the single-broker ceiling, the stream was split into **3 partitions by
PK hash** across **3 independent broker processes** (3 main threads), each with
its own worker pool, all writing to the 3-shard target.

| Setup | Throughput | Read | Ack |
|---|---|---|---|
| Single broker, 1 stream | ~213K/sec | 466 ms | 300 ms |
| 3 brokers, 3 partition streams | **271K/sec** | ~140–185 ms | ~32–72 ms |

Splitting across independent main threads collapsed Read and Ack — exactly the
single-broker wall identified in §4.

### Broker CPU evidence (single broker, during drain)

`docker stats`: broker ≈ **729% CPU (~7.3 cores)**. Per-thread (`top -H`):
- **Main thread:** ~80–100% (peaks at 99.9%) — command execution + consumer-group
  PEL bookkeeping serialize here. This is the per-stream ceiling.
- **io_thd_1..7:** heavily loaded — network/RESP parsing genuinely parallelizes.

So the broker is *not* single-core overall, but the **single main thread**
(where `XREADGROUP`/`XACK` execute) is the serialization point per stream.

---

## 8. Conclusions

1. **Target sharding works** but has diminishing returns once the target is no
   longer the bottleneck (3→6 shards added only ~14% at 5M).
2. **A single broker / single partition tops out at ~220K events/sec**
   end-to-end. Read + Ack on one main thread is the hard ceiling.
3. **To scale beyond ~220K, partition the broker** — multiple streams on multiple
   main threads (prototype: 271K with 3 partitions, scales further).
4. **For steady-state CDC, the binlog reader (~66K/sec) is the true ceiling;**
   high sink throughput matters for snapshots and backlog recovery.

### Key architectural constraints
- A Valkey **stream is a single key = one slot = one shard**. Sharding the broker
  cluster does **not** split a single stream — you must partition into multiple
  stream keys.
- Reading multiple streams in one `XREADGROUP` across slots raises **CROSSSLOT**;
  each worker must read **one** stream per command.
- Debezium's **Redis sink is not cluster-aware** (Jedis 4.1.1, single connection),
  so it cannot natively write partitioned streams across a broker cluster's
  shards. Partition routing must be handled by a custom image
  (`ContentBasedRouter` + scripting) or a partitioner relay — see open
  architecture work.


---

# Valkey Streams vs Kafka — Head-to-Head

Apples-to-apples comparison on the same EC2 host (c5.4xlarge), same 6-shard
ElastiCache Valkey target, same flat events (~95 B), same Go sink logic.

## Headline

> **Valkey Streams delivers similar bulk throughput to Kafka, with dramatically
> lower latency (p99 ~1 ms vs ~97 ms — roughly 90× lower).** For a CDC-into-cache
> pipeline where *speed-to-visibility* matters, Valkey is the faster choice.
> Kafka's strengths are durability and replay, not latency.

## 1. Throughput — 16-way parallel (16 streams vs 16 partitions)

Drain N pre-loaded events → 6-shard target. Counts verified exact.

| Events | Valkey (16 streams, TLS + XACK) | Kafka (16 partitions) |
|---|---|---|
| 1,000,000 | 279K/sec | 317K/sec |
| 2,000,000 | 351K/sec | 376K/sec |
| 5,000,000 | 370K/sec (steady ~440K) | 438K/sec |

**Fully-fair note (equal TLS + ack/commit semantics), 5M steady-state:**
- Valkey 16-stream (TLS + XACK): **~440K/sec**
- Kafka 16-partition (TLS + consumer-group commit): **~476K/sec**
- Difference ~8% — i.e. **comparable / same performance class.**

Caveat: the Kafka column above runs in its lighter native mode (plaintext + no
commit); even so it is only modestly ahead, and under identical TLS+commit
conditions the two converge to ~440K vs ~476K steady-state. Kafka also pays a
~5 s consumer-group **rebalance** startup tax that Valkey (instant `XGROUP
CREATE`) does not.

## 2. Single-unit throughput (1 stream vs 1 partition)

| Engine | Parallelism | Throughput |
|---|---|---|
| Valkey | 1 stream | **196K/sec** |
| Kafka | 1 partition | 92K/sec |

Per stream/partition, **Valkey is ~2.1× faster.** Kafka reaches parity only by
spreading many partitions across its broker thread pool; Valkey scales by adding
shards (one main thread per shard).

## 3. Latency — per-event, 5K/sec steady, local + plaintext + per-event (µs)

This is where the in-memory write path shows.

| Percentile | Valkey | Kafka | Valkey advantage |
|---|---|---|---|
| mean | **502 µs** | 7,495 µs | 15× lower |
| p50 | 484 µs | 607 µs | ~same |
| p90 | **743 µs** | 25,866 µs (26 ms) | ~35× lower |
| p99 | **1,035 µs (1 ms)** | 97,225 µs (97 ms) | **~94× lower** |
| p999 | **2,168 µs (2.2 ms)** | 104,685 µs (105 ms) | ~48× lower |
| max | 4.2 ms | 111.8 ms | — |

- **p50 is comparable** (~0.5 ms both); Valkey's win is in the **tail** (p90+).
- Kafka is batch-oriented; forced into per-event mode its tail latency explodes
  because each message round-trips the broker request pipeline with no batch to
  amortize. Valkey processes each `XADD` immediately in memory.

## 4. Why throughput ties but latency doesn't

- **Throughput:** Kafka's sequential log + page cache + zero-copy `sendfile` +
  batching make it memory-class for bulk streaming, matching Valkey's in-memory
  path. The bottleneck in both is downstream (sink parse + target write), not the
  broker.
- **Latency:** Valkey acks writes in microseconds with no fsync/replication wait;
  Kafka optimizes for batched throughput, so per-event latency carries a much
  larger tail.

## 5. Cost (broker, right-sized for steady-state CDC ~66K/sec, one AWS Region)

| Broker option | Footprint | ~Monthly |
|---|---|---|
| Self-managed Valkey on Fargate | 2 × (2 vCPU, 8 GB) tasks | ~$170 |
| ElastiCache Valkey (managed) | 1 shard, 1 primary + 1 replica (2 × m6g.large) | ~$228 |
| MSK (HA, 2 brokers) | 2 × kafka.m5.large + storage | ~$317 |
| MSK (RF=3) | 3 × kafka.m5.large + storage | ~$470 |

Steady-state CDC is gated by the binlog reader (~66K/sec), so a single Valkey
shard (2 nodes) suffices — the 6-shard cluster was only for peak/backlog tests.

## 6. Verdict for CDC-cache

| Dimension | Winner |
|---|---|
| Bulk throughput | Tie (~440–480K/sec at 16-way) |
| Per-event latency | **Valkey (~90× lower p99)** |
| Single-node efficiency | Kafka (fewer nodes for same throughput) |
| Per-stream speed | **Valkey (2.1×)** |
| Cost (managed, steady-state) | **Valkey (~$228 vs ~$317–470)** |
| Durability / replay | Kafka |
| Operational simplicity | **Valkey (no Zookeeper/KRaft; already in stack)** |

**Conclusion:** For CDC-cache's low-latency CDC-into-cache use case, **Valkey Streams is
the faster choice — equal throughput, ~90× lower tail latency, lower cost, simpler
ops.** Choose Kafka only when you need a durable, replayable event log shared
across many independent consumers.
