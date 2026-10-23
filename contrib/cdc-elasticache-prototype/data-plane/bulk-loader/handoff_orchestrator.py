#!/usr/bin/env python3
"""
CDC-cache initial-load → CDC handoff orchestrator.

Guarantees a correct cutover from the parallel bulk loader to Debezium CDC
even when the source is being written to DURING the load. It does this by
pinning the CDC start position to a bookmark captured BEFORE the load, so
Debezium replays every change from that point and its (newer) writes land
AFTER the loader's — newest wins, idempotent HSET absorbs the overlap.

Sequence:
  1. CAPTURE  P0 = current change-log position (binlog file:pos / LSN / SCN).
  2. STOP     Debezium (ECS desiredCount -> 0) so it can't move the offset.
  3. SEED     the broker offset key to P0, reusing Debezium's own offset
              structure (we only rewrite file/pos), so Debezium resumes from P0.
  4. LOAD     run the parallel bulk loader (snapshot -> target).
  5. START    Debezium (ECS desiredCount -> 1); it reads the seeded offset and
              streams from P0 forward. The sink applies CDC AFTER the load.

Why this is correct under live writes:
  - Nothing after P0 is missed (CDC replays from P0).
  - CDC writes land after the loader's, so the freshest value wins.
  - Double-applied rows are harmless (HSET table:pk is idempotent).

Only MySQL position capture is implemented here; PostgreSQL/Oracle/SQL Server
capture functions are stubbed with the correct query and return the same
(file/pos-equivalent) tuple. The seed/load/ECS steps are source-agnostic.

Usage (MySQL, against the deployed ECS pipeline):
  python3 handoff_orchestrator.py \
    --source mysql --table orders --pk id --workers 16 \
    --broker-host "$BROKER_HOST" \
    --offset-key cdc-cache:metadata:offsets \
    --ecs-cluster cdc-cache-cluster --debezium-service cdc-cache-cdc-ecs-debezium \
    --region "$AWS_REGION"

  # Dry run: capture + show what would be seeded, change nothing:
  python3 handoff_orchestrator.py --source mysql ... --dry-run
"""
import argparse, json, os, subprocess, sys, time


# ── 1. Position capture (per source) ────────────────────────────────────────
def capture_mysql(cfg):
    import pymysql
    conn = pymysql.connect(host=cfg["host"], port=cfg.get("port", 3306),
                           user=cfg["user"], password=cfg["password"])
    try:
        with conn.cursor() as c:
            c.execute("SHOW MASTER STATUS")
            row = c.fetchone()
            if not row:
                raise RuntimeError("SHOW MASTER STATUS empty — is binlog enabled?")
            file_, pos = row[0], int(row[1])
            c.execute("SELECT @@server_id")
            server_id = int(c.fetchone()[0])
        return {"file": file_, "pos": pos, "server_id": server_id}
    finally:
        conn.close()


def capture_postgresql(cfg):
    # SELECT pg_current_wal_lsn();  -> seed {"lsn_proc":..,"lsn":..} in the PG offset shape.
    raise NotImplementedError("PostgreSQL capture: use pg_current_wal_lsn(); seed the PG offset shape.")


def capture_oracle(cfg):
    # SELECT current_scn FROM v$database;  -> seed {"scn":..} in the Oracle offset shape.
    raise NotImplementedError("Oracle capture: use v$database.current_scn; seed the Oracle offset shape.")


def capture_sqlserver(cfg):
    # SELECT sys.fn_cdc_get_max_lsn();  -> seed {"change_lsn":..} in the SQL Server offset shape.
    raise NotImplementedError("SQL Server capture: use sys.fn_cdc_get_max_lsn(); seed the MSSQL offset shape.")


CAPTURE = {"mysql": capture_mysql, "postgresql": capture_postgresql,
           "oracle": capture_oracle, "sqlserver": capture_sqlserver}


# ── 3. Seed the broker offset key (reuse Debezium's own structure) ───────────
def seed_offset(broker, offset_key, p0, source, dry_run):
    """Rewrite the coordinate inside Debezium's existing offset value.

    We reuse the exact hash field + value shape Debezium already wrote so we
    never hand-craft a version-specific format; we only substitute file/pos
    (MySQL) with the captured P0 and refresh ts_sec.
    """
    h = broker.hgetall(offset_key)
    if not h:
        raise RuntimeError(
            f"No existing offset at '{offset_key}'. Start Debezium once so it "
            f"writes a valid offset, then re-run (we rewrite the coordinate).")
    new_h = {}
    for field, value in h.items():
        v = json.loads(value)
        if source == "mysql":
            v["file"] = p0["file"]
            v["pos"] = p0["pos"]
            v["ts_sec"] = int(time.time())
            v.pop("row", None); v.pop("event", None); v.pop("gtids", None)
        else:
            raise NotImplementedError(f"seed for {source} not implemented")
        new_h[field] = json.dumps(v)
    print(f"  seed: {offset_key}")
    for f, v in new_h.items():
        print(f"    {f} -> {v}")
    if dry_run:
        print("  (dry-run: not written)")
        return
    # Overwrite the hash atomically-ish: delete then set the rewritten fields.
    pipe = broker.pipeline()
    pipe.delete(offset_key)
    pipe.hset(offset_key, mapping=new_h)
    pipe.execute()
    print("  seed: written")


