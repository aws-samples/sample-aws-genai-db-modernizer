#!/usr/bin/env bash
# DEBEZIUM monitor - ingest rate (broker entries-added/sec, i.e. what Debezium is
# writing to the broker) plus CPU / memory / task count from CloudWatch. Debezium
# exposes no app metrics endpoint; ~100% of one core = the single-log read ceiling.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export MODE=debezium BRK STREAM GROUP
export ECS_CLUSTER="${ECS_CLUSTER:-cdc-cache-demo-cluster}" DBZ_SVC="${DBZ_SVC:-cdc-cache-demo-cdc-debezium}"
export INTERVAL="${INTERVAL:-5}"
exec python3 "$DIR/monitors.py"
