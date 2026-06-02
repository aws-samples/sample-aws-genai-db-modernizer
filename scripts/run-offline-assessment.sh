#!/usr/bin/env bash
# =============================================================================
# Run offline assessment via the API (no Cognito required on dev-local ALB)
#
# Usage:
#   ./scripts/run-offline-assessment.sh collector-output-fresh.json
#   ./scripts/run-offline-assessment.sh collector-output-fresh.json --count 10
#
# Env vars:
#   API_URL    — API base URL (default: dev-local ALB)
#   DB_NAME    — database name (default: forum_db)
#   DB_TYPE    — source database type (default: mysql)
# =============================================================================

set -euo pipefail

API_URL="${API_URL:-https://api-dev.your-domain.example.com}"
DB_NAME="${DB_NAME:-forum_db}"
DB_TYPE="${DB_TYPE:-mysql}"
FILE="${1:?Usage: $0 <collector-output.json> [--count N]}"
COUNT=1

# Parse --count flag
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --count) COUNT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ ! -f "$FILE" ]; then
  echo "❌ File not found: $FILE"
  exit 1
fi

FILE_SIZE=$(wc -c < "$FILE" | tr -d ' ')
echo "============================================="
echo " Offline Assessment Runner"
echo " API:      $API_URL"
echo " File:     $FILE ($FILE_SIZE bytes)"
echo " Database: $DB_TYPE / $DB_NAME"
echo " Count:    $COUNT"
echo "============================================="

for i in $(seq 1 "$COUNT"); do
  echo ""
  echo ">>> Assessment $i of $COUNT"

  # Step 1: Prepare — get job_id + presigned URL
  echo "  [1/4] Preparing upload..."
  PREPARE=$(curl -sk -X POST "$API_URL/api/v1/assessments/prepare" \
    -H "Content-Type: application/json" \
    -d "{\"database_name\": \"$DB_NAME\", \"source_database_type\": \"$DB_TYPE\"}")

  JOB_ID=$(echo "$PREPARE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
  UPLOAD_URL=$(echo "$PREPARE" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")
  UPLOAD_KEY=$(echo "$PREPARE" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_key'])")

  echo "  Job ID: $JOB_ID"

  # Step 2: Upload file to S3 via presigned URL
  echo "  [2/4] Uploading to S3..."
  HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" -X PUT "$UPLOAD_URL" \
    -H "Content-Type: application/json" \
    --data-binary "@$FILE")

  if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "  Upload OK ($HTTP_CODE)"
  else
    echo "  ❌ Upload failed ($HTTP_CODE)"
    continue
  fi

  # Step 3: Confirm upload
  echo "  [3/4] Confirming upload..."
  CONFIRM=$(curl -sk -X POST "$API_URL/api/v1/assessments/$JOB_ID/uploads/confirm?database_name=$DB_NAME" \
    -H "Content-Type: application/json" -d "{}")
  echo "  $(echo "$CONFIRM" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Confirmed: {d.get(\"filename\",\"?\")} ({d.get(\"size_bytes\",0)} bytes)')" 2>/dev/null || echo "$CONFIRM")"

  # Step 4: Start assessment
  echo "  [4/4] Starting assessment..."
  RESULT=$(curl -sk -X POST "$API_URL/api/v1/assessments" \
    -H "Content-Type: application/json" \
    -d "{
      \"job_id\": \"$JOB_ID\",
      \"source_database_type\": \"$DB_TYPE\",
      \"database_name\": \"$DB_NAME\",
      \"collection_mode\": \"offline\",
      \"offline_s3_key\": \"$UPLOAD_KEY\",
      \"target_databases\": [\"dynamodb\", \"documentdb\", \"opensearch\"],
      \"full_analysis\": false
    }")

  STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
  echo "  ✅ Assessment started: $JOB_ID (status: $STATUS)"
done

echo ""
echo "============================================="
echo " All $COUNT assessments submitted"
echo "============================================="
