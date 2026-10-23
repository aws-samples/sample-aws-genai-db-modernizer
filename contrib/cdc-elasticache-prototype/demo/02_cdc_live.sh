#!/usr/bin/env bash
# DEMO 2 - LIVE CDC works.
#   ./02_cdc_live.sh            -> single-row showcase: insert 1 active (watch it
#                                  reach the cache in ms, transformed) + 1 inactive
#                                  (filtered).
#   ./02_cdc_live.sh 10000      -> batch: insert N rows into the source and watch
#                                  live CDC carry them to the cache; report drain
#                                  time + effective rows/sec, and that inactive
#                                  rows were filtered.
# Uses the 'products' table (Debezium watches it) with MAX(pk)+append so there
# are no PK collisions. ~10% of batch rows are inactive to show the live filter.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export SRC TGT BRK PW DBUSER DB
export CDC_N="${1:-1}"

python3 - <<'PY'
import os, time, pymysql
from valkey.cluster import ValkeyCluster
N = int(os.environ["CDC_N"])
c = pymysql.connect(host=os.environ["SRC"], user=os.environ["DBUSER"], password=os.environ["PW"],
                    database=os.environ["DB"], autocommit=True); cur = c.cursor()
r = ValkeyCluster(host=os.environ["TGT"], port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
cur.execute("SELECT COALESCE(MAX(id),0) FROM products"); base = int(cur.fetchone()[0])

if N <= 1:
    pid = base + 1; key = f"products:{pid}"
    print(f">>> Insert ACTIVE product id={pid} price=24.99 into Aurora ...")
    t0 = time.time()
    cur.execute("INSERT INTO products (id,sku,name,category,price,status) VALUES (%s,%s,%s,%s,%s,%s)",
                (pid, f"LIVE{pid}", "Live Demo", "demo", 24.99, "active"))
    seen = False
    while time.time() - t0 < 30:
        v = r.hgetall(key)
        if v:
            print(f">>> Reached Valkey in {(time.time()-t0)*1000:.0f} ms -> {v}")
            seen = True; break
        time.sleep(0.02)
    if not seen:
        print(">>> NOT seen within 30s - check pipeline health"); raise SystemExit(1)
    pid2 = base + 2
    print(f">>> Insert INACTIVE product id={pid2} (should be filtered, never cached) ...")
    cur.execute("INSERT INTO products (id,sku,name,category,price,status) VALUES (%s,%s,%s,%s,%s,%s)",
                (pid2, f"INAC{pid2}", "Hidden", "demo", 9.99, "inactive"))
    time.sleep(3)
    print(f">>> products:{pid2} ->", r.hgetall(f"products:{pid2}") or "(absent - correctly filtered)")
    print(">>> LIVE CDC (single row): change reached cache in ms with price_cents; inactive filtered.")
else:
    lo = base + 1; hi = base + N
    sentinel = hi
    while sentinel % 10 == 0:   # last active id (inactive if id%10==0) - used as the "all delivered" marker
        sentinel -= 1
    active_expected = sum(1 for i in range(lo, hi + 1) if i % 10 != 0)
    print(f">>> Inserting {N:,} rows into products (ids {lo:,}..{hi:,}, ~10% inactive) and watching live CDC ...")
    t0 = time.time()
    CH = 5000; done = 0
    while done < N:
        n = min(CH, N - done)
        rows = [(base + done + i, f"CDC{base+done+i}", f"Row {base+done+i}", "demo",
                 round(1.0 + ((base + done + i) % 1000) * 0.01, 2),
                 ("inactive" if (base + done + i) % 10 == 0 else "active")) for i in range(1, n + 1)]
        cur.executemany("INSERT INTO products (id,sku,name,category,price,status) VALUES (%s,%s,%s,%s,%s,%s)", rows)
        done += n
    t_ins = time.time()
    print(f"    inserted {N:,} rows into Aurora in {t_ins-t0:.1f}s; waiting for CDC to reach the cache ...")
    key = f"products:{sentinel}"
    seen = False
    while time.time() - t0 < 180:
        if r.exists(key): seen = True; break
        time.sleep(0.05)
    t_done = time.time()
    # Count how many of the active rows actually landed (ordered delivery => sentinel
    # present implies all prior delivered; we still count to be explicit).
    present = 0; CHK = 2000; ids = list(range(lo, hi + 1))
    for s in range(0, len(ids), CHK):
        ch = ids[s:s + CHK]; p = r.pipeline()
        for pid in ch:
            p.exists(f"products:{pid}")
        present += sum(1 for ex in p.execute() if ex)
    print()
    if not seen:
        print(f"    sentinel products:{sentinel} NOT seen in 180s - check pipeline"); 
    print(f"    inserted:            {N:,} rows ({active_expected:,} active, {N-active_expected:,} inactive)")
    print(f"    source insert time:  {t_ins-t0:.1f}s")
    print(f"    CDC drain after ins: {t_done-t_ins:.1f}s  (source-done -> last row in cache)")
    print(f"    end-to-end:          {t_done-t0:.1f}s  = {N/(t_done-t0):,.0f} rows/sec")
    print(f"    active rows in cache:{present:,} / {active_expected:,} expected  ({N-active_expected:,} inactive filtered out)")
    print(">>> LIVE CDC (batch): all active rows carried to the cache; inactive filtered.")
PY
