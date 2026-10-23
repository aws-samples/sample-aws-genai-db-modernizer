#!/usr/bin/env python3
"""
CDC-cache v3 Sink Worker — HTTP receiver for Debezium Server CDC events.

Receives POST /events from Debezium Server HTTP sink.
Applies transforms (row filter, computed fields, denormalization).
Writes to Valkey target cluster.

No Kafka. No broker. Just HTTP in, Valkey out.
"""

import os
import json
import time
import ast
import operator as _op
import threading
import signal
import sys

from flask import Flask, request, jsonify
import valkey

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════
TARGET_HOST = os.getenv('TARGET_HOST', 'localhost')
TARGET_PORT = int(os.getenv('TARGET_PORT', '6379'))
TARGET_SSL = os.getenv('TARGET_SSL', 'true').lower() == 'true'
TRANSFORMS_FILE = os.getenv('TRANSFORMS_FILE', '/opt/cdc-cache/transforms.json')
CLOUDWATCH_ENABLED = os.getenv('CLOUDWATCH_ENABLED', 'true').lower() == 'true'
CLOUDWATCH_NAMESPACE = os.getenv('CLOUDWATCH_NAMESPACE', 'CDC-cache/Pipeline-v3')
CLOUDWATCH_INTERVAL = int(os.getenv('CLOUDWATCH_INTERVAL', '60'))
PIPELINE_NAME = os.getenv('PIPELINE_NAME', 'cdc-cache-v3')

# ═══════════════════════════════════════════════════════════════════
# Global State
# ═══════════════════════════════════════════════════════════════════
target = None
stats = {'writes': 0, 'deletes': 0, 'skips': 0, 'errors': 0, 'events_received': 0}
running = True

# Transforms (hot-reloadable)
ROW_FILTERS = []
COMPUTED_FIELDS = []
DENORMALIZE = []
COLUMN_SELECTION = {}
_transforms_mtime = 0

# ═══════════════════════════════════════════════════════════════════
# Valkey Connection
# ═══════════════════════════════════════════════════════════════════


def connect_target():
    """Connect to Valkey target cluster."""
    global target
    kwargs = {
        'host': TARGET_HOST,
        'port': TARGET_PORT,
        'decode_responses': True,
        'socket_timeout': 5,
        'socket_connect_timeout': 5,
    }
    if TARGET_SSL:
        kwargs['ssl'] = True
        kwargs['ssl_cert_reqs'] = None

    # Try cluster mode first
    try:
        target = valkey.cluster.ValkeyCluster(**kwargs)
        target.ping()
        print(f"  ✅ Connected to Valkey (cluster mode): {TARGET_HOST}:{TARGET_PORT}")
        return
    except Exception as e:
        print(f"  Cluster mode failed ({e}), trying standalone...")

    target = valkey.Valkey(**kwargs)
    target.ping()
    print(f"  ✅ Connected to Valkey (standalone): {TARGET_HOST}:{TARGET_PORT}")


# ═══════════════════════════════════════════════════════════════════
# Transforms — Hot Reloadable
# ═══════════════════════════════════════════════════════════════════


def load_transforms():
    """Load/reload transforms from JSON config file."""
    global ROW_FILTERS, COMPUTED_FIELDS, DENORMALIZE, COLUMN_SELECTION, _transforms_mtime
    if not os.path.exists(TRANSFORMS_FILE):
        return
    try:
        mtime = os.path.getmtime(TRANSFORMS_FILE)
        if mtime <= _transforms_mtime:
            return  # No change
        with open(TRANSFORMS_FILE) as f:
            t = json.load(f)
        ROW_FILTERS = t.get('row_filters', [])
        COMPUTED_FIELDS = t.get('computed_fields', [])
        DENORMALIZE = t.get('denormalize', [])
        COLUMN_SELECTION = t.get('columns', {})
        _transforms_mtime = mtime
        print(f"  Transforms loaded: {len(ROW_FILTERS)} filters, "
              f"{len(COMPUTED_FIELDS)} computed, {len(DENORMALIZE)} denorm")
    except Exception as e:
        print(f"  Transform load error: {e}")


# ═══════════════════════════════════════════════════════════════════
# Safe Math Evaluator
# ═══════════════════════════════════════════════════════════════════
_SAFE_OPS = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
    ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod, ast.USub: _op.neg,
}


