# Template: Valkey Glide Cluster
#!/usr/bin/env python3
"""
CDC-cache Valkey Sink Worker (Glide Cluster)
Consumes CDC events from Kafka and writes to Valkey/ElastiCache
using the Valkey Glide client library for cluster mode.
"""

import os
import json
import signal
import sys
import time
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from kafka import KafkaConsumer
from glide import (
    GlideClusterClientConfiguration,
    GlideClusterClient,
    NodeAddress,
    ServerCredentials,
)

# Configuration from environment
KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
KAFKA_CONSUMER_GROUP = os.getenv('KAFKA_CONSUMER_GROUP', 'cdc-cache-sink')
KAFKA_TOPIC_PATTERN = os.getenv('KAFKA_TOPIC_PATTERN', 'cdc-cache.*')
VALKEY_HOST = os.getenv('VALKEY_HOST', 'localhost')
VALKEY_PORT = int(os.getenv('VALKEY_PORT', '6379'))
VALKEY_SSL = os.getenv('VALKEY_SSL', 'false').lower() == 'true'
VALKEY_AUTH_TOKEN = os.getenv('VALKEY_AUTH_TOKEN', '')
KEY_PREFIX = os.getenv('KEY_PREFIX', '')
KEY_PATTERN = os.getenv('KEY_PATTERN', '{table}:{pk}')
WRITE_MODE = os.getenv('WRITE_MODE', 'hset')
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100'))
LWW_ENABLED = os.getenv('LWW_ENABLED', 'true').lower() == 'true'
KAFKA_SECURITY_PROTOCOL = os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT')
KAFKA_SASL_MECHANISM = os.getenv('KAFKA_SASL_MECHANISM', '')
METRICS_PORT = int(os.getenv('METRICS_PORT', '9090'))

running = True

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


def signal_handler(sig, frame):
    global running
    print("Shutdown signal received, stopping...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


async def create_valkey_client():
    """Create Valkey Glide cluster client connection."""
    addresses = [NodeAddress(host=VALKEY_HOST, port=VALKEY_PORT)]

    config_kwargs = {
        'addresses': addresses,
        'use_tls': VALKEY_SSL,
        'request_timeout': 5000,
    }
    if VALKEY_AUTH_TOKEN:
        config_kwargs['credentials'] = ServerCredentials(password=VALKEY_AUTH_TOKEN)

    config = GlideClusterClientConfiguration(**config_kwargs)
    client = await GlideClusterClient.create(config)

    # Verify connection
    result = await client.custom_command(["PING"])
    print(f"Valkey PING response: {result}")
    return client


def extract_key(event, table_name, kafka_key=None):
    """Extract the key for Valkey based on the configured pattern."""
    key_data = None
    if kafka_key:
        if isinstance(kafka_key, dict):
            key_data = kafka_key.get('payload', kafka_key)
        elif isinstance(kafka_key, (str, int, float)):
            key_data = kafka_key

    if not key_data:
        key_data = event.get('key', {})

    if isinstance(key_data, dict):
        pk_parts = [str(v) for v in key_data.values()]
        pk = ':'.join(pk_parts) if pk_parts else 'unknown'
    else:
        pk = str(key_data) if key_data else 'unknown'

    key = KEY_PATTERN.replace('{table}', table_name).replace('{pk}', pk)

    if KEY_PREFIX:
        key = f"{KEY_PREFIX}:{key}"

    return key


async def process_event(valkey_client, topic, event, kafka_key=None):
    """Process a single CDC event and write to Valkey."""
    try:
        topic_parts = topic.split('.')
        table_name = topic_parts[-1] if topic_parts else 'unknown'

        payload = event.get('payload', event)
        op = payload.get('op', 'c')

        if op == 'd':
            key = extract_key(payload, table_name, kafka_key)
            await valkey_client.delete([key])
            return 'delete'

        after = payload.get('after')
        if not after:
            return 'skip'

        key = extract_key(payload, table_name, kafka_key)

        # LWW check - only write if this is newer
        if LWW_ENABLED:
            ts_ms = payload.get('ts_ms', 0)
            try:
                if WRITE_MODE == 'hset':
                    existing_ts = await valkey_client.hget(key, '__ts_ms')
                elif WRITE_MODE == 'json_object':
                    raw = await valkey_client.custom_command(["JSON.GET", key, "$.__ts_ms"])
                    existing_ts = None
                    if raw:
                        try:
                            parsed = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                            existing_ts = parsed[0] if isinstance(parsed, list) and parsed else parsed
                        except (json.JSONDecodeError, AttributeError):
                            existing_ts = None
                else:
                    raw = await valkey_client.get(key)
                    existing_ts = None
                    if raw:
                        try:
                            existing_obj = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                            existing_ts = existing_obj.get('__ts_ms') if isinstance(existing_obj, dict) else None
                        except (json.JSONDecodeError, AttributeError):
                            existing_ts = None
            except Exception:
                existing_ts = None
            if existing_ts:
                existing_val = existing_ts if isinstance(existing_ts, (int, float)) else (existing_ts if isinstance(existing_ts, str) else existing_ts.decode('utf-8'))
                if int(existing_val) > ts_ms:
                    return 'skip_lww'

        # Write based on mode
        if WRITE_MODE == 'hset':
            hash_data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) if v is not None else ''
                        for k, v in after.items()}
            if LWW_ENABLED:
                hash_data['__ts_ms'] = str(payload.get('ts_ms', 0))
            await valkey_client.hset(key, hash_data)
        elif WRITE_MODE == 'json':
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            await valkey_client.set(key, json.dumps(after))
        elif WRITE_MODE == 'json_object':
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            await valkey_client.custom_command(["JSON.SET", key, "$", json.dumps(after)])
        elif WRITE_MODE == 'string':
            await valkey_client.set(key, json.dumps(after))

        return 'write'

    except Exception as e:
        print(f"Error processing event: {e}")
        return 'error'


