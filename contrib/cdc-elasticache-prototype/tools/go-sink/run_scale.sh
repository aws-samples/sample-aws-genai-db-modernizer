#!/bin/bash
# Single-broker / single-partition scale benchmark — clean state each run.
# Env in: COUNT
set -e
COUNT=${COUNT:-1000000}
TGT_HOST="${VALKEY_HOST}"
STREAM=data:ht.Inventory.customers

echo "=== [1/5] Flush BROKER (local) ==="
docker exec cdc-cache-broker valkey-cli -p 6379 FLUSHALL

echo "=== [2/5] Flush TARGET (all shards) ==="
redis-cli --tls --cluster call ${TGT_HOST}:6379 FLUSHALL >/dev/null 2>&1 || true
TGT_BEFORE=$(redis-cli --tls --cluster call ${TGT_HOST}:6379 DBSIZE 2>/dev/null | awk '{if($NF ~ /^[0-9]+$/) s+=$NF} END {print s+0}')
echo "  target dbsize after flush: ${TGT_BEFORE:-0}"

echo "=== [3/5] Generate $COUNT events into $STREAM ==="
BROKER_ADDR=localhost:6380 STREAM=$STREAM COUNT=$COUNT GEN_WORKERS=8 /opt/go-gen/gen
docker exec cdc-cache-broker valkey-cli -p 6379 XGROUP CREATE $STREAM go-timed 0 MKSTREAM 2>/dev/null || true
docker exec cdc-cache-broker valkey-cli -p 6379 XGROUP SETID $STREAM go-timed 0

cd /opt/go-sink
export BROKER_HOST=localhost BROKER_PORT=6380
export TARGET_HOST=$TGT_HOST TARGET_PORT=6379 TARGET_SSL=true TARGET_CLUSTER=true
export WORKERS=16 BATCH_SIZE=20000 CONSUMER_GROUP=go-timed STREAM_KEY=$STREAM
export EVENT_FORMAT=flat STREAM_TABLE=customers

echo "=== [4/5] Drain (16 workers, batch 20000, flat -> 3-shard target) ==="
START=$(date +%s.%N)
./sink-worker > /tmp/scale_$COUNT.log 2>&1 &
PID=$!
for t in $(seq 1 180); do
  sleep 1
  LAG=$(docker exec cdc-cache-broker valkey-cli -p 6379 XINFO GROUPS $STREAM | grep -A1 '^lag' | tail -1 | tr -d '\r')
  PEND=$(docker exec cdc-cache-broker valkey-cli -p 6379 XPENDING $STREAM go-timed 2>/dev/null | head -1 | tr -d '\r')
  if [ "$LAG" = "0" ] && [ "$PEND" = "0" ]; then END=$(date +%s.%N); break; fi
done
END=${END:-$(date +%s.%N)}
kill -INT $PID 2>/dev/null || true
sleep 1

echo "=== [5/5] Verify ==="
TGT_AFTER=$(redis-cli --tls --cluster info ${TGT_HOST}:6379 2>/dev/null | grep -oE '\[OK\] [0-9]+ keys' | grep -oE '[0-9]+')
WALL=$(echo "$END - $START" | bc)
RATE=$(echo "scale=0; $COUNT / $WALL" | bc)
echo "=================================================="
echo " COUNT=$COUNT  WALL=${WALL}s  RATE=${RATE}/sec"
echo " target keys landed=${TGT_AFTER}  (expected $COUNT)"
echo "=================================================="
grep -E "Avg " /tmp/scale_$COUNT.log | tail -4
