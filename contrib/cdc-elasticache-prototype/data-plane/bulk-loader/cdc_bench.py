#!/usr/bin/env python3
"""CDC throughput benchmark: generate N inserts into Aurora orders and measure
end-to-end propagation through Debezium -> broker -> sink -> target Valkey."""
import os, time, sys
import pymysql
from valkey.cluster import ValkeyCluster

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
BATCH = 5000
DB = dict(host=os.environ.get("SOURCE_DB_HOST", ""),
          user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], database="Inventory", port=3306)
TGT = os.environ.get("VALKEY_HOST", "")


def dbsize(t):
    r = t.dbsize(target_nodes=ValkeyCluster.PRIMARIES)
    return sum(r.values()) if isinstance(r, dict) else r


def main():
    t = ValkeyCluster(host=TGT, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
    c0 = dbsize(t)
    target_total = c0 + N
    print(f"start dbsize={c0:,} target={target_total:,} N={N:,}", flush=True)

    conn = pymysql.connect(**DB); cur = conn.cursor()
    rows = [("cdcbench", 1)] * BATCH
    t0 = time.time()
    done = 0
    while done < N:
        cur.executemany("INSERT INTO orders (category, amount) VALUES (%s,%s)", rows)
        conn.commit()
        done += BATCH
    tgen = time.time() - t0
    print(f"generated {N:,} inserts in {tgen:.1f}s ({N/tgen:,.0f} rows/sec into Aurora)", flush=True)

    # poll target until it reflects all N new keys
    last = c0
    stall = 0
    while True:
        cur_size = dbsize(t)
        el = time.time() - t0
        if cur_size >= target_total:
            print(f"COMPLETE: dbsize={cur_size:,} at {el:.1f}s", flush=True)
            print(f"=== CDC end-to-end: {N:,} events in {el:.1f}s = {N/el:,.0f} events/sec ===", flush=True)
            break
        if cur_size == last:
            stall += 1
        else:
            stall = 0
        last = cur_size
        if el > 900:
            print(f"TIMEOUT at {el:.0f}s dbsize={cur_size:,} (got {cur_size-c0:,}/{N:,})", flush=True)
            break
        if stall > 60:  # ~2min no progress
            print(f"STALLED at {el:.0f}s dbsize={cur_size:,} (got {cur_size-c0:,}/{N:,})", flush=True)
            break
        print(f"  [{el:.0f}s] dbsize={cur_size:,} progress={cur_size-c0:,}/{N:,} rate={(cur_size-c0)/el:,.0f}/s", flush=True)
        time.sleep(2)
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
