#!/usr/bin/env bash
# Live BULK-LOAD monitor. Run this in a SECOND terminal while an initial load
# (./01_initial_load.sh, or quickstart load) is running. It samples the target
# Valkey key count once a second and prints new-keys-per-second = load rate.
#
#   Terminal A:  ./01_initial_load.sh
#   Terminal B:  ./load_monitor.sh
#
# Note: this counts NEW keys landing in the cache. Re-loading rows that already
# exist overwrites in place and will not increase the count - for the exact
# loader throughput regardless, read the loader's own final "rows/sec" line.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
export TGT
python3 - <<'PY'
import os, time
from valkey.cluster import ValkeyCluster
r = ValkeyCluster(host=os.environ["TGT"], port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
def size():
    d = r.dbsize()
    return sum(d.values()) if isinstance(d, dict) else d
base = prev = size(); t0 = time.time()
print("Watching target key count (Ctrl-C to stop). rows/sec = new keys per second.")
print(f"  start: target keys = {base:,}")
while True:
    time.sleep(1)
    cur = size(); rate = cur - prev; prev = cur
    el = time.time() - t0
    avg = (cur - base) / el if el else 0
    print(f"  t+{el:5.0f}s | target keys={cur:>12,} | +{rate:>8,}/s | avg {avg:>8,.0f}/s | loaded so far +{cur-base:,}", flush=True)
PY
