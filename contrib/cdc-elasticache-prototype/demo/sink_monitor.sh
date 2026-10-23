#!/usr/bin/env bash
# SINK monitor - worker/task count, consumed (broker entries-read), rows written
# to the target, and CPU / memory. Counters come from the broker + target in-VPC;
# task count / CPU / memory come from CloudWatch.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export MODE=sink BRK TGT STREAM GROUP
export ECS_CLUSTER="${ECS_CLUSTER:-cdc-cache-demo-cluster}" SINK_SVC="${SINK_SVC:-cdc-cache-demo-cdc-sink}"
export INTERVAL="${INTERVAL:-5}"
exec python3 "$DIR/monitors.py"
