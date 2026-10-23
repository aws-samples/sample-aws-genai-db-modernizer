#!/usr/bin/env python3
"""Controlled denormalization test (fan-out). Feeds synthetic Debezium envelope
events to isolated broker streams and checks the target for: Phase 1 embed,
Phase 2 parent-update fan-out, and on_parent_delete blanking.

Assumes the denorm sink is already running against these streams:
  STREAM_KEYS=data:test.customers,data:test.orders
  CONSUMER_GROUP=denorm-test  TRANSFORMS_FILE=/tmp/denorm-transforms.json
"""
import json, time, sys, os
import valkey
from valkey.cluster import ValkeyCluster

BROKER = os.environ.get("BROKER_HOST", "")
TGT = os.environ.get("VALKEY_HOST", "")
S_CUST, S_ORD = "data:test.customers", "data:test.orders"
CID, OID = 9000001, 9000101

b = valkey.Valkey(host=BROKER, port=6379, decode_responses=True)
t = ValkeyCluster(host=TGT, port=6379, ssl=True, ssl_cert_reqs=None, decode_responses=True)


def envelope(table, op, after=None, before=None):
    return json.dumps({"op": op, "source": {"table": table}, "after": after, "before": before})


def add(stream, pk, table, op, after=None, before=None):
    b.xadd(stream, {"key": json.dumps({"id": pk}), "value": envelope(table, op, after, before)})


def wait(fn, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.5)
    return False


def main():
    # clean target only (do NOT delete broker streams here — that would destroy
    # the sink's consumer group; streams are cleaned before the sink starts).
    t.delete(f"customers:{CID}"); t.delete(f"orders:{OID}"); t.delete(f"idx:orders:by:customers:{CID}")
    print("cleaned target keys")

    # 1) parent create, then child create -> embed
    add(S_CUST, CID, "customers", "c", after={"id": CID, "name": "Alice", "tier": "gold"})
    time.sleep(2)
    add(S_ORD, OID, "orders", "c", after={"id": OID, "category": "food", "amount": "5", "customer_id": CID})
    ok = wait(lambda: t.hget(f"orders:{OID}", "cust_name") == "Alice")
    h = t.hgetall(f"orders:{OID}")
    print(f"PHASE1 embed: {'OK' if ok else 'FAIL'} -> {h}")
    idx = t.smembers(f"idx:orders:by:customers:{CID}")
    print(f"reverse index idx:orders:by:customers:{CID} = {idx} ({'OK' if str(OID) in idx else 'FAIL'})")

    # 2) parent update -> fan-out to child
    add(S_CUST, CID, "customers", "u",
        before={"id": CID, "name": "Alice", "tier": "gold"},
        after={"id": CID, "name": "Alicia", "tier": "platinum"})
    ok = wait(lambda: t.hget(f"orders:{OID}", "cust_name") == "Alicia" and t.hget(f"orders:{OID}", "cust_tier") == "platinum")
    print(f"PHASE2 fan-out: {'OK' if ok else 'FAIL'} -> cust_name={t.hget(f'orders:{OID}','cust_name')} cust_tier={t.hget(f'orders:{OID}','cust_tier')}")

    # 3) parent delete -> on_parent_delete=null blanks embedded fields
    add(S_CUST, CID, "customers", "d", before={"id": CID, "name": "Alicia", "tier": "platinum"})
    ok = wait(lambda: t.hget(f"orders:{OID}", "cust_name") == "" and t.hget(f"orders:{OID}", "cust_tier") == "")
    print(f"DELETE policy (null): {'OK' if ok else 'FAIL'} -> {t.hgetall(f'orders:{OID}')}")


if __name__ == "__main__":
    main()
