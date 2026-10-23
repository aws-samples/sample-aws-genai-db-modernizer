#!/usr/bin/env python3
"""Denormalization throughput (sink-drain, isolated). Two phases:
  Phase 1 (embed): drain N order events; each does HMGET customers:{cid} + embed + HSET + SADD.
  Phase 2 (fan-out): drain FKMOD customer updates; each fans out to its ~N/FKMOD children.
Comparable basis to the plain (200K) / transforms (194K) sink-drain numbers."""
import os, subprocess, sys, time
import valkey
from valkey.cluster import ValkeyCluster

TARGET_HOST = sys.argv[1]
FKMOD = int(os.environ.get("FKMOD", "100000"))
N = int(os.environ.get("N", "1000000"))
GENENV = "/tmp/genenv/cdc-cache-go-genenv"
SINK = "/tmp/gotest/tools/go-sink/cdc-cache-go-sink"
TF = "/tmp/denorm-bench-transform.json"
ORDERS, CUST = "data:bench.orders", "data:bench.customers"

b = valkey.Valkey(host="localhost", port=6380, decode_responses=True)
t = ValkeyCluster(host=TARGET_HOST, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)


def seed_customers():
    pipe = t.pipeline(); n = 0
    for i in range(1, FKMOD + 1):
        pipe.hset(f"customers:{i}", mapping={"id": str(i), "name": f"N_{i}", "tier": "gold"})
        n += 1
        if n % 2000 == 0:
            pipe.execute(); pipe = t.pipeline()
    pipe.execute()
    print(f"seeded {FKMOD:,} customers", flush=True)


def gen(mode, stream, count):
    subprocess.run([GENENV], check=True, stdout=subprocess.DEVNULL,
                   env={**os.environ, "BROKER_ADDR": "localhost:6380", "STREAM": stream,
                        "COUNT": str(count), "MODE": mode, "FKMOD": str(FKMOD), "GEN_WORKERS": "8"})


def drained(streamlist, group, total):
    read = 0
    for s in streamlist:
        try:
            found = False
            for g in b.xinfo_groups(s):
                if g["name"] == group:
                    found = True
                    if int(g["pending"]) != 0:
                        return False
                    read += int(g.get("entries-read") or 0)
            if not found:
                return False
        except Exception:
            return False
    return read >= total


def drain(streams, count, label):
    group = f"denorm-{label}-{int(time.time())}"
    env = {**os.environ, "BROKER_HOST": "localhost", "BROKER_PORT": "6380",
           "TARGET_HOST": TARGET_HOST, "TARGET_PORT": "6379", "TARGET_SSL": "true", "TARGET_CLUSTER": "true",
           "STREAM_KEYS": streams, "EVENT_FORMAT": "envelope", "TRANSFORMS_FILE": TF,
           "CONSUMER_GROUP": group, "WORKERS": "16", "BATCH_SIZE": "500", "HEALTH_PORT": "0"}
    proc = subprocess.Popen([SINK], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time(); ok = False
    while time.time() - t0 < 600:
        if drained(streams.split(","), group, count):
            ok = True; break
        time.sleep(0.3)
    dt = time.time() - t0
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    return dt, ok


def main():
    print(f"FKMOD={FKMOD:,} N={N:,}", flush=True)
    seed_customers()

    gen("orders", ORDERS, N)
    dt, ok = drain(ORDERS + "," + CUST, N, "embed")
    print(f"PHASE1 embed: {N:,} order events in {dt:.1f}s = {N/dt:,.0f} events/sec {'' if ok else 'TIMEOUT'}", flush=True)
    sample = t.hgetall(f"orders:1")
    print(f"  sample orders:1 = {sample}", flush=True)

    gen("custupd", CUST, FKMOD)
    dt2, ok2 = drain(CUST + "," + ORDERS, FKMOD, "fanout")
    print(f"PHASE2 fan-out: {FKMOD:,} parent updates in {dt2:.1f}s = {FKMOD/dt2:,.0f} updates/sec, "
          f"~{N/dt2:,.0f} child-writes/sec {'' if ok2 else 'TIMEOUT'}", flush=True)
    print(f"  sample orders:1 = {t.hgetall('orders:1')}", flush=True)


if __name__ == "__main__":
    main()