def safe_eval(expr, variables):
    """Evaluate math expression safely. No arbitrary code execution."""
    safe_vars = {}
    for k, v in variables.items():
        try:
            safe_vars[k] = float(v) if v is not None and str(v).replace('.', '', 1).replace('-', '', 1).isdigit() else 0
        except (ValueError, TypeError):
            safe_vars[k] = 0

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            return safe_vars.get(node.id, 0)
        elif isinstance(node, ast.BinOp):
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.UnaryOp):
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        return 0

    return _eval(ast.parse(expr, mode='eval'))


# ═══════════════════════════════════════════════════════════════════
# Transform Functions
# ═══════════════════════════════════════════════════════════════════


def apply_filter(row, table):
    """Return False if row should be skipped based on filters."""
    if not ROW_FILTERS:
        return True
    for f in ROW_FILTERS:
        if f.get('table', '*') not in ('*', table):
            continue
        field = f.get('field', '')
        op = f.get('operator', '=')
        expected = f.get('value', '')
        val = str(row.get(field, ''))

        if op == '=' and val != expected:
            return False
        elif op == '!=' and val == expected:
            return False
        elif op == 'in' and val not in [v.strip() for v in expected.split(',')]:
            return False
        elif op == 'not_in' and val in [v.strip() for v in expected.split(',')]:
            return False
    return True


def apply_computed(row, table):
    """Apply computed fields (safe math expressions)."""
    if not COMPUTED_FIELDS:
        return row
    for cf in COMPUTED_FIELDS:
        if cf.get('table', '*') not in ('*', table):
            continue
        try:
            row[cf['name']] = safe_eval(cf['expression'], row)
        except Exception:
            pass
    return row


def apply_column_selection(row, table):
    """Keep only selected columns for a table."""
    if not COLUMN_SELECTION:
        return row
    columns = COLUMN_SELECTION.get(table, COLUMN_SELECTION.get('*', []))
    if not columns:
        return row
    return {k: v for k, v in row.items() if k in columns}


def apply_denormalize(row, table, op='write'):
    """Merge child table fields into parent hash key in Valkey."""
    if not DENORMALIZE:
        return False
    handled = False
    for rule in DENORMALIZE:
        if rule.get('child_table') != table:
            continue
        parent_table = rule.get('parent_table', '')
        join_field = rule.get('join_field', '')
        fields = rule.get('fields', [])
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(',') if f.strip()]
        prefix = rule.get('prefix', '')

        parent_pk = row.get(join_field)
        if not parent_pk:
            continue

        parent_key = f"{parent_table}:{parent_pk}"

        if op == 'delete':
            for f in fields:
                target.hdel(parent_key, f"{prefix}{f}")
        else:
            merge = {}
            for f in fields:
                if f in row:
                    merge[f"{prefix}{f}"] = str(row[f]) if row[f] is not None else ''
            if merge:
                target.hset(parent_key, mapping=merge)
        handled = True
    return handled


def get_pk(row, table):
    """Determine primary key from row data."""
    for pk_field in ['id', f'{table}_id', 'customer_id', 'merchant_id', 'product_id', 'order_id']:
        if pk_field in row:
            return row[pk_field]
    # Fallback: first value
    return list(row.values())[0] if row else 'unknown'


# ═══════════════════════════════════════════════════════════════════
# Event Processing
# ═══════════════════════════════════════════════════════════════════


def process_event(event):
    """
    Process a single CDC event from Debezium Server.
    
    With ExtractNewRecordState transform, event payload is flat row data
    with __op, __table, __source_ts_ms added as metadata fields.
    """
    try:
        stats['events_received'] += 1

        # Extract metadata from unwrapped event
        op = event.get('__op', 'c')  # c=create, u=update, d=delete, r=read(snapshot)
        table = event.get('__table', '')

        # Remove Debezium metadata fields to get clean row data
        row = {k: v for k, v in event.items()
               if not k.startswith('__') and k != '__deleted'}

        # Handle delete
        if op == 'd' or event.get('__deleted') == 'true':
            pk = get_pk(row, table)
            target.delete(f"{table}:{pk}")
            apply_denormalize(row, table, 'delete')
            stats['deletes'] += 1
            return

        # Apply transforms
        if not apply_filter(row, table):
            stats['skips'] += 1
            return

        row = apply_column_selection(row, table)
        row = apply_computed(row, table)

        # Denormalization (if configured, merges into parent key)
        denorm_handled = apply_denormalize(row, table, 'write')

        # Write to Valkey as hash
        pk = get_pk(row, table)
        key = f"{table}:{pk}"
        hash_data = {k: str(v) if v is not None else '' for k, v in row.items()}
        target.hset(key, mapping=hash_data)
        stats['writes'] += 1

    except Exception as e:
        stats['errors'] += 1
        print(f"  Error processing event: {e}")


