#!/usr/bin/env bash
# Source this first:  source ./00_env.sh
# Resolves the pipeline endpoints (source Aurora, target Valkey, broker) and the
# DB secret, then reads the DB user and password from that secret. Order of resolution:
#   1. demo.env in this folder (KEY=VALUE lines), if present
#   2. CloudFormation stack outputs (needs read permissions on this host)
# Export overrides any of: SRC, TGT, BRK, SECRET_ARN, DB, STREAM, GROUP, TABLE.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

if [ -f "$SELF_DIR/demo.env" ]; then
  set -a; . "$SELF_DIR/demo.env"; set +a
fi

_out() { aws cloudformation describe-stacks --stack-name "$1" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null; }

[ -z "${SRC:-}" ] && SRC="$(_out cdc-cache-demo-source ClusterEndpoint)"
[ -z "${TGT:-}" ] && TGT="$(_out cdc-cache-demo-target ClusterEndpoint)"
[ -z "${BRK:-}" ] && BRK="$(_out cdc-cache-demo-broker PrimaryEndpoint)"
[ -z "${SECRET_ARN:-}" ] && SECRET_ARN="$(_out cdc-cache-demo-source MasterUserSecretArn)"
if [ -n "${SECRET_ARN:-}" ] && [ "$SECRET_ARN" != "None" ]; then
  _secret_json="$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text 2>/dev/null)"
  DBUSER="$(printf '%s' "$_secret_json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("username",""))' 2>/dev/null)"
  PW="$(printf '%s' "$_secret_json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("password",""))' 2>/dev/null)"
  unset _secret_json
fi

export SRC TGT BRK PW DBUSER SECRET_ARN
export DB="${DB:-Inventory}"
export STREAM="${STREAM:-data:cdc-cache.Inventory.products}" GROUP="${GROUP:-cdc-cache-sink}" TABLE="${TABLE:-products}"

echo "SRC = ${SRC:-<unset>}"
echo "TGT = ${TGT:-<unset>}"
echo "BRK = ${BRK:-<unset>}"
if [ -n "${PW:-}" ]; then echo "PW  = (set)"; else
  echo "PW  = (MISSING) -> set SECRET_ARN in demo.env (see demo.env.example) and grant Secrets Manager read on this host"; fi
