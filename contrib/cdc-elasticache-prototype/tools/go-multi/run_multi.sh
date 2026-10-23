#!/bin/bash
# Valkey 16-stream-on-cluster benchmark -> 6-shard target.
# Env in: COUNT STREAMS
set -e
COUNT=${COUNT:-1000000}
STREAMS=${STREAMS:-16}
BROKER="${BROKER_HOST}:6379"
TGT_HOST="${VALKEY_HOST}"

echo "=== [1/4] Flush TARGET (all shards) ==="
redis-cli --tls --cluster call ${TGT_HOST}:6379 FLUSHALL >/dev/null 2>&1 || true

echo "=== [2/4] Produce $COUNT events across $STREAMS streams on broker cluster ==="
BROKER_ADDR=$BROKER COUNT=$COUNT STREAMS=$STREAMS MODE=produce GEN_WORKERS=8 /opt/go-multi/multi

echo "=== [3/4] Consume drain ($STREAMS readers) -> 6-shard target ==="
BROKER_ADDR=$BROKER TARGET_ADDR=${TGT_HOST}:6379 COUNT=$COUNT STREAMS=$STREAMS MODE=consume BATCH_SIZE=20000 STREAM_TABLE=customers /opt/go-multi/multi

echo "=== [4/4] Verify ==="
TGT=$(redis-cli --tls --cluster info ${TGT_HOST}:6379 2>/dev/null | grep -oE '\[OK\] [0-9]+ keys' | grep -oE '[0-9]+')
echo " target keys landed=${TGT} (expected $COUNT)"
