#!/usr/bin/env python3
"""
CDC-cache v3 Sink Worker — Final Architecture
Reads from: Broker Valkey (Streams, no TLS, internal)
Writes to:  Target Valkey (Hashes, TLS, ElastiCache)
Applies:    Row filters, computed fields, denormalization, column selection
"""
import os, json, time, signal, sys, threading, ast, operator as _op
import valkey

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════
BROKER_HOST = os.getenv("BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("BROKER_PORT", "6379"))
BROKER_SSL = os.getenv("BROKER_SSL", "false").lower() == "true"

TARGET_HOST = os.getenv("TARGET_HOST", "localhost")
TARGET_PORT = int(os.getenv("TARGET_PORT", "6379"))
TARGET_SSL = os.getenv("TARGET_SSL", "true").lower() == "true"

CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "cdc-cache-sink")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "worker-1")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
STREAM_PATTERN = os.getenv("STREAM_PATTERN", "data:*")
TRANSFORMS_FILE = os.getenv("TRANSFORMS_FILE", "/opt/cdc-cache/transforms.json")
CLOUDWATCH_ENABLED = os.getenv("CLOUDWATCH_ENABLED", "true").lower() == "true"
CLOUDWATCH_NAMESPACE = os.getenv("CLOUDWATCH_NAMESPACE", "CDC-cache/Pipeline")
CLOUDWATCH_INTERVAL = int(os.getenv("CLOUDWATCH_INTERVAL", "60"))
PIPELINE_NAME = os.getenv("PIPELINE_NAME", "cdc-cache-v3")

# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════
running = True
stats = {"writes": 0, "deletes": 0, "skips": 0, "errors": 0, "events": 0, "batches": 0}
ROW_FILTERS = []
COMPUTED_FIELDS = []
DENORMALIZE = []
COLUMN_SELECTION = {}
_transforms_mtime = 0

def signal_handler(sig, frame):
    global running
    running = False
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ═══════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════
def connect_broker():
    """Connect to Broker Valkey (cluster mode or standalone)."""
    kwargs = {"host": BROKER_HOST, "port": BROKER_PORT, "decode_responses": True,
              "socket_timeout": 5, "socket_connect_timeout": 5}
    if BROKER_SSL:
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = None
    # Try cluster mode first (for ElastiCache cluster mode enabled)
    try:
        client = valkey.cluster.ValkeyCluster(**kwargs)
        client.ping()
        print(f"  ✅ Broker connected (cluster): {BROKER_HOST}:{BROKER_PORT}")
        return client
    except Exception:
        pass
    client = valkey.Valkey(**kwargs)
    client.ping()
    print(f"  ✅ Broker connected (standalone): {BROKER_HOST}:{BROKER_PORT}")
    return client

def connect_target():
    """Connect to Target Valkey (ElastiCache, TLS, cluster mode)."""
    kwargs = {"host": TARGET_HOST, "port": TARGET_PORT, "decode_responses": True,
              "socket_timeout": 5, "socket_connect_timeout": 5}
    if TARGET_SSL:
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = None
    try:
        client = valkey.cluster.ValkeyCluster(**kwargs)
        client.ping()
        print(f"  ✅ Target connected (cluster): {TARGET_HOST}:{TARGET_PORT}")
        return client
    except Exception:
        client = valkey.Valkey(**kwargs)
        client.ping()
        print(f"  ✅ Target connected (standalone): {TARGET_HOST}:{TARGET_PORT}")
        return client

# ═══════════════════════════════════════════════════════════════
# Transforms
# ═══════════════════════════════════════════════════════════════
def load_transforms():
    global ROW_FILTERS, COMPUTED_FIELDS, DENORMALIZE, COLUMN_SELECTION, _transforms_mtime
    if not os.path.exists(TRANSFORMS_FILE):
        return
    try:
        mtime = os.path.getmtime(TRANSFORMS_FILE)
        if mtime <= _transforms_mtime:
            return
        with open(TRANSFORMS_FILE) as f:
            t = json.load(f)
        ROW_FILTERS = t.get("row_filters", [])
        COMPUTED_FIELDS = t.get("computed_fields", [])
        DENORMALIZE = t.get("denormalize", [])
        COLUMN_SELECTION = t.get("columns", {})
        _transforms_mtime = mtime
        print(f"  Transforms loaded: {len(ROW_FILTERS)} filters, {len(COMPUTED_FIELDS)} computed, {len(DENORMALIZE)} denorm")
    except Exception as e:
        print(f"  Transform load error: {e}")

