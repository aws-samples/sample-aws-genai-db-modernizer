#!/bin/bash
# Create test user in Cognito User Pool
# Usage: create-test-user.sh <user-pool-id>
# Requires environment variables: TEST_USER_PASSWORD
# Optional: TEST_USER_USERNAME (default: test), TEST_USER_EMAIL (default: test@example.com)

set -e

USER_POOL_ID=$1
EMAIL="${TEST_USER_EMAIL:-test@example.com}"
USERNAME="${TEST_USER_USERNAME:-$EMAIL}"
PASSWORD="${TEST_USER_PASSWORD}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

if [ -z "$USER_POOL_ID" ]; then
  echo "Error: USER_POOL_ID required"
  echo "Usage: $0 <user-pool-id>"
  exit 1
fi

if [ -z "$PASSWORD" ]; then
  echo "Error: TEST_USER_PASSWORD environment variable required"
  echo "  Add TEST_USER_PASSWORD=YourP@ssw0rd! to your .env file"
  exit 1
fi

echo "Creating test user in pool: $USER_POOL_ID (region: $REGION)"
echo "  Username: $USERNAME"
echo "  Email:    $EMAIL"

# Create user
if aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USERNAME" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS \
  --region "$REGION" 2>&1; then
  echo "User created."
else
  echo "User may already exist, continuing..."
fi

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USERNAME" \
  --password "$PASSWORD" \
  --permanent \
  --region "$REGION"

echo "✓ Test user ready: username=$USERNAME password=<from TEST_USER_PASSWORD>"
