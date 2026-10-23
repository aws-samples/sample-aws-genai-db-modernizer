#!/usr/bin/env python3
"""
CDC-cache initial-load benchmark (runs in-VPC on the app/EC2 host).

For each N in the size list:
  1. Ensure the source `orders` table has exactly N rows (fast INSERT...SELECT doubling).
  2. Flush the target Valkey cluster.
  3. Parallel bulk load: split PK range into `workers` chunks, each process reads
     its range and pipelines HSET to the target cluster.
  4. Verify the target key count == N and report load time + rows/sec.

Measures ONLY the loader (parallel SELECT -> Valkey), which is the initial-load
path. CDC is separate.
"""
import os, sys, time, math, argparse
from concurrent.futures import ProcessPoolExecutor, as_completed

DB = dict(
    host=os.environ.get("SOURCE_DB_HOST", ""),
    user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], database="Inventory",
)
TARGET_HOST = os.environ.get("VALKEY_HOST", "")
TARGET_PORT = 6379
TABLE = "orders"
PK = "id"


def mysql():
    import pymysql
    return pymysql.connect(**DB)


def vk_client():
    from valkey.cluster import ValkeyCluster
    return ValkeyCluster(host=TARGET_HOST, port=TARGET_PORT, ssl=True,
                         ssl_cert_reqs=None, decode_responses=False)


def target_count(vk):
    from valkey.cluster import ValkeyCluster
    try:
        res = vk.dbsize(target_nodes=ValkeyCluster.PRIMARIES)
        if isinstance(res, dict):
            return sum(res.values())
        return res
    except Exception:
        return vk.dbsize()


def ensure_rows(n):
    conn = mysql(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS orders "
                "(id INT PRIMARY KEY AUTO_INCREMENT, category VARCHAR(50), amount DECIMAL(10,2))")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM orders"); cnt = cur.fetchone()[0]
    if cnt > n:  # start clean if overshoot from a previous run
        cur.execute("TRUNCATE TABLE orders"); conn.commit(); cnt = 0
    if cnt == 0:
        cur.executemany("INSERT INTO orders(category,amount) VALUES(%s,%s)",
                        [("seed", 1.0)] * 1000)
        conn.commit(); cnt = 1000
    while cnt < n:
        add = min(cnt, n - cnt)
        cur.execute(f"INSERT INTO orders(category,amount) "
                    f"SELECT category,amount FROM orders LIMIT {add}")
        conn.commit(); cnt += add
    cur.close(); conn.close()
    return cnt


def chunk_ranges(lo, hi, k):
    lo, hi = int(lo), int(hi)
    size = max(1, math.ceil((hi - lo + 1) / k))
    out, s = [], lo
    while s <= hi:
        e = min(s + size - 1, hi); out.append((s, e)); s = e + 1
    return out


def load_range(lo, hi):
    import pymysql
    conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB)
    vk = vk_client()
    with conn.cursor() as c:
        c.execute(f"SELECT * FROM {TABLE} WHERE {PK} >= %s AND {PK} <= %s", (lo, hi))
        rows = c.fetchall()
    pipe = vk.pipeline(transaction=False); i = 0
    for row in rows:
        key = f"{TABLE}:{row[PK]}"
        pipe.hset(key, mapping={str(k): ("" if v is None else str(v)) for k, v in row.items()})
        i += 1
        if i % 1000 == 0:
            pipe.execute(); pipe = vk.pipeline(transaction=False)
    pipe.execute()
    conn.close()
    return len(rows)


def run(n, workers):
    print(f"\n===== N = {n:,} | workers={workers} =====", flush=True)
    t0 = time.time()
    ensure_rows(n)
    print(f"  source ready ({n:,} rows) in {time.time()-t0:.1f}s", flush=True)

    vk = vk_client(); vk.flushall(); time.sleep(1)

    conn = mysql(); cur = conn.cursor()
    cur.execute(f"SELECT MIN({PK}), MAX({PK}) FROM {TABLE}"); lo, hi = cur.fetchone()
    cur.close(); conn.close()
    ranges = chunk_ranges(lo, hi, workers)

    t1 = time.time()
    total = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(load_range, r[0], r[1]) for r in ranges]
        for f in as_completed(futs):
            total += f.result()
    load_s = time.time() - t1

    time.sleep(1)
    cnt = target_count(vk)
    rate = total / load_s if load_s else 0
    ok = "OK" if cnt == n else f"MISMATCH (target={cnt})"
    print(f"  LOADED {total:,} rows in {load_s:.1f}s  =>  {rate:,.0f} rows/sec  [{ok}]", flush=True)
    return (n, load_s, rate, cnt, ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000000,2000000,5000000")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    sizes = [int(x) for x in a.sizes.split(",")]
    results = [run(n, a.workers) for n in sizes]
    print("\n================ SUMMARY ================", flush=True)
    print(f"{'Rows':>12} {'Load(s)':>9} {'Rows/sec':>12} {'Verified':>10}", flush=True)
    for n, s, r, cnt, ok in results:
        print(f"{n:>12,} {s:>9.1f} {r:>12,.0f} {ok:>10}", flush=True)


if __name__ == "__main__":
    main()