_SAFE_OPS = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
             ast.Div: _op.truediv, ast.Mod: _op.mod, ast.USub: _op.neg}

def safe_eval(expr, variables):
    safe_vars = {}
    for k, v in variables.items():
        try:
            safe_vars[k] = float(v) if v is not None and str(v).replace('.','',1).replace('-','',1).isdigit() else 0
        except (ValueError, TypeError):
            safe_vars[k] = 0
    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        elif isinstance(node, ast.Constant): return node.value
        elif isinstance(node, ast.Name): return safe_vars.get(node.id, 0)
        elif isinstance(node, ast.BinOp): return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.UnaryOp): return _SAFE_OPS[type(node.op)](_eval(node.operand))
        return 0
    return _eval(ast.parse(expr, mode='eval'))

def apply_filter(row, table):
    if not ROW_FILTERS:
        return True
    for f in ROW_FILTERS:
        if f.get("table", "*") not in ("*", table):
            continue
        val = str(row.get(f.get("field", ""), ""))
        op = f.get("operator", "=")
        expected = f.get("value", "")
        if op == "=" and val != expected: return False
        elif op == "!=" and val == expected: return False
        elif op == "in" and val not in [v.strip() for v in expected.split(",")]: return False
    return True

def apply_computed(row, table):
    if not COMPUTED_FIELDS:
        return row
    for cf in COMPUTED_FIELDS:
        if cf.get("table", "*") not in ("*", table):
            continue
        try:
            row[cf["name"]] = safe_eval(cf["expression"], row)
        except Exception:
            pass
    return row

def apply_denormalize(target, row, table, op="write"):
    if not DENORMALIZE:
        return False
    handled = False
    for rule in DENORMALIZE:
        if rule.get("child_table") != table:
            continue
        parent_table = rule.get("parent_table", "")
        join_field = rule.get("join_field", "")
        fields = rule.get("fields", [])
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",") if f.strip()]
        prefix = rule.get("prefix", "")
        parent_pk = row.get(join_field)
        if not parent_pk:
            continue
        parent_key = f"{parent_table}:{parent_pk}"
        if op == "delete":
            for f in fields:
                target.hdel(parent_key, f"{prefix}{f}")
        else:
            merge = {}
            for f in fields:
                if f in row:
                    merge[f"{prefix}{f}"] = str(row[f]) if row[f] is not None else ""
            if merge:
                target.hset(parent_key, mapping=merge)
        handled = True
    return handled

def get_pk(row, table):
    for f in ["id", "customer_id", "merchant_id", "product_id", "order_id"]:
        if f in row:
            return row[f]
    return list(row.values())[0] if row else "unknown"

