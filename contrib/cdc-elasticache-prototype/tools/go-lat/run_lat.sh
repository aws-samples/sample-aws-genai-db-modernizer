#!/bin/bash
# Per-event latency: Valkey local stream vs Kafka local topic (both plaintext, per-event).
set -e
RATE=${RATE:-5000}
DURATION=${DURATION:-20}

echo "### Ensure local brokers up ###"
docker start cdc-cache-broker >/dev/null 2>&1 || true
docker exec cdc-cache-broker valkey-cli -p 6379 PING

echo "### Recreate kafka lat-topic (1 partition) ###"
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic lat-topic 2>/dev/null || true
sleep 3
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --create --topic lat-topic --partitions 1 --replication-factor 1 2>&1 | tail -1

echo ""
echo "########## VALKEY ##########"
ENGINE=valkey VALKEY_ADDR=localhost:6380 RATE=$RATE DURATION=$DURATION /opt/go-lat/lat

echo ""
echo "########## KAFKA ##########"
ENGINE=kafka KAFKA=localhost:9092 RATE=$RATE DURATION=$DURATION /opt/go-lat/lat
