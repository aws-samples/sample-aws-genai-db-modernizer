#!/bin/bash
# Run the benchmark and sample per-thread CPU of the broker valkey-server.
set -e

echo "=== Reset + start worker (3-shard target, flat, batch 20000) ==="
docker exec cdc-cache-broker valkey-cli -p 6379 XGROUP SETID data:ht.Inventory.customers go-timed 0
echo "Broker XLEN: $(docker exec cdc-cache-broker valkey-cli -p 6379 XLEN data:ht.Inventory.customers)"

cd /opt/go-sink
export BROKER_HOST=localhost BROKER_PORT=6380
export TARGET_HOST="${VALKEY_HOST}"
export TARGET_PORT=6379 TARGET_SSL=true TARGET_CLUSTER=true
export WORKERS=16 BATCH_SIZE=20000
export CONSUMER_GROUP=go-timed STREAM_KEY=data:ht.Inventory.customers
export EVENT_FORMAT=flat STREAM_TABLE=customers
./sink-worker > /tmp/bench_out.log 2>&1 &
PID=$!

BPID=$(docker inspect -f '{{.State.Pid}}' cdc-cache-broker)
echo "=== Broker valkey-server host PID: $BPID ==="
echo "=== Per-thread CPU (top -H), name=valkey thread, COMMAND shows thread role ==="
# Sample per-thread CPU 5 times at 1s intervals while draining.
top -bH -d 1 -n 5 -p $BPID | grep -E "PID|valkey|redis|io_thr|^ *[0-9]" | grep -v "top -" || true

echo "=== docker stats snapshot (CPU% : 100% = 1 core) ==="
docker stats --no-stream --format "{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}}" cdc-cache-broker

kill -INT $PID 2>/dev/null || true
sleep 1
echo "=== Worker result ==="
grep -E "Writes:|Avg " /tmp/bench_out.log | tail -8
