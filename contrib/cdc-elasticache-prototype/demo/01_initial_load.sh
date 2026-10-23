#!/usr/bin/env bash
# DEMO 1 - INITIAL LOAD works (clean, incremental, no truncation).
# Intelligence: read the current MAX(pk), append N new rows after it (~90%
# active / ~10% inactive), then bulk-load ONLY that new PK range. Loading just
# the fresh range means the loader reports real rows/sec and the target gains
# net-new keys, so load_monitor.sh shows a real climb. Uses a dedicated
# 'catalog' table that Debezium does NOT watch, so it's a pure bulk-load demo.
#
#   DEMO_ROWS=100000 ./01_initial_load.sh     # rows to append (default 100000)
#   Run ./load_monitor.sh in a second terminal to watch rows/sec climb.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$DIR/00_env.sh"
REPO="$(cd "$DIR/.." && pwd)"
export SRC TGT BRK PW DBUSER DB
export DEMO_TABLE="${DEMO_TABLE:-catalog}"
# Row count: first positional arg wins, then DEMO_ROWS env, then default.
export DEMO_ROWS="${1:-${DEMO_ROWS:-100000}}"
# Private temp file for the new PK range (mktemp = current user owns it; avoids
# collisions with files a different user may have created in /tmp).
RANGE_FILE="$(mktemp)"; export RANGE_FILE
trap 'rm -f "$RANGE_FILE"' EXIT

echo; echo ">>> 1/3  Read MAX(pk) and append ${DEMO_ROWS} new rows to '${DEMO_TABLE}' (~90% active, ~10% inactive)"
python3 - <<'PY'
import os, pymysql
N = int(os.environ["DEMO_ROWS"]); T = os.environ["DEMO_TABLE"]
c = pymysql.connect(host=os.environ["SRC"], user=os.environ["DBUSER"], password=os.environ["PW"],
                    database=os.environ["DB"], autocommit=True); cur = c.cursor()
cur.execute(f"CREATE TABLE IF NOT EXISTS {T} (id INT PRIMARY KEY, sku VARCHAR(64), name VARCHAR(128), category VARCHAR(64), price DECIMAL(10,2), status VARCHAR(16))")
cur.execute(f"SELECT COALESCE(MAX(id),0) FROM {T}"); base = int(cur.fetchone()[0])
lo = base + 1; hi = base + N
CH = 10000; done = 0
while done < N:
    n = min(CH, N - done)
    rows = [(base + done + i, f"SKU{base+done+i}", f"Item {base+done+i}", "demo",
             round(1.0 + ((base + done + i) % 1000) * 0.01, 2),
             ("inactive" if (base + done + i) % 10 == 0 else "active")) for i in range(1, n + 1)]
    cur.executemany(f"INSERT INTO {T} (id,sku,name,category,price,status) VALUES (%s,%s,%s,%s,%s,%s)", rows)
    done += n
cur.execute(f"SELECT COUNT(*) FROM {T} WHERE id BETWEEN %s AND %s AND status='active'", (lo, hi))
active = cur.fetchone()[0]
open(os.environ["RANGE_FILE"], "w").write(f"{lo} {hi}\n")
print(f"   MAX(pk) was {base:,}; appended rows {lo:,}..{hi:,} ({active:,} active, {N-active:,} inactive)")
PY
read LO HI < "$RANGE_FILE"
echo "   new range: $LO..$HI"

echo; echo ">>> 2/3  Bulk-load ONLY the new range [$LO..$HI] into Valkey WITH transforms (filter active + computed price_cents)"
echo "         (tip: run ./load_monitor.sh in another terminal to watch rows/sec)"
( cd "$REPO/data-plane/bulk-loader"
  SOURCE_DB_HOST="$SRC" SOURCE_DB_PORT=3306 SOURCE_DB_USER="$DBUSER" SOURCE_DB_PASSWORD="$PW" SOURCE_DB_NAME="$DB" \
  VALKEY_HOST="$TGT" VALKEY_PORT=6379 VALKEY_SSL=true \
  python3 bulk_loader.py --source mysql --table "$DEMO_TABLE" --pk id \
    --min-pk "$LO" --max-pk "$HI" \
    --transforms "$DIR/demo_transforms.json" )

echo; echo ">>> 3/3  Verify (active -> cached with price_cents; inactive -> absent)"
LO="$LO" python3 - <<'PY'
import os
from valkey.cluster import ValkeyCluster
T = os.environ["DEMO_TABLE"]; lo = int(os.environ["LO"])
r = ValkeyCluster(host=os.environ["TGT"], port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)
# lo is base+1; find the first active id (skip if lo%10==0) and first inactive id
for off in (0, 1, 8, 9):
    pid = lo + off
    v = r.hgetall(f"{T}:{pid}")
    tag = "INACTIVE" if pid % 10 == 0 else "ACTIVE  "
    print(f"   [{tag}] {T}:{pid} ->", v if v else "(absent - correctly filtered)")
PY
echo; echo ">>> INITIAL LOAD demo complete: ~90% of the new rows cached with computed price_cents; inactive filtered."
