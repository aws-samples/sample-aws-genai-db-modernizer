#!/usr/bin/env python3
"""
CDC-cache Parallel Bulk Loader — fast initial load for any relational source.

Reads an existing source table in parallel PK-range chunks and writes rows
directly to the Valkey target (cluster-aware, pipelined HSET). This is the
"initial load" path that runs BEFORE Debezium CDC takes over for steady state.

Design:
  - Source is pluggable via a small SourceAdapter interface (snapshot only).
    MySQL and PostgreSQL are concrete here; Oracle/SQL Server are drop-in
    (same interface, swap the driver + bounds/chunk SQL).
  - The table is split into N ranges by primary key (min..max), and each range
    is loaded by a separate process (multiprocessing = real parallelism).
  - Writes go straight to the target Valkey cluster, pipelined.

Two-phase load story:
  1. Capture the source change-log position (binlog/LSN/SCN) — done by the
     orchestrator, not here.
  2. Run THIS loader (fast, parallel) to populate the cache.
  3. Start Debezium from the captured position (schema_only) for steady-state
     CDC. Overlapping changes are reconciled by idempotent HSET upserts.

Usage:
  python3 bulk_loader.py --config loader.json
  python3 bulk_loader.py --source mysql --table orders --pk id \
      --workers 16 --batch 5000
"""
import argparse
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# Source adapters — snapshot capability only (parallel range read).
# ─────────────────────────────────────────────────────────────────────────────

class SourceAdapter:
    """Interface: bounds() -> (min_pk, max_pk); fetch(lo, hi) -> list[dict]."""
    def connect(self): raise NotImplementedError
    def bounds(self, table, pk): raise NotImplementedError
    def fetch(self, table, pk, lo, hi): raise NotImplementedError


class MySQLAdapter(SourceAdapter):
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import pymysql
        self.conn = pymysql.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 3306),
            user=self.cfg["user"], password=self.cfg["password"],
            database=self.cfg["database"], cursorclass=pymysql.cursors.DictCursor,
        )

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(f"SELECT MIN(`{pk}`) lo, MAX(`{pk}`) hi FROM `{table}`")
            r = c.fetchone()
            return r["lo"], r["hi"]

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor() as c:
            c.execute(f"SELECT * FROM `{table}` WHERE `{pk}` >= %s AND `{pk}` <= %s", (lo, hi))
            return c.fetchall()


class PostgreSQLAdapter(SourceAdapter):
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import psycopg2
        import psycopg2.extras
        self.conn = psycopg2.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 5432),
            user=self.cfg["user"], password=self.cfg["password"],
            dbname=self.cfg["database"],
        )
        self._dict = psycopg2.extras.RealDictCursor

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(f'SELECT MIN("{pk}") lo, MAX("{pk}") hi FROM "{table}"')
            lo, hi = c.fetchone()
            return lo, hi

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor(cursor_factory=self._dict) as c:
            c.execute(f'SELECT * FROM "{table}" WHERE "{pk}" >= %s AND "{pk}" <= %s', (lo, hi))
            return list(c.fetchall())


