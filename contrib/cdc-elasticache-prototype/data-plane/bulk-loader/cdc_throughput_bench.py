#!/usr/bin/env python3
"""
End-to-end CDC throughput benchmark for the ECS pipeline:
  Aurora orders INSERTs -> Debezium -> broker stream -> Go sink -> target Valkey.

Generates N inserts into Inventory.orders (batched), then measures when the LAST
row (max PK) becomes visible in the target. Because CDC is ordered, the last PK
appearing means all N have been applied.

Reports:
  - gen time      : time to commit all N rows to Aurora
  - drain time    : time from source-done to last row visible in target
  - e2e wall time : gen start -> last row visible
  - CDC throughput: N / e2e wall time

Usage: python3 cdc_throughput_bench.py --n 1000000 --batch 1000
"""
import argparse, time, sys, os
import pymysql, valkey

DB = dict(host=os.environ.get("SOURCE_DB_HOST", ""),
          user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], database="Inventory", port=3306)
TGT_NODE = os.environ.get("VALKEY_HOST", "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--timeout", type=int, default=1200)
    a = ap.parse_args()

    conn = pymysql.connect(**DB); cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM orders")
    start_max = cur.fetchone()[0] or 0
    target_pk = start_max + a.n
    print(f"start_max={start_max} target_pk={target_pk} N={a.n} batch={a.batch}", flush=True)

    t = valkey.Valkey(host=TGT_NODE, port=6379, ssl=True, ssl_cert_reqs=None,
                      decode_responses=True, socket_timeout=10)

    # --- generate N inserts (batched multi-row INSERT) ---
    row = "('bench',1.00)"
    values = ",".join([row] * a.batch)
    stmt = f"INSERT INTO orders (category, amount) VALUES {values}"
    t0 = time.time()
    done = 0
    while done < a.n:
        this = min(a.batch, a.n - done)
        if this == a.batch:
            cur.execute(stmt)
        else:
            cur.execute(f"INSERT INTO orders (category, amount) VALUES {','.join([row]*this)}")
        done += this
        if done % 100000 == 0:
            conn.commit()
            print(f"  generated {done:,} ({done/(time.time()-t0):,.0f}/s src)", flush=True)
    conn.commit()
    t_gen = time.time()
    print(f"GEN DONE: {a.n:,} rows in {t_gen-t0:.1f}s ({a.n/(t_gen-t0):,.0f}/s into Aurora)", flush=True)

    # --- wait until the last PK is visible in the target ---
    last_key = f"orders:{target_pk}"
    while time.time() - t0 < a.timeout:
        if t.exists(last_key):
            break
        time.sleep(0.5)
    t_done = time.time()

    if not t.exists(last_key):
        print(f"TIMEOUT: {last_key} not visible after {a.timeout}s", flush=True); sys.exit(1)

    e2e = t_done - t0
    drain = t_done - t_gen
    print("──────── RESULTS ────────", flush=True)
    print(f"  rows                : {a.n:,}", flush=True)
    print(f"  gen into Aurora     : {t_gen-t0:.1f}s ({a.n/(t_gen-t0):,.0f}/s)", flush=True)
    print(f"  drain after src done: {drain:.1f}s", flush=True)
    print(f"  e2e wall (gen->tgt) : {e2e:.1f}s", flush=True)
    print(f"  CDC throughput      : {a.n/e2e:,.0f} rows/s end-to-end", flush=True)
    cur.close(); conn.close()

if __name__ == "__main__":
    main()