async def main():
    print(f"CDC-cache Valkey Sink Worker (Glide Cluster) starting...")
    print(f"  Kafka: {KAFKA_BOOTSTRAP_SERVERS}")
    print(f"  Topic Pattern: {KAFKA_TOPIC_PATTERN}")
    print(f"  Valkey: {VALKEY_HOST}:{VALKEY_PORT}")
    print(f"  Write Mode: {WRITE_MODE}")
    print(f"  LWW Enabled: {LWW_ENABLED}")

    # Start metrics HTTP server
    start_metrics_server()
    metrics['start_time'] = time.time()

    # Connect to Valkey
    print("Connecting to Valkey (Glide Cluster)...")
    valkey_client = None
    while running and valkey_client is None:
        try:
            valkey_client = await create_valkey_client()
            print("Connected to Valkey!")
            metrics['status'] = 'connected_valkey'
        except Exception as e:
            print(f"Failed to connect to Valkey: {e}, retrying in 5s...")
            metrics['status'] = 'error_valkey'
            await asyncio.sleep(5)

    if not running:
        return

    # Create Kafka consumer (sync — kafka-python is not async)
    print("Connecting to Kafka...")
    print(f"  Consumer group: {KAFKA_CONSUMER_GROUP}")
    consumer = None
    while running and consumer is None:
        try:
            kafka_kwargs = {
                'bootstrap_servers': KAFKA_BOOTSTRAP_SERVERS.split(','),
                'group_id': KAFKA_CONSUMER_GROUP,
                'auto_offset_reset': 'earliest',
                'enable_auto_commit': True,
                'value_deserializer': lambda m: json.loads(m.decode('utf-8')) if m else None,
                'key_deserializer': lambda m: json.loads(m.decode('utf-8')) if m else None,
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
            import re
            all_topics = consumer.topics()
            pattern_re = re.compile(KAFKA_TOPIC_PATTERN)
            matched_topics = [t for t in all_topics if pattern_re.match(t)]
            if matched_topics:
                consumer.subscribe(topics=matched_topics)
                print(f"Subscribed to {len(matched_topics)} topic(s): {matched_topics}")
                metrics['topics'] = matched_topics
            else:
                print(f"  No topics matched '{KAFKA_TOPIC_PATTERN}' yet, using pattern subscribe...")
                consumer.subscribe(pattern=KAFKA_TOPIC_PATTERN)
                print(f"Subscribed to topics matching: {KAFKA_TOPIC_PATTERN}")
                metrics['topics'] = [KAFKA_TOPIC_PATTERN]
        except Exception as e:
            print(f"Failed to connect to Kafka: {e}, retrying in 5s...")
            metrics['status'] = 'error_kafka'
            await asyncio.sleep(5)

    if not running:
        return

    # Process messages
    stats = {'write': 0, 'delete': 0, 'skip': 0, 'error': 0}
    batch_count = 0
    metrics['status'] = 'running'

    print("Processing messages...")
    while running:
        try:
            messages = consumer.poll(timeout_ms=1000, max_records=BATCH_SIZE)

            for topic_partition, records in messages.items():
                for record in records:
                    if not running:
                        break

                    result = await process_event(valkey_client, record.topic, record.value, record.key)
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

                    if batch_count >= 1000:
                        print(f"Stats: writes={stats['write']}, deletes={stats['delete']}, "
                              f"skips={stats['skip']}, errors={stats['error']}")
                        batch_count = 0

        except Exception as e:
            print(f"Error in main loop: {e}")
            await asyncio.sleep(1)

    # Cleanup
    print("Shutting down...")
    metrics['status'] = 'stopped'
    if consumer:
        consumer.close()
    print(f"Final stats: writes={stats['write']}, deletes={stats['delete']}, "
          f"skips={stats['skip']}, errors={stats['error']}")
    print("Goodbye!")


if __name__ == '__main__':
    asyncio.run(main())
