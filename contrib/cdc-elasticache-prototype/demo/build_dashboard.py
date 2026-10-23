#!/usr/bin/env python3
"""Generate the CDC-cache CloudWatch dashboard body and write it to cdc_cache_dashboard.json.
Create/update it with:
  aws cloudwatch put-dashboard --dashboard-name CDC-cache \
    --dashboard-body file://cdc_cache_dashboard.json --region "$AWS_REGION"
Resource names default to the cdc-cache-demo-* deployment; override via env if needed.
"""
import json, os

REGION = os.environ["AWS_REGION"]
CLUSTER = os.environ.get("ECS_CLUSTER", "cdc-cache-demo-cluster")
DBZ = os.environ.get("DBZ_SVC", "cdc-cache-demo-cdc-debezium")
SINK = os.environ.get("SINK_SVC", "cdc-cache-demo-cdc-sink")
TGT = os.environ.get("TGT_NODE", "cdc-cache-demo-valkey-0001-001")
BRK = os.environ.get("BRK_NODE", "cdc-cache-demo-broker-cache-001")
SRC = os.environ.get("SRC_CLUSTER", "cdc-cache-demo-source")

EC = "AWS/ElastiCache"; ECS = "AWS/ECS"; CI = "ECS/ContainerInsights"; RDS = "AWS/RDS"


def w(x, y, width, height, title, metrics, stat="Average", period=60, ptype="timeSeries", ymax=None):
    props = {"title": title, "region": REGION, "period": period, "stat": stat,
             "view": ptype, "metrics": metrics}
    if ymax is not None:
        props["yAxis"] = {"left": {"min": 0, "max": ymax}}
    return {"type": "metric", "x": x, "y": y, "width": width, "height": height, "properties": props}


widgets = [
    # Row 1 - the money row: what is flowing through the pipeline
    w(0, 0, 8, 6, "Debezium -> Broker ingest (events/min)",
      [[EC, "SetTypeCmds", "CacheClusterId", BRK, {"label": "broker XADDs (ingest)"}]], stat="Sum"),
    w(8, 0, 8, 6, "Sink -> Target writes/reads (ops/min)",
      [[EC, "SetTypeCmds", "CacheClusterId", TGT, {"label": "target writes (HSET)"}],
       [EC, "GetTypeCmds", "CacheClusterId", TGT, {"label": "target reads"}]], stat="Sum"),
    w(16, 0, 8, 6, "Cached keys (target CurrItems)",
      [[EC, "CurrItems", "CacheClusterId", TGT, {"label": "target keys"}]], stat="Average"),

    # Row 2 - broker health (backlog / memory) + target memory
    w(0, 6, 8, 6, "Broker backlog (CurrItems) + memory %",
      [[EC, "CurrItems", "CacheClusterId", BRK, {"label": "broker stream depth"}],
       [EC, "DatabaseMemoryUsagePercentage", "CacheClusterId", BRK, {"label": "broker mem %", "yAxis": "right"}]]),
    w(8, 6, 8, 6, "Target memory % + engine CPU %",
      [[EC, "DatabaseMemoryUsagePercentage", "CacheClusterId", TGT, {"label": "target mem %"}],
       [EC, "EngineCPUUtilization", "CacheClusterId", TGT, {"label": "target engine CPU %"}]], ymax=100),
    w(16, 6, 8, 6, "Target replication lag (s)",
      [[EC, "ReplicationLag", "CacheClusterId", TGT, {"label": "repl lag"}]]),

    # Row 3 - compute (Debezium + sink)
    w(0, 12, 8, 6, "Debezium ECS CPU % / Mem %",
      [[ECS, "CPUUtilization", "ClusterName", CLUSTER, "ServiceName", DBZ, {"label": "cpu %"}],
       [ECS, "MemoryUtilization", "ClusterName", CLUSTER, "ServiceName", DBZ, {"label": "mem %"}]], ymax=100),
    w(8, 12, 8, 6, "Sink ECS CPU % / Mem %",
      [[ECS, "CPUUtilization", "ClusterName", CLUSTER, "ServiceName", SINK, {"label": "cpu %"}],
       [ECS, "MemoryUtilization", "ClusterName", CLUSTER, "ServiceName", SINK, {"label": "mem %"}]], ymax=100),
    w(16, 12, 8, 6, "Running task count (Debezium / Sink)",
      [[CI, "RunningTaskCount", "ClusterName", CLUSTER, "ServiceName", DBZ, {"label": "debezium tasks"}],
       [CI, "RunningTaskCount", "ClusterName", CLUSTER, "ServiceName", SINK, {"label": "sink tasks"}]]),

    # Row 4 - source Aurora
    w(0, 18, 8, 6, "Aurora source - ACU utilization %",
      [[RDS, "ACUUtilization", "DBClusterIdentifier", SRC, {"label": "ACU %"}]]),
    w(8, 18, 8, 6, "Aurora source - write throughput (bytes/s)",
      [[RDS, "WriteThroughput", "DBClusterIdentifier", SRC, {"label": "write B/s"}]]),
    w(16, 18, 8, 6, "Aurora source - connections",
      [[RDS, "DatabaseConnections", "DBClusterIdentifier", SRC, {"label": "connections"}]]),
]

body = {"widgets": widgets}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdc_cache_dashboard.json")
json.dump(body, open(out, "w"), indent=2)
print("wrote", out)
