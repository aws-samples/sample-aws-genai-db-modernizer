#!/usr/bin/env bash
# BROKER monitor - messages read, pending, lag, active consumers, stream depth,
# and DLQ (dead-letter) depth. All read directly from the broker (in-VPC).
# Note: "retried" is a sink-process counter, exposed on the sink task :8081/metrics.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export MODE=broker BRK STREAM GROUP
export INTERVAL="${INTERVAL:-2}"
exec python3 "$DIR/monitors.py"
