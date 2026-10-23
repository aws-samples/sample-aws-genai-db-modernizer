#!/usr/bin/env bash
# Connect to the TARGET Valkey (cluster mode, TLS). Interactive if no args.
#   ./connect_target.sh                          # interactive prompt
#   ./connect_target.sh HGETALL products:700001  # active row (has price_cents)
#   ./connect_target.sh DBSIZE
#   ./connect_target.sh SCAN 0 MATCH 'products:*' COUNT 20
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export VCLI_HOST="$TGT" VCLI_SSL=true VCLI_CLUSTER=true
exec python3 "$DIR/vcli.py" "$@"