# ═══════════════════════════════════════════════════════════════════
# HTTP Endpoints
# ═══════════════════════════════════════════════════════════════════


@app.route('/events', methods=['POST'])
def receive_events():
    """
    HTTP endpoint that Debezium Server posts CDC events to.
    Debezium HTTP sink sends each event as a separate POST.
    """
    try:
        data = request.get_json(force=True, silent=True)
        if data is None:
            # Try raw body
            raw = request.get_data(as_text=True)
            if raw:
                data = json.loads(raw)
            else:
                return jsonify({'status': 'empty'}), 200

        if isinstance(data, list):
            for event in data:
                if event:
                    process_event(event)
        elif isinstance(data, dict):
            process_event(data)

        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        stats['errors'] += 1
        print(f"  HTTP handler error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check for ECS."""
    try:
        target.ping()
        return jsonify({
            'status': 'healthy',
            'stats': stats,
            'target': f"{TARGET_HOST}:{TARGET_PORT}",
            'uptime_seconds': int(time.time() - _start_time)
        }), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503


@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """Prometheus-style metrics for monitoring."""
    return jsonify({
        **stats,
        'target_host': TARGET_HOST,
        'transforms': {
            'row_filters': len(ROW_FILTERS),
            'computed_fields': len(COMPUTED_FIELDS),
            'denormalize': len(DENORMALIZE),
        }
    }), 200


# ═══════════════════════════════════════════════════════════════════
# Background Threads
# ═══════════════════════════════════════════════════════════════════


def reload_watcher():
    """Check for transform file changes every 10 seconds."""
    while running:
        time.sleep(10)
        load_transforms()


def stats_printer():
    """Print stats every 30 seconds."""
    while running:
        time.sleep(30)
        print(f"  [Stats] events={stats['events_received']}, writes={stats['writes']}, "
              f"deletes={stats['deletes']}, skips={stats['skips']}, errors={stats['errors']}")


def cloudwatch_publisher():
    """Publish metrics to CloudWatch."""
    if not CLOUDWATCH_ENABLED:
        return
    try:
        import boto3
        cw = boto3.client('cloudwatch')
        print(f"  CloudWatch enabled: namespace={CLOUDWATCH_NAMESPACE}")
    except Exception as e:
        print(f"  CloudWatch unavailable: {e}")
        return

    prev = {'writes': 0, 'errors': 0}
    while running:
        time.sleep(CLOUDWATCH_INTERVAL)
        try:
            write_rate = stats['writes'] - prev['writes']
            error_rate = stats['errors'] - prev['errors']
            prev = {'writes': stats['writes'], 'errors': stats['errors']}

            dims = [{'Name': 'Pipeline', 'Value': PIPELINE_NAME}]
            cw.put_metric_data(
                Namespace=CLOUDWATCH_NAMESPACE,
                MetricData=[
                    {'MetricName': 'WritesToValkey', 'Value': write_rate, 'Unit': 'Count', 'Dimensions': dims},
                    {'MetricName': 'Errors', 'Value': error_rate, 'Unit': 'Count', 'Dimensions': dims},
                    {'MetricName': 'TotalWrites', 'Value': stats['writes'], 'Unit': 'Count', 'Dimensions': dims},
                    {'MetricName': 'EventsReceived', 'Value': stats['events_received'], 'Unit': 'Count', 'Dimensions': dims},
                ]
            )
        except Exception as e:
            print(f"  CloudWatch publish error: {e}")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════
_start_time = time.time()


def main():
    global running

    print("=" * 60)
    print("  CDC-cache v3 Sink Worker")
    print("  Receives CDC events via HTTP from Debezium Server")
    print("  Applies transforms → writes to Valkey")
    print("=" * 60)
    print(f"  Target: {TARGET_HOST}:{TARGET_PORT} (SSL={TARGET_SSL})")
    print(f"  Transforms: {TRANSFORMS_FILE}")
    print()

    # Connect to Valkey
    connect_target()
    load_transforms()

    # Start background threads
    threading.Thread(target=reload_watcher, daemon=True).start()
    threading.Thread(target=stats_printer, daemon=True).start()
    threading.Thread(target=cloudwatch_publisher, daemon=True).start()

    print()
    print("  Listening on port 8081 for Debezium events...")
    print("─" * 60)

    # Run Flask (use gunicorn in production via CMD override)
    app.run(host='0.0.0.0', port=8081, threaded=True)


if __name__ == '__main__':
    main()
