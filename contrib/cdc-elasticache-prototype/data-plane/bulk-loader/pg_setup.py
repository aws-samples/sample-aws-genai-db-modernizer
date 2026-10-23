#!/usr/bin/env python3
"""Prepare Aurora PostgreSQL for CDC + benchmarks: schema, REPLICA IDENTITY,
publication, and replication grant."""
import os, sys, psycopg2

HOST = sys.argv[1]
conn = psycopg2.connect(host=HOST, port=5432, user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], dbname="postgres")
conn.autocommit = True
cur = conn.cursor()

cur.execute("SHOW wal_level")
print("wal_level =", cur.fetchone()[0])  # must be 'logical' for CDC

cur.execute("CREATE TABLE IF NOT EXISTS customers (id int PRIMARY KEY, name text, tier text)")
cur.execute("CREATE TABLE IF NOT EXISTS orders (id bigserial PRIMARY KEY, category text, amount numeric, customer_id int)")
cur.execute("ALTER TABLE orders REPLICA IDENTITY FULL")
cur.execute("ALTER TABLE customers REPLICA IDENTITY FULL")

cur.execute("DROP PUBLICATION IF EXISTS dbz_pub")
cur.execute("CREATE PUBLICATION dbz_pub FOR TABLE orders, customers")

# Debezium needs REPLICATION privilege; on Aurora PG grant the rds_replication role.
try:
    cur.execute("GRANT rds_replication TO postgres")
except Exception as e:
    print("grant rds_replication:", e)

cur.execute("SELECT count(*) FROM orders")
print("orders rows:", cur.fetchone()[0])
print("setup OK: tables + REPLICA IDENTITY FULL + publication dbz_pub")
cur.close(); conn.close()
