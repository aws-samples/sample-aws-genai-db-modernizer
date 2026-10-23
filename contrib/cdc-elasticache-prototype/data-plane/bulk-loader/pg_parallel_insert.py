#!/usr/bin/env python3
"""Parallel inserts into Aurora PostgreSQL orders. Two uses:
  --populate         : just insert N rows (to seed the initial-load benchmark)
  --measure <tgt>    : insert N rows AND time end-to-end propagation to target Valkey
Args: PGHOST N WORKERS [--measure TARGET_CLUSTERCFG]"""
import sys, os, time
from concurrent.futures import ProcessPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import execute_values

PGHOST = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 24
MEASURE = sys.argv[sys.argv.index("--measure") + 1] if "--measure" in sys.argv else None
BATCH = 2000
PG = dict(host=PGHOST, port=5432, user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], dbname="postgres")


def insert_share(share):
    conn = psycopg2.connect(**PG); conn.autocommit = False; cur = conn.cursor()
    rows = [("cdcpg", 1, (i % 100000) + 1) for i in range(BATCH)]
    done = 0
    while done < share:
        execute_values(cur, "INSERT INTO orders (category, amount, customer_id) VALUES %s", rows)
        conn.commit()
        done += BATCH
    cur.close(); conn.close()
    return done


def tgt_dbsize(t):
    from valkey.cluster import ValkeyCluster
    r = t.dbsize(target_nodes=ValkeyCluster.PRIMARIES)
    return sum(r.values()) if isinstance(r, dict) else r


def main():
    per = N // WORKERS
    t = None; c0 = 0
    if MEASURE:
        from valkey.cluster import ValkeyCluster
        t = ValkeyCluster(host=MEASURE, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
        c0 = tgt_dbsize(t)
        print(f"start target dbsize={c0:,}", flush=True)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(insert_share, per) for _ in range(WORKERS)]
        for f in as_completed(futs):
            f.result()
    tgen = time.time() - t0
    print(f"PG insert: {N:,} rows in {tgen:.1f}s = {N/tgen:,.0f} rows/sec", flush=True)

    if MEASURE:
        target_total = c0 + N
        while True:
            cur = tgt_dbsize(t); el = time.time() - t0
            if cur >= target_total:
                print(f"=== PG CDC end-to-end: {N:,} in {el:.1f}s = {N/el:,.0f} events/sec ===", flush=True)
                break
            if el > 900:
                print(f"TIMEOUT {el:.0f}s got {cur-c0:,}/{N:,}", flush=True); break
            print(f"  [{el:.0f}s] progress={cur-c0:,}/{N:,} rate={(cur-c0)/el:,.0f}/s", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
