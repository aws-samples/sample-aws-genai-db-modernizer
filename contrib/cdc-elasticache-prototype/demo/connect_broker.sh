#!/usr/bin/env bash
# Connect to the BROKER Valkey Streams (cluster mode OFF, no TLS). Interactive if no args.
#   ./connect_broker.sh                                            # interactive prompt
#   ./connect_broker.sh XINFO STREAM data:cdc-cache.Inventory.products  # stream depth / entries-added
#   ./connect_broker.sh XINFO GROUPS data:cdc-cache.Inventory.products  # entries-read, lag, pending
#   ./connect_broker.sh XLEN dlq:data:cdc-cache.Inventory.products      # dead-letters (0 = none)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export VCLI_HOST="$BRK" VCLI_SSL=false VCLI_CLUSTER=false
exec python3 "$DIR/vcli.py" "$@"