class OracleAdapter(SourceAdapter):
    """Oracle source (RDS oracle-ee / oracle-se2). Uses python-oracledb in thin
    mode (no Oracle Instant Client needed). `database` is the service name
    (e.g. ORCL). Identifiers are quoted with double quotes; Oracle folds unquoted
    names to UPPERCASE, so pass table/pk exactly as stored."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import oracledb
        self.conn = oracledb.connect(
            user=self.cfg["user"], password=self.cfg["password"],
            dsn=oracledb.makedsn(
                self.cfg["host"], self.cfg.get("port", 1521),
                service_name=self.cfg["database"]),
        )

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(f'SELECT MIN("{pk}"), MAX("{pk}") FROM "{table}"')
            lo, hi = c.fetchone()
            return lo, hi

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor() as c:
            c.execute(
                f'SELECT * FROM "{table}" WHERE "{pk}" >= :lo AND "{pk}" <= :hi',
                {"lo": lo, "hi": hi})
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, row)) for row in c.fetchall()]


# SQL Server is the same shape — swap the driver and quoting:
#   SQLServer:  import pyodbc;    quote with [...]; TOP/OFFSET-FETCH if needed
ADAPTERS = {"mysql": MySQLAdapter, "postgresql": PostgreSQLAdapter, "oracle": OracleAdapter}


# ─────────────────────────────────────────────────────────────────────────────
# Worker — runs in its own process; loads one PK range into Valkey.
# ─────────────────────────────────────────────────────────────────────────────

def load_range(args):
    src_type, src_cfg, target_cfg, table, pk, key_prefix, lo, hi, spec, table_name = args
    from valkey.cluster import ValkeyCluster
    import transforms

    adapter = ADAPTERS[src_type](src_cfg)
    adapter.connect()

    vk = ValkeyCluster(
        host=target_cfg["host"], port=target_cfg.get("port", 6379),
        ssl=target_cfg.get("ssl", True), ssl_cert_reqs=None,
        decode_responses=False,
    )

    rows = adapter.fetch(table, pk, lo, hi)
    written = 0
    pending = 0
    pipe = vk.pipeline(transaction=False)
    for row in rows:
        # Stringify the row, then apply the SAME transform spec the sink uses so
        # the initial load and CDC shape data identically.
        row_str = {str(k): ("" if v is None else str(v)) for k, v in row.items()}
        out, keep = transforms.apply(spec, table_name, row_str)
        if not keep:
            continue  # filtered out — loader simply skips (nothing to delete yet)
        key = f"{key_prefix}{row[pk]}"
        pipe.hset(key, mapping=out)
        written += 1
        pending += 1
        if pending >= 1000:
            pipe.execute()
            pending = 0
            pipe = vk.pipeline(transaction=False)
    if pending:
        pipe.execute()
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def chunk_ranges(lo, hi, n):
    """Split [lo, hi] into n contiguous integer ranges."""
    lo, hi = int(lo), int(hi)
    span = hi - lo + 1
    size = max(1, math.ceil(span / n))
    ranges = []
    start = lo
    while start <= hi:
        end = min(start + size - 1, hi)
        ranges.append((start, end))
        start = end + 1
    return ranges


def main():
    ap = argparse.ArgumentParser(description="CDC-cache parallel bulk loader")
    ap.add_argument("--config", help="JSON config file")
    ap.add_argument("--source", choices=list(ADAPTERS))
    ap.add_argument("--table")
    ap.add_argument("--pk", default="id")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--key-prefix", default=None, help="default: {table}:")
    ap.add_argument("--transforms", default=os.environ.get("TRANSFORMS_FILE"),
                    help="path to shared transforms.json (optional)")
    ap.add_argument("--min-pk", type=int, default=None,
                    help="only load rows with pk >= this (incremental load from a watermark)")
    ap.add_argument("--max-pk", type=int, default=None,
                    help="only load rows with pk <= this")
    args = ap.parse_args()

    if args.config:
        cfg = json.load(open(args.config))
    else:
        cfg = {
            "source_type": args.source,
            "source": {
                "host": os.environ["SOURCE_DB_HOST"],
                "port": int(os.environ.get("SOURCE_DB_PORT", 3306)),
                "user": os.environ["SOURCE_DB_USER"],
                "password": os.environ["SOURCE_DB_PASSWORD"],
                "database": os.environ["SOURCE_DB_NAME"],
            },
            "target": {
                "host": os.environ["VALKEY_HOST"],
                "port": int(os.environ.get("VALKEY_PORT", 6379)),
                "ssl": os.environ.get("VALKEY_SSL", "true").lower() == "true",
            },
            "table": args.table,
            "pk": args.pk,
            "workers": args.workers,
        }

    src_type = cfg["source_type"]
    table = cfg["table"]
    pk = cfg["pk"]
    workers = cfg.get("workers", 16)
    key_prefix = args.key_prefix or cfg.get("key_prefix") or f"{table}:"

    # Table name used to look up transform rules (short name, e.g. "orders").
    table_name = table.split(".")[-1]
    import transforms
    spec = transforms.load_spec(args.transforms) if args.transforms else None

    print(f"CDC-cache bulk loader | source={src_type} table={table} pk={pk} workers={workers} "
          f"transforms={'on' if spec else 'off'}")

    # 1) Discover PK bounds
    probe = ADAPTERS[src_type](cfg["source"])
    probe.connect()
    lo, hi = probe.bounds(table, pk)
    if lo is None:
        print("Table is empty — nothing to load.")
        return
    # Optional watermark bounds: load only rows within [min-pk, max-pk]. Useful
    # for incremental loads (e.g. everything inserted after the last max PK).
    if args.min_pk is not None:
        lo = max(int(lo), args.min_pk)
    if args.max_pk is not None:
        hi = min(int(hi), args.max_pk)
    if int(lo) > int(hi):
        print(f"No rows in PK range [{args.min_pk}..{args.max_pk}] — nothing to load.")
        return
    ranges = chunk_ranges(lo, hi, workers)
    print(f"PK range [{lo}..{hi}] split into {len(ranges)} chunks")

    # 2) Load ranges in parallel
    start = time.time()
    tasks = [
        (src_type, cfg["source"], cfg["target"], table, pk, key_prefix, r[0], r[1], spec, table_name)
        for r in ranges
    ]
    total = 0
    done = 0
    nchunks = len(ranges)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(load_range, t) for t in tasks]
        for f in as_completed(futs):
            total += f.result()
            done += 1
            el = time.time() - start
            rate = total / el if el else 0
            print(f"  [{done:>2}/{nchunks} chunks done] {total:>12,} rows | {el:6.1f}s | {rate:>10,.0f} rows/sec",
                  flush=True)

    elapsed = time.time() - start
    rate = total / elapsed if elapsed else 0
    print(f"\n{'='*56}")
    print(f"Loaded {total:,} rows in {elapsed:.1f}s  ({rate:,.0f} rows/sec)")
    print(f"{'='*56}")


if __name__ == "__main__":
    main()
