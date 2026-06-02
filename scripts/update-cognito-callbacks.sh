#!/usr/bin/env bash
set -euo pipefail

# Update Cognito User Pool App Client callback URLs
# Usage: ./update-cognito-callbacks.sh add|remove <user-pool-id> <client-id> <callback-url>

ACTION=$1
POOL_ID=$2
CLIENT_ID=$3
CALLBACK_URL=$4

echo "=== Updating Cognito callbacks: $ACTION $CALLBACK_URL ==="

# Get current configuration
CURRENT_CONFIG=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" \
  --client-id "$CLIENT_ID" \
  --query 'UserPoolClient' \
  --output json)

# Extract current callback URLs
CURRENT_CALLBACKS=$(echo "$CURRENT_CONFIG" | jq -r '.CallbackURLs | join(",")')

if [ "$ACTION" = "add" ]; then
  # Add new callback if not already present
  if echo "$CURRENT_CALLBACKS" | grep -q "$CALLBACK_URL"; then
    echo "Callback URL already exists, skipping"
    exit 0
  fi
  NEW_CALLBACKS="${CURRENT_CALLBACKS},${CALLBACK_URL}"
elif [ "$ACTION" = "remove" ]; then
  # Remove callback
  NEW_CALLBACKS=$(echo "$CURRENT_CALLBACKS" | tr ',' '\n' | grep -v "^${CALLBACK_URL}$" | tr '\n' ',' | sed 's/,$//')
  if [ -z "$NEW_CALLBACKS" ]; then
    echo "ERROR: Cannot remove last callback URL"
    exit 1
  fi
else
  echo "ERROR: Invalid action. Use 'add' or 'remove'"
  exit 1
fi

# Build callback URLs as a proper array for the AWS CLI
# Each URL must be a separate argument, not a single space-delimited string
IFS=',' read -ra CALLBACK_ARRAY <<< "$NEW_CALLBACKS"

# Update the app client
aws cognito-idp update-user-pool-client \
  --user-pool-id "$POOL_ID" \
  --client-id "$CLIENT_ID" \
  --callback-urls "${CALLBACK_ARRAY[@]}" \
  --allowed-o-auth-flows code \
  --allowed-o-auth-scopes email openid profile \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers COGNITO

echo "✅ Callback URLs updated successfully"
