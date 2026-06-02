#!/bin/bash
# Create test user in Cognito User Pool
# Usage: create-test-user.sh <user-pool-id>
# Requires environment variables: TEST_USER_USERNAME, TEST_USER_PASSWORD, TEST_USER_EMAIL

set -e

USER_POOL_ID=$1
USERNAME="${TEST_USER_USERNAME:-test}"
PASSWORD="${TEST_USER_PASSWORD}"
EMAIL="${TEST_USER_EMAIL:-test@example.com}"

if [ -z "$USER_POOL_ID" ]; then
  echo "Error: USER_POOL_ID required"
  echo "Usage: $0 <user-pool-id>"
  exit 1
fi

if [ -z "$PASSWORD" ]; then
  echo "Error: TEST_USER_PASSWORD environment variable required"
  exit 1
fi

echo "Creating test user in pool: $USER_POOL_ID"

# Create user (idempotent - fails silently if exists)
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USERNAME" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS \
  --region "$AWS_REGION" 2>/dev/null || echo "User already exists"

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USERNAME" \
  --password "$PASSWORD" \
  --permanent \
  --region "$AWS_REGION"

echo "✓ Test user ready: username=$USERNAME"
