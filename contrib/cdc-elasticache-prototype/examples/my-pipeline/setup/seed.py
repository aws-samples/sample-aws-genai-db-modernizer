#!/usr/bin/env python3
"""
CDC-cache sample-workload seeder.

Creates the canonical schema (schema.sql) and populates it with
referentially-consistent test data, so the example pipeline in ../config.yaml
can be run end to end against a fresh database.

Connection comes from the SAME environment variables the bulk loader uses
SOURCE_DB_HOST / SOURCE_DB_PORT / SOURCE_DB_NAME, plus SOURCE_DB_USER / SOURCE_DB_PASSWORD,
which quickstart.sh reads from the DB secret at run time. No credentials are hardcoded.

Usage:
  ../../../quickstart.sh seed      # or, with the variables above set:
  python3 seed.py --users 1000 --merchants 200 --products 500

Row relationships produced:
  users.id                 <- customer_kyc.user_id
  customer_kyc.customer_id  <- customer_txn_aggregates.customer_id
"""
import argparse
import os
import random
import sys

try:
    import pymysql
except ImportError:
    sys.exit("pymysql is required: pip install pymysql")

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(HERE, "schema.sql")

CATEGORIES = ["restaurant", "electronics", "travel", "grocery", "fuel", "healthcare"]
COUNTRIES = ["US", "US", "US", "UK", "DE", "FR", "JP", "CA", "IN", "AU"]
PROD_CATS = ["Electronics", "Home", "Sports", "Clothing", "Books", "Toys"]
BRANDS = ["TechCo", "HomePro", "SportMax", "StyleX", "ReadMore", "FunTime"]


def connect():
    missing = [k for k in ("SOURCE_DB_HOST", "SOURCE_DB_USER", "SOURCE_DB_PASSWORD")
               if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)} (see ../.env.example)")
    # Connect without a database so we can create/select it ourselves.
    return pymysql.connect(
        host=os.environ["SOURCE_DB_HOST"],
        port=int(os.environ.get("SOURCE_DB_PORT", "3306")),
        user=os.environ["SOURCE_DB_USER"],
        password=os.environ["SOURCE_DB_PASSWORD"],
        autocommit=False,
    )


def create_schema(cur):
    db = os.environ.get("SOURCE_DB_NAME", "Inventory")
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    cur.execute(f"USE `{db}`")
    with open(SCHEMA_FILE) as f:
        raw = f.read()
    # Strip full-line comments FIRST so a comment sitting above a CREATE TABLE
    # doesn't get lumped into (and skip) that statement when we split on ';'.
    sql = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("--"))
    for stmt in sql.split(";"):
        s = stmt.strip()
        if not s:
            continue
        if s.upper().startswith(("CREATE DATABASE", "USE ")):
            continue  # DB is created/selected above
        cur.execute(s)


def batch_insert(cur, table, cols, rows, chunk=1000):
    if not rows:
        return
    placeholders = "(" + ",".join(["%s"] * len(cols)) + ")"
    col_list = ",".join(f"`{c}`" for c in cols)
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        values = ",".join([placeholders] * len(part))
        flat = [v for row in part for v in row]
        cur.execute(f"INSERT INTO `{table}` ({col_list}) VALUES {values}", flat)


def seed(conn, n_users, n_merchants, n_products):
    cur = conn.cursor()

    print("[1/6] Creating schema...")
    create_schema(cur)
    conn.commit()

    print(f"[2/6] users x{n_users}...")
    users = [(f"User {i}", f"user{i}@example.com",
              random.choices(["active", "inactive"], weights=[9, 1])[0])
             for i in range(1, n_users + 1)]
    batch_insert(cur, "users", ["name", "email", "status"], users)
    conn.commit()

    print(f"[3/6] merchants x{n_merchants}...")
    merchants = [(f"Merchant {i}", random.choice(CATEGORIES), random.choice(COUNTRIES),
                  random.choices(["active", "suspended"], weights=[9, 1])[0],
                  random.choice(["low", "low", "medium", "high"]),
                  round(random.uniform(0.01, 0.05), 4))
                 for i in range(1, n_merchants + 1)]
    batch_insert(cur, "merchants",
                 ["name", "category", "country", "status", "risk_tier", "fee_rate"], merchants)
    conn.commit()

    print(f"[4/6] products x{n_products}...")
    products = [(f"SKU-{100000 + i}",
                 f"{random.choice(BRANDS)} {random.choice(['Pro', 'Plus', 'Lite', 'Max'])}",
                 random.choice(PROD_CATS), round(random.uniform(5, 2000), 2),
                 random.choices(["active", "inactive", "discontinued"], weights=[8, 1, 1])[0],
                 random.randint(0, 5000))
                for i in range(1, n_products + 1)]
    batch_insert(cur, "products",
                 ["sku", "name", "category", "price", "status", "stock_count"], products)
    conn.commit()

    # customer_kyc: one KYC record per user (customer_id auto-increments 1..n_users)
    print(f"[5/6] customer_kyc x{n_users}...")
    kyc = [(uid, f"User {uid}", random.choice(COUNTRIES),
            random.choices(["verified", "pending", "rejected"], weights=[8, 1, 1])[0])
           for uid in range(1, n_users + 1)]
    batch_insert(cur, "customer_kyc", ["user_id", "full_name", "country", "kyc_status"], kyc)
    conn.commit()

    # customer_txn_aggregates: one row per customer (customer_id 1..n_users)
    print(f"[6/6] customer_txn_aggregates x{n_users}...")
    aggs = []
    for cid in range(1, n_users + 1):
        txn_count = random.randint(0, 500)
        total = round(random.uniform(0, 250000), 2)
        avg = round(total / txn_count, 2) if txn_count else 0.0
        aggs.append((cid, "2026Q1", txn_count, total, avg,
                     round(random.uniform(0, 20000), 2),
                     random.randint(0, 30), random.randint(0, 50)))
    batch_insert(cur, "customer_txn_aggregates",
                 ["customer_id", "period", "txn_count", "total_amount", "avg_txn_amount",
                  "max_single_txn", "failed_txn_count", "cross_border_count"], aggs)
    conn.commit()

    # Summary
    print("\n" + "=" * 48)
    for t in ("users", "merchants", "products", "customer_kyc", "customer_txn_aggregates"):
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        print(f"  {t:<28} {cur.fetchone()[0]:>10,} rows")
    print("=" * 48)
    cur.close()


def main():
    ap = argparse.ArgumentParser(description="Seed the CDC-cache sample workload")
    ap.add_argument("--users", type=int, default=1000, help="number of users / customers")
    ap.add_argument("--merchants", type=int, default=200)
    ap.add_argument("--products", type=int, default=500)
    args = ap.parse_args()

    conn = connect()
    try:
        seed(conn, args.users, args.merchants, args.products)
        print("\nDone. Next: run the bulk loader or deploy the pipeline (see PROTOTYPE_README.md).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
