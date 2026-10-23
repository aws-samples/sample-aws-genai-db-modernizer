#!/usr/bin/env python3
"""Max sustained end-to-end CDC: hammer Aurora with parallel batched inserts and
measure how fast changes propagate all the way to the target Valkey.
Bottleneck is the source (Aurora binlog) + Debezium ingest, not the sink."""
import os, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
import pymysql
from valkey.cluster import ValkeyCluster

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 24
BATCH = 2000
DB = dict(host=os.environ.get("SOURCE_DB_HOST", ""),
          user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], database="Inventory", port=3306)
TGT = os.environ.get("VALKEY_HOST", "")


def insert_share(share):
    conn = pymysql.connect(**DB); cur = conn.cursor()
    rows = [("cdcmax", 1)] * BATCH
    done = 0
    while done < share:
        cur.executemany("INSERT INTO orders (category, amount) VALUES (%s,%s)", rows)
        conn.commit()
        done += BATCH
    cur.close(); conn.close()
    return done


def dbsize(t):
    r = t.dbsize(target_nodes=ValkeyCluster.PRIMARIES)
    return sum(r.values()) if isinstance(r, dict) else r


def main():
    t = ValkeyCluster(host=TGT, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
    c0 = dbsize(t)
    target_total = c0 + N
    per = N // WORKERS
    print(f"start dbsize={c0:,} N={N:,} workers={WORKERS}", flush=True)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(insert_share, per) for _ in range(WORKERS)]
        for f in as_completed(futs):
            f.result()
    tgen = time.time() - t0
    print(f"Aurora ingest: {N:,} rows in {tgen:.1f}s = {N/tgen:,.0f} rows/sec (source production)", flush=True)

    # poll target until pipeline catches up
    while True:
        cur = dbsize(t); el = time.time() - t0
        if cur >= target_total:
            print(f"COMPLETE end-to-end at {el:.1f}s", flush=True)
            print(f"=== END-TO-END CDC: {N:,} in {el:.1f}s = {N/el:,.0f} events/sec ===", flush=True)
            break
        if el > 900:
            print(f"TIMEOUT {el:.0f}s got {cur-c0:,}/{N:,}", flush=True); break
        print(f"  [{el:.0f}s] progress={cur-c0:,}/{N:,} rate={(cur-c0)/el:,.0f}/s", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