# ═══════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════
def main():
    global running
    print("=" * 60)
    print("  CDC-cache v3 Sink Worker (Final Architecture)")
    print("  Broker (streams) → Transform → Target (hashes)")
    print("=" * 60)
    print(f"  Broker: {BROKER_HOST}:{BROKER_PORT} (SSL={BROKER_SSL})")
    print(f"  Target: {TARGET_HOST}:{TARGET_PORT} (SSL={TARGET_SSL})")
    print()

    broker = connect_broker()
    target = connect_target()
    load_transforms()

    # Wait for streams to appear
    streams = {}
    while running and not streams:
        stream_keys = [k for k in broker.keys(STREAM_PATTERN)]
        if stream_keys:
            for sk in stream_keys:
                try:
                    broker.xgroup_create(sk, CONSUMER_GROUP, id="0", mkstream=True)
                except Exception:
                    pass
            streams = {sk: ">" for sk in stream_keys}
            print(f"  Consuming from {len(streams)} streams: {list(streams.keys())}")
        else:
            print("  Waiting for streams to appear...")
            time.sleep(2)

    print(f"  Batch size: {BATCH_SIZE}")
    print("  Running...")
    print("-" * 60)

    last_print = time.time()
    reload_counter = 0

    while running:
        try:
            # Hot-reload transforms every ~10s
            reload_counter += 1
            if reload_counter >= 20:
                reload_counter = 0
                load_transforms()
                # Check for new streams
                new_keys = [k for k in broker.keys(STREAM_PATTERN) if k not in streams]
                for nk in new_keys:
                    try:
                        broker.xgroup_create(nk, CONSUMER_GROUP, id="0", mkstream=True)
                    except Exception:
                        pass
                    streams[nk] = ">"
                    print(f"  New stream discovered: {nk}")

            # Read batch from broker streams
            results = broker.xreadgroup(
                groupname=CONSUMER_GROUP, consumername=CONSUMER_NAME,
                streams=streams, count=BATCH_SIZE, block=500
            )

            if not results:
                continue

            stats["batches"] += 1
            pipe = target.pipeline(transaction=False)
            ack_list = []

            for stream_key, messages in results:
                for msg_id, data in messages:
                    stats["events"] += 1
                    try:
                        # Parse Debezium extended format (no schema, flat structure)
                        value_str = data.get("value", "{}")
                        parsed = json.loads(value_str) if isinstance(value_str, str) else value_str

                        # With schemas.enable=false, structure is flat: op, after, before, source at top level
                        # (no "payload" wrapper)
                        op = parsed.get("op", "c")
                        source = parsed.get("source", {})
                        table = source.get("table", "")

                        if op == "d":
                            before = parsed.get("before", {})
                            if before:
                                pk = get_pk(before, table)
                                pipe.delete(f"{table}:{pk}")
                                apply_denormalize(target, before, table, "delete")
                                stats["deletes"] += 1
                        else:
                            after = parsed.get("after", {})
                            if after:
                                if not apply_filter(after, table):
                                    stats["skips"] += 1
                                else:
                                    after = apply_computed(after, table)
                                    apply_denormalize(target, after, table, "write")
                                    pk = get_pk(after, table)
                                    key = f"{table}:{pk}"
                                    hash_data = {k: str(v) if v is not None else "" for k, v in after.items()}
                                    pipe.hset(key, mapping=hash_data)
                                    stats["writes"] += 1
                    except Exception as e:
                        stats["errors"] += 1

                    ack_list.append((stream_key, msg_id))

            # Execute pipeline (batch write to target)
            pipe.execute()

            # ACK all processed messages on broker
            for stream_key, msg_id in ack_list:
                broker.xack(stream_key, CONSUMER_GROUP, msg_id)

        except Exception as e:
            stats["errors"] += 1
            print(f"  Error: {e}")
            time.sleep(1)

        # Stats every 10s
        now = time.time()
        if now - last_print > 10:
            print(f"  [Stats] events={stats['events']}, writes={stats['writes']}, "
                  f"deletes={stats['deletes']}, skips={stats['skips']}, "
                  f"batches={stats['batches']}, errors={stats['errors']}")
            last_print = now

    print(f"\n  Final: {stats}")

# ═══════════════════════════════════════════════════════════════
# Background: CloudWatch + Health
# ═══════════════════════════════════════════════════════════════
from flask import Flask, jsonify
app = Flask(__name__)
_start_time = time.time()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "stats": stats, "uptime": int(time.time()-_start_time)}), 200

@app.route("/metrics", methods=["GET"])
def metrics():
    return jsonify(stats), 200

def run_health_server():
    app.run(host="0.0.0.0", port=8081, threaded=True, use_reloader=False)

def cloudwatch_publisher():
    if not CLOUDWATCH_ENABLED:
        return
    try:
        import boto3
        cw = boto3.client("cloudwatch")
    except Exception:
        return
    prev = {"writes": 0, "errors": 0}
    while running:
        time.sleep(CLOUDWATCH_INTERVAL)
        try:
            dims = [{"Name": "Pipeline", "Value": PIPELINE_NAME}]
            cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=[
                {"MetricName": "WritesToValkey", "Value": stats["writes"]-prev["writes"], "Unit": "Count", "Dimensions": dims},
                {"MetricName": "Errors", "Value": stats["errors"]-prev["errors"], "Unit": "Count", "Dimensions": dims},
                {"MetricName": "TotalWrites", "Value": stats["writes"], "Unit": "Count", "Dimensions": dims},
            ])
            prev = {"writes": stats["writes"], "errors": stats["errors"]}
        except Exception:
            pass

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=cloudwatch_publisher, daemon=True).start()
    main()
