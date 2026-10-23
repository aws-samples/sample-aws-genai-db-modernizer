#!/bin/bash
# Kafka 16-partition sink benchmark -> same 6-shard Valkey target.
# Env in: COUNT
set -e
COUNT=${COUNT:-1000000}
PARTS=${PARTS:-16}
CONS=${CONS:-16}
TOPIC=${TOPIC:-cdc-customers}
TGT_HOST="${VALKEY_HOST}"
KB="docker exec kafka"

echo "=== [1/5] Recreate topic ($TOPIC, $PARTS partitions) ==="
$KB kafka-topics --bootstrap-server localhost:9092 --delete --topic $TOPIC 2>/dev/null || true
sleep 3
$KB kafka-topics --bootstrap-server localhost:9092 --create --topic $TOPIC --partitions $PARTS --replication-factor 1 2>&1 | tail -1
$KB kafka-consumer-groups --bootstrap-server localhost:9092 --delete --group g-kafka 2>/dev/null || true

echo "=== [2/5] Flush TARGET (all shards) ==="
redis-cli --tls --cluster call ${TGT_HOST}:6379 FLUSHALL >/dev/null 2>&1 || true

echo "=== [3/5] Produce $COUNT events ==="
KAFKA=localhost:9093 KAFKA_TLS=true TOPIC=$TOPIC COUNT=$COUNT MODE=produce PARTITIONS=$PARTS PROD_WORKERS=8 /opt/go-kafka/kafkabench

echo "=== [4/5] Consume drain -> 6-shard target ==="
KAFKA=localhost:9093 KAFKA_TLS=true TOPIC=$TOPIC COUNT=$COUNT MODE=consume CONSUMERS=$CONS BATCH_SIZE=20000 \
  TARGET_HOST=$TGT_HOST TARGET_PORT=6379 TARGET_SSL=true STREAM_TABLE=customers \
  /opt/go-kafka/kafkabench

echo "=== [5/5] Verify ==="
TGT=$(redis-cli --tls --cluster info ${TGT_HOST}:6379 2>/dev/null | grep -oE '\[OK\] [0-9]+ keys' | grep -oE '[0-9]+')
echo " target keys landed=${TGT} (expected $COUNT)"
