#!/usr/bin/env python3
"""
CDC-cache live observability panel — makes every CDC stage visible in real time.

Run this from an in-VPC host (EC2 / CloudShell / Cloud9) so it can reach the
private Aurora + Valkey endpoints. It answers, live, the four questions an
audience always asks about a CDC pipeline:

  STAGE 1  Did Debezium read the binlog and write to the broker?
             -> broker stream `entries-added` climbs as changes arrive.
  STAGE 2  Did the sink worker consume from the broker?
             -> consumer-group `entries-read` climbs; `lag` ~ 0 means caught up.
  STAGE 3  Did the sink write to the target Valkey?
             -> target key count climbs (and the sink /metrics `writes` counter).
  STAGE 4  Is anything being dropped / retried?
             -> sink /metrics `dlq` and `retried` (0 dlq = nothing dead-lettered).

`lag = entries-added - entries-read` is the single best "is the pipeline keeping
up" number. Watch the deltas (in parentheses) march across the stages when you
insert rows in another terminal — that IS the end-to-end flow, visible.

Two modes:
  (default)   live panel, refreshes every INTERVAL seconds
  --trace     inject one sentinel row and report its per-stage arrival + latency

Config via env (all optional except SRC/PW for Aurora + trace):
  BRK   broker host (cluster-mode-disabled Valkey)         [required]
  TGT   target host (cluster-mode-enabled Valkey, TLS)     [required]
  SRC   Aurora endpoint     PW  Aurora password            [needed for source count + --trace]
  DB=Inventory  TABLE=products  STREAM=data:cdc-cache.Inventory.products  GROUP=cdc-cache-sink
  INTERVAL=1.5  SINK_METRICS_URL=http://<task-ip>:8081/metrics  (optional; adds writes/dlq/retried)

Examples:
  BRK=... TGT=... SRC=... PW=... python3 observe.py
  BRK=... TGT=... SRC=... PW=... python3 observe.py --trace
"""
import os
import sys
import time
import json
import urllib.request

import valkey
from valkey.cluster import ValkeyCluster

BRK = os.environ.get("BRK")
TGT = os.environ.get("TGT")
SRC = os.environ.get("SRC")
PW = os.environ.get("PW")
DB = os.environ.get("DB", "Inventory")
TABLE = os.environ.get("TABLE", "products")
STREAM = os.environ.get("STREAM", "data:cdc-cache.Inventory.%s" % TABLE if os.environ.get("TABLE") else "data:cdc-cache.Inventory.products")
GROUP = os.environ.get("GROUP", "cdc-cache-sink")
INTERVAL = float(os.environ.get("INTERVAL", "1.5"))
SINK_METRICS_URL = os.environ.get("SINK_METRICS_URL")

if not BRK or not TGT:
    sys.exit("set BRK (broker host) and TGT (target host) env vars")


def broker_client():
    return valkey.Valkey(host=BRK, port=6379, ssl=False, decode_responses=True, socket_timeout=5)


def target_client():
    return ValkeyCluster(host=TGT, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)


def mysql_conn():
    if not SRC or not PW:
        return None
    import pymysql
    return pymysql.connect(host=SRC, user=os.environ["DBUSER"],
                           password=PW, database=DB, autocommit=True)


def target_keys(r):
    try:
        d = r.dbsize()
        return sum(d.values()) if isinstance(d, dict) else d
    except Exception:
        return -1


def stream_added(b):
    try:
        return int(b.xinfo_stream(STREAM).get("entries-added", 0))
    except Exception:
        return 0


def group_stat(b):
    try:
        g = b.xinfo_groups(STREAM)[0]
        return int(g.get("entries-read") or 0), g.get("lag"), g.get("pending")
    except Exception:
        return 0, None, None


def sink_metrics():
    if not SINK_METRICS_URL:
        return None
    try:
        with urllib.request.urlopen(SINK_METRICS_URL, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fmt(n):
    return "-" if n is None else ("{:,}".format(n) if isinstance(n, int) else str(n))


def source_count(cur):
    if cur is None:
        return None
    try:
        cur.execute("SELECT COUNT(*) FROM %s" % TABLE)
        return int(cur.fetchone()[0])
    except Exception:
        return None


def live_panel():
    b = broker_client()
    r = target_client()
    conn = mysql_conn()
    cur = conn.cursor() if conn else None
    print("CDC-cache live pipeline — refresh %.1fs. Insert rows in another terminal and watch the deltas flow." % INTERVAL)
    print("stage1 Debezium->broker | stage2 sink consume | stage3 sink->target | stage4 reliability")
    print("-" * 108)
    prev = {}
    t0 = time.time()
    while True:
        src = source_count(cur)
        added = stream_added(b)
        read, lag, pend = group_stat(b)
        keys = target_keys(r)
        m = sink_metrics()

        def d(k, v):
            if v is None or not isinstance(v, int):
                return ""
            pv = prev.get(k)
            prev[k] = v
            return "" if pv is None else " (%+d)" % (v - pv)

        line = ("t+%4ds | src rows=%s%s | broker added=%s%s | sink read=%s%s lag=%s pend=%s | target keys=%s%s"
                % (int(time.time() - t0),
                   fmt(src), d("src", src),
                   fmt(added), d("added", added),
                   fmt(read), d("read", read), fmt(lag), fmt(pend),
                   fmt(keys), d("keys", keys)))
        if m:
            line += " | sink writes=%s dlq=%s retried=%s" % (
                fmt(m.get("writes")), fmt(m.get("dlq")), fmt(m.get("retried")))
        print(line, flush=True)
        time.sleep(INTERVAL)


def trace_one():
    import random
    conn = mysql_conn()
    if conn is None:
        sys.exit("--trace needs SRC + PW (Aurora) to inject a sentinel row")
    cur = conn.cursor()
    b = broker_client()
    r = target_client()
    pid = 999000000 + random.randint(0, 999999)
    key = "%s:%d" % (TABLE, pid)
    added0 = stream_added(b)
    read0, _, _ = group_stat(b)
    t0 = time.time()
    cur.execute("INSERT INTO %s (id,sku,name,category,price,status) VALUES (%%s,%%s,%%s,%%s,%%s,%%s)" % TABLE,
                (pid, "TRACE-%d" % pid, "trace", "demo", 4.2, "active"))
    print("injected %s at t0; watching it flow through the 4 stages ...\n" % key)
    st = {"broker": None, "consumed": None, "target": None}
    while time.time() - t0 < 60:
        if st["broker"] is None and stream_added(b) > added0:
            st["broker"] = time.time() - t0
            print("  STAGE1+2  Debezium read binlog -> wrote to broker stream   @ %6.0f ms" % (st["broker"] * 1000))
        rd, lag, _ = group_stat(b)
        if st["consumed"] is None and rd > read0:
            st["consumed"] = time.time() - t0
            print("  STAGE3    sink worker consumed the entry from the broker    @ %6.0f ms" % (st["consumed"] * 1000))
        if st["target"] is None and r.exists(key):
            st["target"] = time.time() - t0
            print("  STAGE4    sink wrote it to the target Valkey (%s) @ %6.0f ms" % (key, st["target"] * 1000))
            print("            -> %s" % r.hgetall(key))
            break
        time.sleep(0.02)
    if st["target"] is None:
        print("  NOT SEEN in target within 60s (check pipeline health)")
    else:
        print("\n  end-to-end commit -> readable in cache: %.0f ms" % (st["target"] * 1000))


if __name__ == "__main__":
    if "--trace" in sys.argv:
        trace_one()
    else:
        try:
            live_panel()
        except KeyboardInterrupt:
            print("\nbye")
