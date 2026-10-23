#!/bin/bash
# Benchmark run: drain 2M events from local broker -> target, with per-step timing.
# Env in: TGT_HOST TGT_PORT TGT_SSL TGT_CLUSTER LABEL
set -e

: "${TGT_HOST:=localhost}"
: "${TGT_PORT:=6381}"
: "${TGT_SSL:=false}"
: "${TGT_CLUSTER:=false}"
: "${LABEL:=local}"

echo "=== [$LABEL] Resetting state (target=$TGT_HOST:$TGT_PORT cluster=$TGT_CLUSTER) ==="
if [ "$TGT_CLUSTER" = "false" ] && [ "$TGT_HOST" = "localhost" ]; then
  docker exec cdc-cache-bench-target valkey-cli FLUSHALL
fi
docker exec cdc-cache-broker valkey-cli -p 6379 XGROUP CREATE data:ht.Inventory.customers go-timed 0 MKSTREAM 2>/dev/null || true
docker exec cdc-cache-broker valkey-cli -p 6379 XGROUP SETID data:ht.Inventory.customers go-timed 0
echo "Broker XLEN: $(docker exec cdc-cache-broker valkey-cli -p 6379 XLEN data:ht.Inventory.customers)"

echo "=== Running Go sink worker (16 workers, batch ${BATCH_SIZE:-5000}) ==="
cd /opt/go-sink
export BROKER_HOST=localhost BROKER_PORT=6380
export TARGET_HOST=$TGT_HOST TARGET_PORT=$TGT_PORT TARGET_SSL=$TGT_SSL TARGET_CLUSTER=$TGT_CLUSTER
export WORKERS=16 BATCH_SIZE=${BATCH_SIZE:-5000}
export CONSUMER_GROUP=go-timed STREAM_KEY=data:ht.Inventory.customers
export EVENT_FORMAT=${EVENT_FORMAT:-envelope} STREAM_TABLE=customers

./sink-worker > /tmp/bench_out.log 2>&1 &
PID=$!

# Run for a fixed window, sampling broker lag to know when drained.
for i in $(seq 1 30); do
  sleep 2
  LAG=$(docker exec cdc-cache-broker valkey-cli -p 6379 XINFO GROUPS data:ht.Inventory.customers | grep -A1 '^lag' | tail -1 | tr -d '\r')
  echo "  t=$((i*2))s broker_lag=$LAG"
  if [ "$LAG" = "0" ]; then
    echo "  DRAINED at ~$((i*2))s"
    break
  fi
done

kill -INT $PID
sleep 2
echo "=== WORKER OUTPUT ==="
cat /tmp/bench_out.log
