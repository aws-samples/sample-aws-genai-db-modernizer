#!/usr/bin/env python3
"""
Oracle demo seeder for the CDC-cache initial-load benchmark.

Creates a single wide-ish table and bulk-inserts N rows with a contiguous
integer primary key (1..N) so the parallel PK-range bulk loader can split it
cleanly. Uses python-oracledb in thin mode (no Oracle client needed).

Env (same contract as the loader):
  SOURCE_DB_HOST, SOURCE_DB_PORT (1521), SOURCE_DB_USER, SOURCE_DB_PASSWORD,
  SOURCE_DB_NAME  (Oracle service name, e.g. ORCL)

Usage:
  python3 seed_oracle.py --table CATALOG --pk ID --rows 100000 --batch 5000
"""
import argparse
import os
import time


def main():
    ap = argparse.ArgumentParser(description="CDC-cache Oracle demo seeder")
    ap.add_argument("--table", default="CATALOG")
    ap.add_argument("--pk", default="ID")
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()

    import oracledb

    conn = oracledb.connect(
        user=os.environ["SOURCE_DB_USER"],
        password=os.environ["SOURCE_DB_PASSWORD"],
        dsn=oracledb.makedsn(
            os.environ["SOURCE_DB_HOST"],
            int(os.environ.get("SOURCE_DB_PORT", 1521)),
            service_name=os.environ["SOURCE_DB_NAME"]),
    )
    t = args.table.upper()
    pk = args.pk.upper()
    cur = conn.cursor()

    # Drop + recreate so the run is repeatable.
    try:
        cur.execute(f'DROP TABLE "{t}"')
    except oracledb.DatabaseError:
        pass  # ORA-00942: table does not exist — fine on first run

    cur.execute(f'''
        CREATE TABLE "{t}" (
            "{pk}"        NUMBER PRIMARY KEY,
            "NAME"        VARCHAR2(120),
            "CATEGORY"    VARCHAR2(40),
            "PRICE"       NUMBER(10,2),
            "PRICE_CENTS" NUMBER(12),
            "STATUS"      VARCHAR2(16),
            "SKU"         VARCHAR2(40),
            "STOCK_COUNT" NUMBER,
            "UPDATED_AT"  VARCHAR2(32)
        )''')

    insert = (f'INSERT INTO "{t}" '
              f'("{pk}","NAME","CATEGORY","PRICE","PRICE_CENTS","STATUS","SKU","STOCK_COUNT","UPDATED_AT") '
              f'VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9)')

    cats = ["demo", "books", "toys", "food", "tech"]
    start = time.time()
    done = 0
    rows = []
    for i in range(1, args.rows + 1):
        price = (i % 10000) / 100.0
        status = "active" if (i % 10) else "inactive"
        rows.append((i, f"item-{i}", cats[i % len(cats)], price, int(price * 100),
                     status, f"SKU-{i}", i % 500, "2026-08-10T00:00:00Z"))
        if len(rows) >= args.batch:
            cur.executemany(insert, rows)
            conn.commit()
            done += len(rows)
            rows = []
            if done % 50000 == 0:
                el = time.time() - start
                print(f"  seeded {done:,}/{args.rows:,} ({done/el:,.0f} rows/s)", flush=True)
    if rows:
        cur.executemany(insert, rows)
        conn.commit()
        done += len(rows)

    el = time.time() - start
    print(f"Seeded {done:,} rows into {t} in {el:.1f}s ({done/el:,.0f} rows/s)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