# ── ECS control ──────────────────────────────────────────────────────────────
def ecs_scale(cluster, service, count, region, dry_run):
    print(f"  ecs: {service} -> desiredCount={count}")
    if dry_run:
        print("  (dry-run: not scaled)")
        return
    subprocess.run(
        ["aws", "ecs", "update-service", "--cluster", cluster, "--service", service,
         "--desired-count", str(count), "--region", region],
        check=True, capture_output=True)


def ecs_wait_running(cluster, service, count, region, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = subprocess.run(
            ["aws", "ecs", "describe-services", "--cluster", cluster, "--services", service,
             "--region", region, "--query", "services[0].runningCount", "--output", "text"],
            capture_output=True, text=True)
        running = out.stdout.strip()
        if running == str(count):
            return True
        time.sleep(5)
    return False


# ── Loader ────────────────────────────────────────────────────────────────────
def run_loader(a):
    print("  loader: starting parallel bulk load")
    env = dict(os.environ,
               SOURCE_DB_HOST=a.db_host, SOURCE_DB_PORT=str(a.db_port),
               SOURCE_DB_USER=os.environ["SOURCE_DB_USER"], SOURCE_DB_PASSWORD=os.environ["SOURCE_DB_PASSWORD"],
               SOURCE_DB_NAME=a.db_name,
               VALKEY_HOST=a.target_host, VALKEY_PORT=str(a.target_port),
               VALKEY_SSL=str(a.target_ssl).lower())
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(
        ["python3", os.path.join(here, "bulk_loader.py"),
         "--source", a.source, "--table", a.table, "--pk", a.pk, "--workers", str(a.workers)],
        check=True, env=env)
    print("  loader: done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="mysql", choices=list(CAPTURE))
    ap.add_argument("--table", required=True)
    ap.add_argument("--pk", default="id")
    ap.add_argument("--workers", type=int, default=16)
    # source db
    ap.add_argument("--db-host", required=True)
    ap.add_argument("--db-port", type=int, default=3306)
    # DB user and password: SOURCE_DB_USER / SOURCE_DB_PASSWORD, read from the DB secret
    # by the caller at run time (never on the command line).
    ap.add_argument("--db-name", required=True)
    # target
    ap.add_argument("--target-host", required=True)
    ap.add_argument("--target-port", type=int, default=6379)
    ap.add_argument("--target-ssl", default="true")
    # broker offset
    ap.add_argument("--broker-host", required=True)
    ap.add_argument("--broker-port", type=int, default=6379)
    ap.add_argument("--offset-key", default="cdc-cache:metadata:offsets")
    # ecs
    ap.add_argument("--ecs-cluster", default="cdc-cache-cluster")
    ap.add_argument("--debezium-service", default="cdc-cache-cdc-ecs-debezium")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION"))
    ap.add_argument("--skip-ecs", action="store_true",
                    help="don't scale Debezium (e.g. running it yourself)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import valkey
    broker = valkey.Valkey(host=a.broker_host, port=a.broker_port, decode_responses=True)
    src_cfg = {"host": a.db_host, "port": a.db_port, "user": os.environ["SOURCE_DB_USER"],
               "password": os.environ["SOURCE_DB_PASSWORD"], "database": a.db_name}

    print("=" * 60)
    print("CDC-cache handoff orchestrator")
    print("=" * 60)

    # 1. CAPTURE
    print("[1/5] capture position")
    p0 = CAPTURE[a.source](src_cfg)
    print(f"  P0 = {p0}")

    # 2. STOP Debezium
    if not a.skip_ecs:
        print("[2/5] stop Debezium")
        ecs_scale(a.ecs_cluster, a.debezium_service, 0, a.region, a.dry_run)
        if not a.dry_run and not ecs_wait_running(a.ecs_cluster, a.debezium_service, 0, a.region):
            print("  ERROR: Debezium did not stop in time", file=sys.stderr); sys.exit(1)
    else:
        print("[2/5] stop Debezium — skipped (--skip-ecs)")

    # 3. SEED offset
    print("[3/5] seed broker offset to P0")
    seed_offset(broker, a.offset_key, p0, a.source, a.dry_run)

    # 4. LOAD
    print("[4/5] bulk load")
    if a.dry_run:
        print("  (dry-run: loader not run)")
    else:
        run_loader(a)

    # 5. START Debezium (resumes from P0)
    if not a.skip_ecs:
        print("[5/5] start Debezium (resumes from P0)")
        ecs_scale(a.ecs_cluster, a.debezium_service, 1, a.region, a.dry_run)
    else:
        print("[5/5] start Debezium — skipped (--skip-ecs); start it yourself now")

    print("=" * 60)
    print("Handoff complete. Debezium streams from P0; sink applies CDC after load.")
    print("Run cdc_gate.py to confirm SAFE TO READ before app cutover.")


if __name__ == "__main__":
    main()
