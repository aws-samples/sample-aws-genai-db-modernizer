#!/usr/bin/env bash
# DEMO 3 - OBSERVABILITY (per-stage visibility).
#   ./03_observe.sh          trace one record through all 4 stages + latency
#   ./03_observe.sh panel    live panel: insert rows elsewhere and watch deltas flow
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
REPO="$(cd "$DIR/.." && pwd)"
MODE="${1:-trace}"
if [ "$MODE" = "panel" ]; then
  BRK="$BRK" TGT="$TGT" SRC="$SRC" PW="$PW" python3 "$REPO/tools/observe.py"
else
  BRK="$BRK" TGT="$TGT" SRC="$SRC" PW="$PW" python3 "$REPO/tools/observe.py" --trace
fi
