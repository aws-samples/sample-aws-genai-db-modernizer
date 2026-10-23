# CDC-cache Benchmark 05 — Denormalization Throughput (fan-out)

Sink-drain (isolated, Aurora/Debezium excluded), same basis as docs 03/04, but
with a **denormalization rule** active. This is the round-trip-bound profile,
fundamentally different from the write-bound plain/transform numbers.

## Setup
- Host c5.4xlarge; broker single Docker Valkey (io-threads=8); target `<target-cluster-3-shards>` (3 shards, TLS).
- Denorm sink, 16 workers, envelope events. Rule: embed `customers.{name,tier}` onto `orders` as `cust_*`.
- 100,000 customers pre-seeded in the cache; 1M orders each referencing a customer.

## Results

| Phase | Work per event | Throughput |
|---|---|---|
| **Embed** (child insert) | `HMGET customers:{cid}` + embed + `HSET orders:{id}` + `SADD` (reverse index) | **~8,033 events/sec** |
| **Fan-out** (parent update) | `SMEMBERS` index + `HSET` each child (pipelined) | **~7,076 parent-updates/sec ≈ 70,756 child-writes/sec** |

Both verified correct: embed put `cust_name/cust_tier` on the order; a parent
update fanned the new values (`NN_* / platinum`) out to every child.

## Comparison across all sink-drain modes (same basis)

| Mode | Throughput | vs plain |
|---|---|---|
| Plain HSET (doc 04/03) | ~200K/s | 1× |
| Simple transforms (doc 03) | ~194K/s | ~0.97× (write-bound, ~3% cost) |
| **Denorm embed** | **~8K/s** | **~25× slower** |
| Denorm fan-out (child writes) | ~71K/s | — |

## Why embed is ~25× slower
It's **round-trip-bound, not write-bound.** Each event makes ~3 sequential
round-trips to the target (`HMGET` lookup → `HSET` → `SADD`), and the current
denorm path processes **per message without pipelining**. At ~1 ms/round-trip
over TLS with 16 workers, ~8K/s is expected. The plain/transform paths pipeline
thousands of `HSET`s per round-trip, so they're 25× faster.

Fan-out is faster per child-write (~71K/s) because the children of one parent are
**pipelined** in a single batch.

## End-to-end implication
Denorm embed (~8K/s) is **below** the single-lane CDC ceiling (~37K/s, doc 03).
So with denorm active, the **sink becomes the end-to-end bottleneck** (~8K/s),
not Debezium. Live end-to-end CDC with denormalization would run at ~8K/s on this
setup.

## Optimization path (not yet done)
This is the **unoptimized, correctness-first** denorm number. The standard fixes
(exactly what Redis RDI's Flink processor does) would raise it substantially:
- **Lookup batching** — pipeline the `HMGET` lookups for a whole read-batch instead of one per message.
- **Lookup result caching** — cache hot parent rows in the sink to skip repeat `HMGET`s.
- **Pipelined child writes** — batch the `HSET`/`SADD` across a batch of events.
With batching + caching, embed should move from ~8K/s toward the write-bound
range. Deferred until there's a need.
