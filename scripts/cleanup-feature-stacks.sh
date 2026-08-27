#!/usr/bin/env bash
# =============================================================================
# Clean up feature branch CloudFormation stacks
#
# Empties S3 buckets, removes Cognito callback, deletes all stacks
# in reverse dependency order.
#
# Usage: ./scripts/cleanup-feature-stacks.sh <sanitized-branch-name>
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/cfn-helpers.sh"

SANITIZED_BRANCH="${1:?Usage: $0 <sanitized-branch-name>}"
FEATURE_STACK="${STACK_NAME_PREFIX}-feat-${SANITIZED_BRANCH}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "========================================="
echo "  Cleaning up: ${SANITIZED_BRANCH}"
echo "  Prefix: ${FEATURE_STACK}"
echo "========================================="

STACK_EXISTS=false
for SUFFIX in ui api-service orchestration storage ecs-infra; do
  if aws cloudformation describe-stacks \
    --stack-name "${FEATURE_STACK}-${SUFFIX}" \
    --region "$REGION" 2>/dev/null; then
    STACK_EXISTS=true
    break
  fi
done

if [ "$STACK_EXISTS" = false ]; then
  echo "No stacks found — nothing to clean up"
  exit 0
fi

# Remove Cognito callback (best effort)
POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME_PREFIX}-dev-auth" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text 2>/dev/null || echo "")
POOL_CLIENT=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME_PREFIX}-dev-auth" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text 2>/dev/null || echo "")
DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name "$DNS_STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`DomainName`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -n "$POOL_ID" ] && [ -n "$POOL_CLIENT" ] && [ -n "$DOMAIN" ]; then
  CALLBACK="https://app-feat-${SANITIZED_BRANCH}.${DOMAIN}/oauth2/idpresponse"
  echo "Removing Cognito callback: ${CALLBACK}"
  "${SCRIPT_DIR}/update-cognito-callbacks.sh" remove \
    "$POOL_ID" "$POOL_CLIENT" "$CALLBACK" 2>/dev/null || true
fi

# Empty S3 buckets before deleting storage stack
echo ""
echo "=== Emptying S3 buckets ==="
for BUCKET_NAME in "${FEATURE_STACK}-storage-bucket" "${FEATURE_STACK}-logs-bucket"; do
  if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
    echo "  Emptying s3://${BUCKET_NAME} (including versions and delete markers)..."
    empty_bucket "$BUCKET_NAME" "$REGION"
    echo "  Done: ${BUCKET_NAME}"
  else
    echo "  Bucket ${BUCKET_NAME} not found (skipping)"
  fi
done

# Delete stacks in reverse dependency order
echo ""
echo "=== Deleting stacks ==="
delete_stack_if_exists "${FEATURE_STACK}-ui"
delete_stack_if_exists "${FEATURE_STACK}-api-service"
delete_stack_if_exists "${FEATURE_STACK}-orchestration"
delete_stack_if_exists "${FEATURE_STACK}-storage"
delete_stack_if_exists "${FEATURE_STACK}-ecs-infra"

echo ""
echo "========================================="
echo "  Cleanup complete: ${SANITIZED_BRANCH}"
echo "========================================="
