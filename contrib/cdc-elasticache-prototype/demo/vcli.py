#!/usr/bin/env python3
"""Tiny valkey-cli replacement (the EC2 has no valkey-cli, but the valkey python
lib is installed). Connects using env set by the connect_* wrappers:
  VCLI_HOST, VCLI_SSL=true|false, VCLI_CLUSTER=true|false
Usage:
  python3 vcli.py                     # interactive prompt
  python3 vcli.py HGETALL products:1  # run one command and print the result
"""
import os, sys, shlex
import valkey

HOST = os.environ["VCLI_HOST"]
SSL = os.environ.get("VCLI_SSL", "false") == "true"
CLUSTER = os.environ.get("VCLI_CLUSTER", "false") == "true"

if CLUSTER:
    from valkey.cluster import ValkeyCluster
    r = ValkeyCluster(host=HOST, port=6379, ssl=SSL, ssl_cert_reqs=None, decode_responses=True)
else:
    r = valkey.Valkey(host=HOST, port=6379, ssl=SSL, ssl_cert_reqs=None, decode_responses=True)


def fmt(v, indent=0):
    pad = "  " * indent
    if isinstance(v, dict):
        return "\n".join(f"{pad}{k} = {v[k]}" for k in v) or f"{pad}(empty)"
    if isinstance(v, (list, tuple)):
        if not v:
            return f"{pad}(empty list)"
        return "\n".join(f"{pad}{i+1}) {fmt(x).strip() if not isinstance(x,(list,dict)) else chr(10)+fmt(x,indent+1)}" for i, x in enumerate(v))
    return f"{pad}{v}"


def run(argv):
    try:
        print(fmt(r.execute_command(*argv)))
    except Exception as e:
        print("ERR", e)


if len(sys.argv) > 1:
    run(sys.argv[1:])
else:
    prompt = HOST.split(".")[0] + "> "
    print(f"connected to {HOST} (ssl={SSL} cluster={CLUSTER}). type 'exit' to quit.")
    while True:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        run(shlex.split(line))
