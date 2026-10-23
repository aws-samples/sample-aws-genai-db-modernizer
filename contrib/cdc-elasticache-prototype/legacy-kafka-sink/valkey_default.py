# Template: Valkey Default
#!/usr/bin/env python3
"""
CDC-cache Valkey Sink Worker
Consumes CDC events from Kafka and writes to Valkey/ElastiCache
"""

# Template created 03-20-2026

import os
import json
import signal
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from kafka import KafkaConsumer
import valkey

# Configuration from environment
KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
KAFKA_CONSUMER_GROUP = os.getenv('KAFKA_CONSUMER_GROUP', 'cdc-cache-sink')
KAFKA_TOPIC_PATTERN = os.getenv('KAFKA_TOPIC_PATTERN', 'cdc-cache.*')
VALKEY_HOST = os.getenv('VALKEY_HOST', 'localhost')
VALKEY_PORT = int(os.getenv('VALKEY_PORT', '6379'))
VALKEY_SSL = os.getenv('VALKEY_SSL', 'false').lower() == 'true'
VALKEY_AUTH_TOKEN = os.getenv('VALKEY_AUTH_TOKEN', '')
VALKEY_AUTH_USERNAME = os.getenv('VALKEY_AUTH_USERNAME', '')
KEY_PREFIX = os.getenv('KEY_PREFIX', '')
KEY_PATTERN = os.getenv('KEY_PATTERN', '{table}:{pk}')
WRITE_MODE = os.getenv('WRITE_MODE', 'hset')
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100'))
LWW_ENABLED = os.getenv('LWW_ENABLED', 'true').lower() == 'true'
KAFKA_SECURITY_PROTOCOL = os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT')
KAFKA_SASL_MECHANISM = os.getenv('KAFKA_SASL_MECHANISM', '')
METRICS_PORT = int(os.getenv('METRICS_PORT', '9090'))

# CloudWatch metrics
CLOUDWATCH_ENABLED = os.getenv('CLOUDWATCH_ENABLED', 'true').lower() == 'true'
CLOUDWATCH_NAMESPACE = os.getenv('CLOUDWATCH_NAMESPACE', 'CDC-cache/Pipeline')
CLOUDWATCH_INTERVAL = int(os.getenv('CLOUDWATCH_INTERVAL', '60'))
PIPELINE_NAME = os.getenv('KAFKA_CONSUMER_GROUP', 'unknown')

# Row filters from connector transforms
ROW_FILTERS = []
COLUMN_SELECTION = {}
COMPUTED_FIELDS = []
AGGREGATIONS = []
DENORMALIZE = []

# Config file path for hot-reload (optional)
TRANSFORMS_CONFIG_FILE = os.getenv('TRANSFORMS_CONFIG_FILE', '/opt/cdc-cache/transforms.json')
_transforms_file_mtime = 0


def load_transforms():
    """Load transforms from env var or config file."""
    global ROW_FILTERS, COLUMN_SELECTION, COMPUTED_FIELDS, AGGREGATIONS, DENORMALIZE, _transforms_file_mtime
    
    _transforms = {}
    
    # Try config file first (for hot-reload)
    if os.path.exists(TRANSFORMS_CONFIG_FILE):
        try:
            mtime = os.path.getmtime(TRANSFORMS_CONFIG_FILE)
            with open(TRANSFORMS_CONFIG_FILE) as f:
                _transforms = json.load(f)
            _transforms_file_mtime = mtime
        except (json.JSONDecodeError, OSError):
            pass
    
    # Fall back to env var
    if not _transforms:
        try:
            _transforms = json.loads(os.getenv('ROW_FILTERS', '{}'))
        except (json.JSONDecodeError, TypeError):
            pass
    
    if isinstance(_transforms, dict):
        ROW_FILTERS = _transforms.get('row_filters', [])
        COLUMN_SELECTION = _transforms.get('columns', {})
        COMPUTED_FIELDS = _transforms.get('computed_fields', [])
        AGGREGATIONS = _transforms.get('aggregations', [])
        DENORMALIZE = _transforms.get('denormalize', [])


