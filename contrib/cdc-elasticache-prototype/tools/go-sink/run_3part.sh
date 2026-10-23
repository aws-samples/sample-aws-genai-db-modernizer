#!/bin/bash
# 3-partition test: 3 brokers (6380/6391/6392), one worker pool each,
# all writing to the 3-shard target cluster. Measures aggregate throughput.
set -e

TGT_HOST="${VALKEY_HOST}"
PORTS=(6380 6391 6392)

echo "=== Reset: flush target + recreate consumer groups ==="
for i in 0 1 2; do
  P=${PORTS[$i]}
  docker exec $(docker ps --filter "publish=$P" --format '{{.Names}}') valkey-cli XGROUP CREATE part:$i g3 0 MKSTREAM 2>/dev/null || true
  docker exec $(docker ps --filter "publish=$P" --format '{{.Names}}') valkey-cli XGROUP SETID part:$i g3 0
  LEN=$(docker exec $(docker ps --filter "publish=$P" --format '{{.Names}}') valkey-cli XLEN part:$i | tr -d '\r')
  echo "  broker port $P part:$i XLEN=$LEN"
done

cd /opt/go-sink
echo "=== Launch 3 worker pools (5 workers each, batch 20000, flat) ==="
START=$(date +%s.%N)
for i in 0 1 2; do
  P=${PORTS[$i]}
  BROKER_HOST=localhost BROKER_PORT=$P \
  TARGET_HOST=$TGT_HOST TARGET_PORT=6379 TARGET_SSL=true TARGET_CLUSTER=true \
  WORKERS=5 BATCH_SIZE=20000 CONSUMER_GROUP=g3 STREAM_KEY=part:$i \
  EVENT_FORMAT=flat STREAM_TABLE=customers \
  ./sink-worker > /tmp/p$i.log 2>&1 &
  echo $! > /tmp/p$i.pid
done

# Poll all 3 brokers' lag until drained.
for t in $(seq 1 40); do
  sleep 1
  DONE=0
  for i in 0 1 2; do
    P=${PORTS[$i]}
    LAG=$(docker exec $(docker ps --filter "publish=$P" --format '{{.Names}}') valkey-cli XINFO GROUPS part:$i | grep -A1 '^lag' | tail -1 | tr -d '\r')
    [ "$LAG" = "0" ] && DONE=$((DONE+1))
  done
  if [ "$DONE" = "3" ]; then
    END=$(date +%s.%N)
    echo "  ALL DRAINED at t=${t}s"
    break
  fi
done
END=${END:-$(date +%s.%N)}

for i in 0 1 2; do kill -INT $(cat /tmp/p$i.pid) 2>/dev/null || true; done
sleep 1

WALL=$(echo "$END - $START" | bc)
echo "=== WALL TIME: ${WALL}s for 2,000,000 events ==="
echo "=== AGGREGATE: $(echo "scale=0; 2000000 / $WALL" | bc)/sec ==="
echo ""
for i in 0 1 2; do
  echo "--- pool $i (broker ${PORTS[$i]}) ---"
  grep -E "Writes:|Avg " /tmp/p$i.log | tail -6
done
