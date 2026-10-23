#!/usr/bin/env python3
"""Per-component live monitors for CDC-cache. Invoked by sink_monitor.sh /
debezium_monitor.sh / broker_monitor.sh (which set the env below).

Data sources, by design:
  - Broker/target counters (consumed, pending, lag, DLQ, ingest rate, target keys)
    come straight from Valkey in-VPC - always available, no AWS permissions.
  - CPU / memory / task-count come from CloudWatch via the AWS CLI (the EC2 role
    can read CloudWatch). If a CloudWatch value is unavailable it shows 'n/a'.

Env: MODE=sink|debezium|broker, BRK, TGT, STREAM, GROUP, ECS_CLUSTER, AWS_DEFAULT_REGION
"""
import os, sys, time, subprocess, datetime
import valkey
from valkey.cluster import ValkeyCluster

MODE = os.environ.get("MODE", "broker")
BRK = os.environ["BRK"]
TGT = os.environ.get("TGT", "")
STREAM = os.environ.get("STREAM", "data:cdc-cache.Inventory.products")
GROUP = os.environ.get("GROUP", "cdc-cache-sink")
DLQ = "dlq:" + STREAM
REGION = os.environ.get("AWS_DEFAULT_REGION")
ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "cdc-cache-demo-cluster")
SINK_SVC = os.environ.get("SINK_SVC", "cdc-cache-demo-cdc-sink")
DBZ_SVC = os.environ.get("DBZ_SVC", "cdc-cache-demo-cdc-debezium")
INTERVAL = float(os.environ.get("INTERVAL", "5"))

b = valkey.Valkey(host=BRK, port=6379, decode_responses=True, socket_timeout=5)
tgt = ValkeyCluster(host=TGT, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True) if TGT else None


def cw(ns, metric, dims, stat="Average"):
    """Latest CloudWatch datapoint over the last 10 min, via the AWS CLI."""
    end = datetime.datetime.utcnow(); start = end - datetime.timedelta(minutes=10)
    args = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", ns,
            "--metric-name", metric, "--dimensions"] + ["Name=%s,Value=%s" % (n, v) for n, v in dims] + \
           ["--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--period", "60", "--statistics", stat, "--region", REGION,
            "--query", "sort_by(Datapoints,&Timestamp)[-1].%s" % stat, "--output", "text"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=15).stdout.strip()
        return out if out and out != "None" else "n/a"
    except Exception:
        return "n/a"


def fnum(x):
    try: return "%.1f" % float(x)
    except Exception: return str(x)


def group_stats():
    try:
        g = b.xinfo_groups(STREAM)[0]
        return int(g.get("entries-read") or 0), g.get("pending"), g.get("lag"), g.get("consumers")
    except Exception:
        return 0, "n/a", "n/a", "n/a"


def active_consumers(idle_ms=30000):
    """Count consumers in the group that polled within idle_ms (currently-live
    sink workers). The raw group 'consumers' count includes stale consumers from
    prior task generations, so this is a better 'how many workers' number."""
    try:
        cs = b.xinfo_consumers(STREAM, GROUP)
        return sum(1 for c in cs if int(c.get("idle", 0)) < idle_ms)
    except Exception:
        return "n/a"


def stream_added():
    try: return int(b.xinfo_stream(STREAM).get("entries-added", 0))
    except Exception: return 0


def dlq_len():
    try: return b.xlen(DLQ)
    except Exception: return 0


def tgt_keys():
    if not tgt: return None
    try:
        d = tgt.dbsize(); return sum(d.values()) if isinstance(d, dict) else d
    except Exception: return None


def ecs_cpu_mem_tasks(svc):
    cpu = cw("AWS/ECS", "CPUUtilization", [("ClusterName", ECS_CLUSTER), ("ServiceName", svc)], "Average")
    mem = cw("AWS/ECS", "MemoryUtilization", [("ClusterName", ECS_CLUSTER), ("ServiceName", svc)], "Average")
    tasks = cw("ECS/ContainerInsights", "RunningTaskCount", [("ClusterName", ECS_CLUSTER), ("ServiceName", svc)], "Average")
    return cpu, mem, tasks


def loop(fn, title):
    print(title + "  (interval %.0fs, Ctrl-C to stop)" % INTERVAL)
    t0 = time.time()
    state = {}
    while True:
        try:
            fn(state, time.time() - t0)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("err", e)
        time.sleep(INTERVAL)


def sink_tick(s, el):
    read, pend, lag, cons = group_stats()
    keys = tgt_keys()
    rr = "" if "read" not in s else " (+%d/s)" % ((read - s["read"]) / INTERVAL)
    kk = ""
    if keys is not None and "keys" in s:
        kk = " (+%d/s)" % ((keys - s["keys"]) / INTERVAL)
    s["read"] = read
    if keys is not None: s["keys"] = keys
    cpu, mem, tasks = ecs_cpu_mem_tasks(SINK_SVC)
    workers = active_consumers()
    tasks_disp = ("tasks=%s " % tasks) if tasks != "n/a" else ""
    print("t+%4.0fs | %sactive workers=%s | consumed(entries-read)=%s%s | target keys=%s%s | cpu=%s%% mem=%s%%"
          % (el, tasks_disp, workers, format(read, ","), rr,
             (format(keys, ",") if keys is not None else "n/a"), kk, fnum(cpu), fnum(mem)), flush=True)


def dbz_tick(s, el):
    added = stream_added()
    rr = "" if "added" not in s else " (+%d/s)" % ((added - s["added"]) / INTERVAL)
    s["added"] = added
    cpu, mem, tasks = ecs_cpu_mem_tasks(DBZ_SVC)
    note = ""
    try:
        if cpu != "n/a" and float(cpu) >= 90: note = "  <-- near single-core ceiling"
    except Exception: pass
    print("t+%4.0fs | debezium tasks=%s | ingest(entries-added)=%s%s | cpu=%s%% mem=%s%%%s"
          % (el, tasks, format(added, ","), rr, fnum(cpu), fnum(mem), note), flush=True)


def broker_tick(s, el):
    read, pend, lag, cons = group_stats()
    added = stream_added()
    try: depth = b.xlen(STREAM)
    except Exception: depth = "n/a"
    dq = dlq_len()
    rr = "" if "read" not in s else " (+%d/s)" % ((read - s["read"]) / INTERVAL)
    s["read"] = read
    print("t+%4.0fs | added=%s | read=%s%s | pending=%s | lag=%s | consumers=%s | depth=%s | DLQ=%s"
          % (el, format(added, ","), format(read, ","), rr, pend, lag, cons, depth, dq), flush=True)


if __name__ == "__main__":
    if MODE == "sink":
        loop(sink_tick, "SINK monitor - workers / consumed / target writes / cpu / mem")
    elif MODE == "debezium":
        loop(dbz_tick, "DEBEZIUM monitor - ingest rate / cpu / mem (single-threaded log reader)")
    else:
        loop(broker_tick, "BROKER monitor - read / pending / lag / consumers / depth / DLQ")
