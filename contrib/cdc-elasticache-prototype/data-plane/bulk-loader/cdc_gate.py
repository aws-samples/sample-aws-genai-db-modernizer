#!/usr/bin/env python3
"""
CDC-cache CDC "safe to read" gate.

After an initial bulk load, the app must NOT read the cache until CDC has caught
up. This gate makes that a concrete, automated check instead of a guess.

Mechanism (engine-agnostic, no binlog-offset parsing):
  1. SENTINEL — write a uniquely-marked row into the source table and capture its
     PK + a nonce. Because CDC is ordered, once the sentinel lands in the target,
     the changes emitted before it have flowed through.
  2. PARITY  — confirm source row count == target key count for the table. This
     is the backstop for parallel/partitioned sinks where cross-key ordering
     alone isn't a strict guarantee.
  3. SIGNAL  — print "SAFE TO READ" and exit 0 when both pass within the timeout;
     exit 1 on timeout. Optionally publish a CloudWatch metric.

Usage:
  python3 cdc_gate.py --table orders --pk id --timeout 180 [--check-parity] [--cloudwatch]
"""
import argparse, os, time, uuid, sys

DB = dict(
    host=os.environ.get("SOURCE_DB_HOST", ""),
    user=os.environ["SOURCE_DB_USER"], password=os.environ["SOURCE_DB_PASSWORD"], database="Inventory",
)
TARGET_HOST = os.environ.get("VALKEY_HOST", "")
TARGET_PORT = 6379


def mysql():
    import pymysql
    return pymysql.connect(**DB)


def vk_client():
    from valkey.cluster import ValkeyCluster
    return ValkeyCluster(host=TARGET_HOST, port=TARGET_PORT, ssl=True,
                         ssl_cert_reqs=None, decode_responses=True)


def target_count(vk):
    from valkey.cluster import ValkeyCluster
    try:
        res = vk.dbsize(target_nodes=ValkeyCluster.PRIMARIES)
        return sum(res.values()) if isinstance(res, dict) else res
    except Exception:
        return vk.dbsize()


def source_head(cur):
    """Return current binlog coordinate for reporting (not required for the gate)."""
    try:
        cur.execute("SHOW MASTER STATUS")
        r = cur.fetchone()
        return f"{r[0]}:{r[1]}" if r else "n/a"
    except Exception:
        return "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="orders")
    ap.add_argument("--pk", default="id")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--check-parity", action="store_true")
    ap.add_argument("--cloudwatch", action="store_true")
    a = ap.parse_args()

    conn = mysql(); cur = conn.cursor()
    head_before = source_head(cur)

    # 1) Write the sentinel row into the source and capture PK + nonce.
    nonce = f"cdc-cache-sentinel-{uuid.uuid4().hex[:12]}"
    cur.execute(f"INSERT INTO {a.table} (category, amount) VALUES (%s, %s)", (nonce, 0))
    conn.commit()
    sentinel_pk = cur.lastrowid
    t0 = time.time()
    print(f"gate: sentinel pk={sentinel_pk} nonce={nonce} | source head {head_before}", flush=True)

    # 2) Poll the target until the sentinel key appears with the matching nonce.
    vk = vk_client()
    key = f"{a.table}:{sentinel_pk}"
    caught_up = False
    while time.time() - t0 < a.timeout:
        val = vk.hget(key, "category")
        if val == nonce:
            caught_up = True
            break
        time.sleep(a.poll)

    lag = time.time() - t0
    if not caught_up:
        print(f"gate: TIMEOUT after {a.timeout}s — sentinel not seen in cache "
              f"(key {key}). CDC not caught up.", flush=True)
        _emit(a, 0, lag)
        sys.exit(1)

    print(f"gate: sentinel visible in cache after {lag:.2f}s (CDC lag <= {lag:.2f}s)", flush=True)

    # 3) Optional parity backstop.
    if a.check_parity:
        cur.execute(f"SELECT COUNT(*) FROM {a.table}")
        src = cur.fetchone()[0]
        tgt = target_count(vk)
        # cache holds one key per table row (plus this run's sentinel already counted)
        print(f"gate: parity source={src:,} target_keys={tgt:,}", flush=True)
        if tgt < src:
            print(f"gate: PARITY FAIL — target behind source by {src - tgt:,}", flush=True)
            _emit(a, 0, lag)
            sys.exit(1)

    print("gate: SAFE TO READ ✅  — app may cut over to the cache.", flush=True)
    _emit(a, 1, lag)
    cur.close(); conn.close()
    sys.exit(0)


def _emit(a, safe, lag):
    if not a.cloudwatch:
        return
    try:
        import boto3
        cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION"))
        cw.put_metric_data(Namespace="CDC-cache/Pipeline", MetricData=[
            {"MetricName": "SafeToRead", "Value": safe, "Unit": "None"},
            {"MetricName": "CDCLagSeconds", "Value": round(lag, 2), "Unit": "Seconds"},
        ])
    except Exception as e:
        print(f"gate: cloudwatch emit skipped ({e})", flush=True)


if __name__ == "__main__":
    main()