def check_hot_reload():
    """Check if transforms config file changed and reload if so."""
    global _transforms_file_mtime
    if not os.path.exists(TRANSFORMS_CONFIG_FILE):
        return False
    try:
        mtime = os.path.getmtime(TRANSFORMS_CONFIG_FILE)
        if mtime > _transforms_file_mtime:
            print(f"  Hot-reload: transforms.json changed, reloading...")
            load_transforms()
            print(f"  Hot-reload: {len(ROW_FILTERS)} filters, {len(COMPUTED_FIELDS)} computed, {len(DENORMALIZE)} denormalize")
            return True
    except OSError:
        pass
    return False


# Initial load
load_transforms()


running = True

# Safe math expression evaluator — no code execution
import ast
import operator as _op

_SAFE_OPS = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
    ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv, ast.Mod: _op.mod,
    ast.Pow: _op.pow, ast.USub: _op.neg, ast.UAdd: _op.pos,
}

def _safe_eval(expr, variables):
    """Evaluate a math expression safely using AST parsing. No code execution."""
    # Prepare variables — convert numeric strings to floats
    safe_vars = {}
    for k, v in variables.items():
        try:
            safe_vars[k] = float(v) if v is not None and str(v).replace('.', '', 1).replace('-', '', 1).isdigit() else v
        except (ValueError, TypeError):
            safe_vars[k] = v

    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"Unsupported constant: {node.value}")
        elif isinstance(node, ast.Name):
            if node.id in safe_vars:
                return safe_vars[node.id]
            raise ValueError(f"Unknown variable: {node.id}")
        elif isinstance(node, ast.BinOp):
            op_func = _SAFE_OPS.get(type(node.op))
            if not op_func:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_func(_eval_node(node.left), _eval_node(node.right))
        elif isinstance(node, ast.UnaryOp):
            op_func = _SAFE_OPS.get(type(node.op))
            if not op_func:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op_func(_eval_node(node.operand))
        elif isinstance(node, ast.Call):
            # Allow basic string concat: str(x)
            if isinstance(node.func, ast.Name) and node.func.id == 'str' and len(node.args) == 1:
                return str(_eval_node(node.args[0]))
            raise ValueError(f"Function calls not allowed")
        else:
            raise ValueError(f"Unsupported expression: {type(node).__name__}")

    tree = ast.parse(expr, mode='eval')
    return _eval_node(tree)

# Shared metrics dict — updated by main loop, read by metrics HTTP server
metrics = {
    'messages_consumed': 0,
    'writes': 0,
    'deletes': 0,
    'skips': 0,
    'errors': 0,
    'start_time': None,
    'last_event_time': None,
    'consumer_group': KAFKA_CONSUMER_GROUP,
    'topics': [],
    'kafka_bootstrap': KAFKA_BOOTSTRAP_SERVERS,
    'valkey_host': VALKEY_HOST,
    'valkey_port': VALKEY_PORT,
    'write_mode': WRITE_MODE,
    'status': 'starting',
}


class MetricsHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler that serves /metrics as JSON."""
    def do_GET(self):
        if self.path == '/metrics' or self.path == '/':
            uptime = None
            if metrics['start_time']:
                uptime = round(time.time() - metrics['start_time'], 1)
            body = json.dumps({**metrics, 'uptime_seconds': uptime}, default=str)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_metrics_server():
    """Start the metrics HTTP server in a daemon thread."""
    try:
        server = HTTPServer(('0.0.0.0', METRICS_PORT), MetricsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"  Metrics server listening on port {METRICS_PORT}")
    except Exception as e:
        print(f"  Warning: Could not start metrics server on port {METRICS_PORT}: {e}")


def _publish_cloudwatch():
    """Background thread that publishes metrics to CloudWatch every CLOUDWATCH_INTERVAL seconds."""
    if not CLOUDWATCH_ENABLED:
        return
    try:
        import boto3
        cw = boto3.client('cloudwatch')
        print(f"  CloudWatch metrics enabled: namespace={CLOUDWATCH_NAMESPACE}, interval={CLOUDWATCH_INTERVAL}s")
    except Exception as e:
        print(f"  CloudWatch unavailable: {e}")
        return

    _prev = {'messages_consumed': 0, 'writes': 0, 'errors': 0, 'skips': 0}

    while running:
        time.sleep(CLOUDWATCH_INTERVAL)
        try:
            # Calculate rates (per interval)
            consumed_rate = metrics['messages_consumed'] - _prev['messages_consumed']
            write_rate = metrics['writes'] - _prev['writes']
            error_rate = metrics['errors'] - _prev['errors']
            skip_rate = metrics['skips'] - _prev['skips']
            _prev = {k: metrics[k] for k in _prev}

            # Calculate lag (seconds since last event)
            lag = 0
            if metrics['last_event_time']:
                lag = time.time() - metrics['last_event_time']

            dimensions = [{'Name': 'Pipeline', 'Value': PIPELINE_NAME}]

            cw.put_metric_data(
                Namespace=CLOUDWATCH_NAMESPACE,
                MetricData=[
                    {'MetricName': 'MessagesConsumed', 'Value': consumed_rate, 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'WritesToValkey', 'Value': write_rate, 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'Errors', 'Value': error_rate, 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'Skipped', 'Value': skip_rate, 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'ConsumerLagSeconds', 'Value': lag, 'Unit': 'Seconds', 'Dimensions': dimensions},
                    {'MetricName': 'TotalMessagesConsumed', 'Value': metrics['messages_consumed'], 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'TotalWrites', 'Value': metrics['writes'], 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'TotalErrors', 'Value': metrics['errors'], 'Unit': 'Count', 'Dimensions': dimensions},
                    {'MetricName': 'Status', 'Value': 1 if metrics['status'] == 'running' else 0, 'Unit': 'None', 'Dimensions': dimensions},
                ]
            )
        except Exception as e:
            print(f"  CloudWatch publish error: {e}")


def start_cloudwatch_publisher():
    """Start CloudWatch metrics publisher in a daemon thread."""
    if CLOUDWATCH_ENABLED:
        thread = threading.Thread(target=_publish_cloudwatch, daemon=True)
        thread.start()


def signal_handler(sig, frame):
    global running
    print("Shutdown signal received, stopping...")
    running = False


def evaluate_filter(row, f):
    """Evaluate a single filter condition against a row."""
    field = f.get('field', '')
    op = f.get('operator', '=')
    value = f.get('value', '')
    row_val = row.get(field)
    if row_val is None:
        return False
    row_str = str(row_val)
    if op == '=':
        return row_str == value
    elif op == '!=':
        return row_str != value
    elif op in ('>', '<', '>=', '<='):
        try:
            rv, fv = float(row_val), float(value)
            return (rv > fv if op == '>' else rv < fv if op == '<' else rv >= fv if op == '>=' else rv <= fv)
        except (ValueError, TypeError):
            return row_str > value if op == '>' else row_str < value if op == '<' else row_str >= value if op == '>=' else row_str <= value
    elif op == 'contains':
        return value.lower() in row_str.lower()
    elif op == 'not_contains':
        return value.lower() not in row_str.lower()
    elif op == 'in':
        vals = [v.strip() for v in value.split(',')]
        return row_str in vals
    elif op == 'not_in':
        vals = [v.strip() for v in value.split(',')]
        return row_str not in vals
    return True


def passes_filters(row, table_name=''):
    """Check if a row passes all configured filters. Empty filters = pass all."""
    if not ROW_FILTERS:
        return True
    # Only apply filters that match this table explicitly or wildcard '*'
    applicable = [f for f in ROW_FILTERS if f.get('table', '') == '*' or f.get('table', '') == table_name]
    if not applicable:
        return True
    return all(evaluate_filter(row, f) for f in applicable)


def apply_column_selection(row, table_name):
    """Keep only selected columns for a table. Empty selection = keep all."""
    if not COLUMN_SELECTION:
        return row
    columns = COLUMN_SELECTION.get(table_name, COLUMN_SELECTION.get('*', []))
    if not columns:
        return row
    return {k: v for k, v in row.items() if k in columns}


def apply_computed_fields(row, table_name):
    """Add computed fields to a row based on expressions."""
    if not COMPUTED_FIELDS:
        return row
    applicable = [cf for cf in COMPUTED_FIELDS if cf.get('table', '') in ('*', table_name)]
    for cf in applicable:
        name = cf.get('name', '')
        expr = cf.get('expression', '')
        if not name or not expr:
            continue
        try:
            result = _safe_eval(expr, row)
            row[name] = result
        except Exception as e:
            row[name] = f"ERROR: {e}"
    return row


def apply_aggregation(valkey_client, row, table_name, kafka_key=None):
    """Apply aggregation functions and write to Valkey. Returns True if handled."""
    if not AGGREGATIONS:
        return False
    applicable = [a for a in AGGREGATIONS if a.get('table', '') in ('*', table_name)]
    if not applicable:
        return False
    for agg in applicable:
        func = agg.get('function', 'count')
        field = agg.get('field', '')
        group_by = agg.get('group_by', '')
        target_key = agg.get('target_key', f'{table_name}:{func}')
        # Resolve group_by value into the target key
        if group_by and group_by in row:
            group_val = str(row.get(group_by, 'unknown'))
            target_key = target_key.replace(f'{{{group_by}}}', group_val)
        try:
            if func == 'count':
                valkey_client.incr(target_key)
            elif func in ('sum', 'min', 'max', 'avg') and field in row:
                raw_val = row[field]
                # Handle Debezium base64-encoded DECIMAL values
                if isinstance(raw_val, str) and raw_val and not raw_val.replace('.', '', 1).replace('-', '', 1).isdigit():
                    try:
                        import base64
                        decoded = base64.b64decode(raw_val)
                        val = int.from_bytes(decoded, byteorder='big', signed=True) / 100.0
                    except Exception:
                        val = 0
                else:
                    val = float(raw_val) if raw_val is not None else 0

                if func == 'sum':
                    valkey_client.incrbyfloat(target_key, val)
                elif func == 'min':
                    existing = valkey_client.get(target_key)
                    if existing is None or val < float(existing):
                        valkey_client.set(target_key, str(val))
                elif func == 'max':
                    existing = valkey_client.get(target_key)
                    if existing is None or val > float(existing):
                        valkey_client.set(target_key, str(val))
                elif func == 'avg':
                    count_key = f'{target_key}:__count'
                    sum_key = f'{target_key}:__sum'
                    new_count = valkey_client.incr(count_key)
                    new_sum = valkey_client.incrbyfloat(sum_key, val)
                    valkey_client.set(target_key, str(float(new_sum) / int(new_count)))
        except Exception as e:
            print(f"Aggregation error ({func} on {field}): {e}")
    return True


def apply_denormalize(valkey_client, row, table_name, op='c', pending_ops=None):
    """Denormalize: merge child table fields into parent key.
    Config example:
      {"child_table": "addresses", "parent_table": "customers",
       "join_field": "customer_id", "fields": ["street","city","state","zip"],
       "prefix": "address_"}
    Returns True if this event was handled by denormalization (skip normal write).
    """
    if not DENORMALIZE:
        return False
    applicable = [d for d in DENORMALIZE if d.get('child_table') == table_name]
    if not applicable:
        return False

    for rule in applicable:
        parent_table = rule.get('parent_table', '')
        join_field = rule.get('join_field', '')
        fields = rule.get('fields', [])
        # Handle fields as comma-separated string or list
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(',') if f.strip()]
        prefix = rule.get('prefix', '')

        if not parent_table or not join_field:
            continue

        parent_pk = row.get(join_field)
        if not parent_pk:
            continue

        parent_key = f"{KEY_PREFIX}{parent_table}:{parent_pk}" if KEY_PREFIX else f"{parent_table}:{parent_pk}"

        if op == 'd':
            # On delete: remove the merged fields from parent
            remove_fields = [f"{prefix}{f}" for f in fields]
            if pending_ops is not None:
                pending_ops.append({'type': 'hdel', 'key': parent_key, 'fields': remove_fields})
            else:
                for f in remove_fields:
                    valkey_client.hdel(parent_key, f)
        else:
            # On create/update: merge fields into parent hash
            merge_data = {}
            for f in fields:
                if f in row:
                    val = row[f]
                    merge_data[f"{prefix}{f}"] = json.dumps(val) if isinstance(val, (dict, list)) else str(val) if val is not None else ''
            if merge_data:
                if pending_ops is not None:
                    pending_ops.append({'type': 'hset', 'key': parent_key, 'data': merge_data})
                else:
                    valkey_client.hset(parent_key, mapping=merge_data)

    return True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def create_valkey_client():
    """Create Valkey/Redis client connection (cluster-aware)."""
    kwargs = {
        'host': VALKEY_HOST,
        'port': VALKEY_PORT,
        'decode_responses': True,
        'socket_timeout': 5,
        'socket_connect_timeout': 5,
    }
    if VALKEY_SSL:
        kwargs['ssl'] = True
        kwargs['ssl_cert_reqs'] = None
    if VALKEY_AUTH_TOKEN:
        kwargs['password'] = VALKEY_AUTH_TOKEN
    if VALKEY_AUTH_USERNAME:
        kwargs['username'] = VALKEY_AUTH_USERNAME

    # Try cluster mode first, fall back to standalone
    try:
        client = valkey.cluster.ValkeyCluster(**kwargs)
        client.ping()
        print("  Connected in CLUSTER mode")
        return client
    except Exception as cluster_err:
        print(f"  Cluster mode failed ({cluster_err}), trying standalone...")
        client = valkey.Valkey(**kwargs)
        client.ping()
        print("  Connected in STANDALONE mode")
        return client


def extract_key(event, table_name, kafka_key=None):
    """Extract the key for Valkey based on the configured pattern."""
    # Prefer the Kafka message key (Debezium puts PK fields there)
    key_data = None
    if kafka_key:
        if isinstance(kafka_key, dict):
            key_data = kafka_key.get('payload', kafka_key)
        elif isinstance(kafka_key, (str, int, float)):
            key_data = kafka_key

    if not key_data:
        key_data = event.get('key', {})

    # Build primary key from key payload
    if isinstance(key_data, dict):
        pk_parts = [str(v) for v in key_data.values()]
        pk = ':'.join(pk_parts) if pk_parts else 'unknown'
    else:
        pk = str(key_data) if key_data else 'unknown'

    # Apply key pattern
    key = KEY_PATTERN.replace('{table}', table_name).replace('{pk}', pk)

    # Add prefix if configured
    if KEY_PREFIX:
        key = f"{KEY_PREFIX}:{key}"

    return key


def process_event(valkey_client, topic, event, kafka_key=None, pending_ops=None):
    """Process a single CDC event. Queues write to pending_ops if provided, otherwise writes directly."""
    try:
        # Parse topic to get table name (format: prefix.schema.table)
        topic_parts = topic.split('.')
        table_name = topic_parts[-1] if topic_parts else 'unknown'

        payload = event.get('payload', event)
        op = payload.get('op', 'c')  # c=create, u=update, d=delete, r=read(snapshot)

        if op == 'd':
            # Delete operation
            # Check if this is a denormalized child table — remove merged fields from parent
            before = payload.get('before', {})
            if before and DENORMALIZE:
                apply_denormalize(valkey_client, before, table_name, 'd', pending_ops)
            key = extract_key(payload, table_name, kafka_key)
            if pending_ops is not None:
                pending_ops.append({'type': 'delete', 'key': key})
            else:
                valkey_client.delete(key)
            return 'delete'

        # For create/update/snapshot, get the after state
        after = payload.get('after')
        if not after:
            return 'skip'

        # === TRANSFORMATION PIPELINE ===
        # Step 1: Row filter
        if not passes_filters(after, table_name):
            return 'skip'

        # Step 2: Column selection
        after = apply_column_selection(after, table_name)

        # Step 3: Computed fields
        after = apply_computed_fields(after, table_name)

        # Step 4: Aggregation (if configured, handles write and returns)
        if apply_aggregation(valkey_client, after, table_name, kafka_key):
            return 'write'

        # Step 5: Denormalization (merge child fields into parent key)
        if apply_denormalize(valkey_client, after, table_name, op, pending_ops):
            return 'write'

        key = extract_key(payload, table_name, kafka_key)

        # LWW check - only write if this is newer
        if LWW_ENABLED:
            ts_ms = payload.get('ts_ms', 0)
            try:
                if WRITE_MODE == 'hset':
                    existing_ts = valkey_client.hget(key, '__ts_ms')
                elif WRITE_MODE == 'json_object':
                    raw = valkey_client.json().get(key, '$.__ts_ms')
                    existing_ts = raw[0] if raw else None
                else:
                    raw = valkey_client.get(key)
                    existing_ts = None
                    if raw:
                        try:
                            existing_obj = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                            existing_ts = existing_obj.get('__ts_ms') if isinstance(existing_obj, dict) else None
                        except (json.JSONDecodeError, AttributeError):
                            existing_ts = None
            except Exception:
                existing_ts = None
            if existing_ts and int(existing_ts) > ts_ms:
                return 'skip_lww'

        # Write based on mode
        if WRITE_MODE == 'hset':
            # Store as hash
            hash_data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) if v is not None else ''
                        for k, v in after.items()}
            if LWW_ENABLED:
                hash_data['__ts_ms'] = str(payload.get('ts_ms', 0))
            if pending_ops is not None:
                pending_ops.append({'type': 'hset', 'key': key, 'data': hash_data})
            else:
                valkey_client.hset(key, mapping=hash_data)
        elif WRITE_MODE == 'json':
            # Store as JSON string
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            data = json.dumps(after)
            if pending_ops is not None:
                pending_ops.append({'type': 'set', 'key': key, 'data': data})
            else:
                valkey_client.set(key, data)
        elif WRITE_MODE == 'json_object':
            # Store using Valkey native JSON datatype (JSON.SET)
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            valkey_client.json().set(key, '$', after)
        elif WRITE_MODE == 'string':
            # Store specific field or entire record as string
            data = json.dumps(after)
            if pending_ops is not None:
                pending_ops.append({'type': 'set', 'key': key, 'data': data})
            else:
                valkey_client.set(key, data)

        return 'write'

    except Exception as e:
        print(f"Error processing event: {e}")
        return 'error'


def main():
    print(f"CDC-cache Valkey Sink Worker starting...")
    print(f"  Kafka: {KAFKA_BOOTSTRAP_SERVERS}")
    print(f"  Topic Pattern: {KAFKA_TOPIC_PATTERN}")
    print(f"  Valkey: {VALKEY_HOST}:{VALKEY_PORT}")
    print(f"  Write Mode: {WRITE_MODE}")
    print(f"  LWW Enabled: {LWW_ENABLED}")
    print(f"  Pipeline Batch: {BATCH_SIZE} (Valkey pipelining enabled)")
    if ROW_FILTERS:
        print(f"  Row Filters: {len(ROW_FILTERS)} filter(s) active")
        for f in ROW_FILTERS:
            print(f"    - [{f.get('table', '*')}] {f.get('field')} {f.get('operator')} {f.get('value')}")
    if COLUMN_SELECTION:
        print(f"  Column Selection: {len(COLUMN_SELECTION)} table(s)")
        for tbl, cols in COLUMN_SELECTION.items():
            print(f"    - [{tbl}] {', '.join(cols)}")
    if COMPUTED_FIELDS:
        print(f"  Computed Fields: {len(COMPUTED_FIELDS)} field(s)")
        for cf in COMPUTED_FIELDS:
            print(f"    - [{cf.get('table', '*')}] {cf.get('name')} = {cf.get('expression')}")
    if AGGREGATIONS:
        print(f"  Aggregations: {len(AGGREGATIONS)} rule(s)")
        for a in AGGREGATIONS:
            print(f"    - [{a.get('table', '*')}] {a.get('function')}({a.get('field', '*')}) group_by={a.get('group_by', 'none')} -> {a.get('target_key')}")
    if DENORMALIZE:
        print(f"  Denormalize: {len(DENORMALIZE)} rule(s)")
        for d in DENORMALIZE:
            print(f"    - {d.get('child_table')} -> {d.get('parent_table')} (join: {d.get('join_field')}, fields: {d.get('fields')}, prefix: {d.get('prefix', '')})")

    # Start metrics HTTP server
    start_metrics_server()
    start_cloudwatch_publisher()
    metrics['start_time'] = time.time()

    # Connect to Valkey
    print("Connecting to Valkey...")
    valkey_client = None
    while running and valkey_client is None:
        try:
            valkey_client = create_valkey_client()
            print("Connected to Valkey!")
            metrics['status'] = 'connected_valkey'
        except Exception as e:
            print(f"Failed to connect to Valkey: {e}, retrying in 5s...")
            metrics['status'] = 'error_valkey'
            time.sleep(5)

    if not running:
        return

    # Create Kafka consumer
    print("Connecting to Kafka...")
    print(f"Consumer group: {KAFKA_CONSUMER_GROUP}")
    consumer = None
    while running and consumer is None:
        try:
            def safe_json_deserialize(m):
                if not m:
                    return None
                try:
                    return json.loads(m.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return None

            kafka_kwargs = {
                'bootstrap_servers': KAFKA_BOOTSTRAP_SERVERS.split(','),
                'group_id': KAFKA_CONSUMER_GROUP,
                'auto_offset_reset': 'earliest',
                'enable_auto_commit': True,
                'metadata_max_age_ms': 10000,
                'value_deserializer': safe_json_deserialize,
                'key_deserializer': safe_json_deserialize,
            }
            # MSK IAM authentication
            if KAFKA_SECURITY_PROTOCOL == 'SASL_SSL' and KAFKA_SASL_MECHANISM == 'AWS_MSK_IAM':
                from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
                from kafka.sasl.oauth import AbstractTokenProvider

                class MSKTokenProvider(AbstractTokenProvider):
                    def token(self):
                        token, _ = MSKAuthTokenProvider.generate_auth_token(
                            region=os.getenv('AWS_DEFAULT_REGION')
                        )
                        return token

                kafka_kwargs['security_protocol'] = 'SASL_SSL'
                kafka_kwargs['sasl_mechanism'] = 'OAUTHBEARER'
                kafka_kwargs['sasl_oauth_token_provider'] = MSKTokenProvider()
                print("  Using MSK IAM authentication")
            consumer = KafkaConsumer(**kafka_kwargs)
            # Discover topics matching pattern, then subscribe explicitly
            # (pattern subscribe can be unreliable with MSK IAM auth)
            import re
            all_topics = consumer.topics()
            pattern_re = re.compile(KAFKA_TOPIC_PATTERN)
            # Only match CDC data topics (format: prefix.pipeline.database.table)
            # Exclude internal connect topics (configs, offsets, status) and schema history
            internal_suffixes = ('_connect_configs', '_connect_offsets', '_connect_status')
            matched_topics = [
                t for t in all_topics
                if pattern_re.match(t)
                and not t.endswith(internal_suffixes)
                and 'schema-history' not in t
                and len(t.split('.')) >= 4  # CDC data topics have prefix.pipeline.db.table
            ]
            if matched_topics:
                consumer.subscribe(topics=matched_topics)
                print(f"Subscribed to {len(matched_topics)} topic(s): {matched_topics}")
                metrics['topics'] = matched_topics
            else:
                # Fall back to pattern subscribe and wait for metadata refresh
                print(f"  No topics matched '{KAFKA_TOPIC_PATTERN}' yet, using pattern subscribe...")
                consumer.subscribe(pattern=KAFKA_TOPIC_PATTERN)
                print(f"Subscribed to topics matching: {KAFKA_TOPIC_PATTERN}")
                metrics['topics'] = [KAFKA_TOPIC_PATTERN]
        except Exception as e:
            print(f"Failed to connect to Kafka: {e}, retrying in 5s...")
            metrics['status'] = 'error_kafka'
            time.sleep(5)

    if not running:
        return

    # Process messages
    stats = {'write': 0, 'delete': 0, 'skip': 0, 'error': 0}
    batch_count = 0
    metrics['status'] = 'running'

    # Pipelining: batch writes for higher throughput
    PIPELINE_FLUSH_SIZE = BATCH_SIZE
    pending_ops = []

    def flush_pipeline():
        """Execute all pending write operations in a single Valkey pipeline."""
        if not pending_ops:
            return
        try:
            pipe = valkey_client.pipeline(transaction=False)
            for op in pending_ops:
                if op['type'] == 'hset':
                    pipe.hset(op['key'], mapping=op['data'])
                elif op['type'] == 'set':
                    pipe.set(op['key'], op['data'])
                elif op['type'] == 'delete':
                    pipe.delete(op['key'])
                elif op['type'] == 'hdel':
                    for f in op['fields']:
                        pipe.hdel(op['key'], f)
            pipe.execute()
        except Exception as e:
            # Fallback: execute one by one
            for op in pending_ops:
                try:
                    if op['type'] == 'hset':
                        valkey_client.hset(op['key'], mapping=op['data'])
                    elif op['type'] == 'set':
                        valkey_client.set(op['key'], op['data'])
                    elif op['type'] == 'delete':
                        valkey_client.delete(op['key'])
                    elif op['type'] == 'hdel':
                        for f in op['fields']:
                            valkey_client.hdel(op['key'], f)
                except Exception:
                    pass
        pending_ops.clear()

    print("Processing messages (pipelining enabled)...")
    _hot_reload_counter = 0
    while running:
        try:
            # Check for config hot-reload every ~10 seconds (100 poll cycles at 100ms)
            _hot_reload_counter += 1
            if _hot_reload_counter >= 100:
                _hot_reload_counter = 0
                check_hot_reload()

            messages = consumer.poll(timeout_ms=100, max_records=BATCH_SIZE)

            for topic_partition, records in messages.items():
                for record in records:
                    if not running:
                        break

                    # Skip messages that couldn't be deserialized
                    if record.value is None:
                        continue

                    result = process_event(valkey_client, record.topic, record.value, record.key, pending_ops)
                    stats[result] = stats.get(result, 0) + 1
                    batch_count += 1

                    # Update shared metrics
                    metrics['messages_consumed'] += 1
                    metrics['last_event_time'] = time.time()
                    if result == 'write':
                        metrics['writes'] += 1
                    elif result == 'delete':
                        metrics['deletes'] += 1
                    elif result == 'error':
                        metrics['errors'] += 1
                    else:
                        metrics['skips'] += 1

                    # Flush pipeline when batch is full
                    if len(pending_ops) >= PIPELINE_FLUSH_SIZE:
                        flush_pipeline()

                    if batch_count >= 1000:
                        print(f"Stats: writes={stats['write']}, deletes={stats['delete']}, "
                              f"skips={stats['skip']}, errors={stats['error']}")
                        batch_count = 0

            # Flush remaining ops after each poll
            flush_pipeline()

        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(1)

    # Cleanup
    print("Shutting down...")
    metrics['status'] = 'stopped'
    if consumer:
        consumer.close()
    print(f"Final stats: writes={stats['write']}, deletes={stats['delete']}, "
          f"skips={stats['skip']}, errors={stats['error']}")
    print("Goodbye!")


if __name__ == '__main__':
    main()
