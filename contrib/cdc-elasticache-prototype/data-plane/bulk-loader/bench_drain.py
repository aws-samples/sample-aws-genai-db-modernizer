#!/usr/bin/env python3
"""Sink-drain benchmark: pre-load a broker stream with N flat events, then time
the sink draining them into the target. Runs plain vs with-transforms for
1M/2M/5M. Excludes Aurora/Debezium (measures sink capacity)."""
import os, subprocess, sys, time
import valkey

TARGET_HOST = sys.argv[1]                       # clustercfg host of the 6-shard target
STREAM = "data:bench.customers"
GEN = "/tmp/gogen/cdc-cache-go-gen"
SINK = "/tmp/gotest/tools/go-sink/cdc-cache-go-sink"
TF = "/tmp/bench-transform.json"
b = valkey.Valkey(host="localhost", port=6380, decode_responses=True)


def preload(n):
    subprocess.run([GEN], check=True, stdout=subprocess.DEVNULL,
                   env={**os.environ, "BROKER_ADDR": "localhost:6380",
                        "STREAM": STREAM, "COUNT": str(n), "GEN_WORKERS": "8"})


def group_done(group, n):
    try:
        for g in b.xinfo_groups(STREAM):
            if g["name"] == group:
                return int(g.get("lag") or 0) == 0 and int(g["pending"]) == 0 and int(g.get("entries-read") or 0) >= n
    except Exception:
        return False
    return False


def drain(n, mode):
    group = f"bench-{mode}-{n}-{int(time.time())}"
    env = {**os.environ,
           "BROKER_HOST": "localhost", "BROKER_PORT": "6380",
           "TARGET_HOST": TARGET_HOST, "TARGET_PORT": "6379",
           "TARGET_SSL": "true", "TARGET_CLUSTER": "true",
           "STREAM_KEY": STREAM, "STREAM_TABLE": "customers",
           "EVENT_FORMAT": "flat", "CONSUMER_GROUP": group,
           "WORKERS": "16", "BATCH_SIZE": "20000", "HEALTH_PORT": "0"}
    if mode == "tf":
        env["TRANSFORMS_FILE"] = TF
    proc = subprocess.Popen([SINK], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    ok = False
    while time.time() - t0 < 300:
        if group_done(group, n):
            ok = True
            break
        time.sleep(0.2)
    dt = time.time() - t0
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return dt, ok


def main():
    print("mode,events,seconds,events_per_sec", flush=True)
    for n in (1_000_000, 2_000_000, 5_000_000):
        for mode in ("plain", "tf"):
            preload(n)
            dt, ok = drain(n, mode)
            rate = n / dt if dt > 0 else 0
            print(f"{mode},{n},{dt:.2f},{rate:,.0f}{'' if ok else ' (TIMEOUT)'}", flush=True)


if __name__ == "__main__":
    main()
