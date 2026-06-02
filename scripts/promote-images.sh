#!/usr/bin/env bash
# =============================================================================
# Promote Docker images from dev ECR to prod ECR
#
# Copies the image tagged with DOCKER_IMAGE_TAG from each dev repo to the
# corresponding prod repo using the same tag. Uses skopeo to copy images
# without Docker daemon.
#
# Usage:
#   ./scripts/promote-images.sh
#
# Requires:
#   - skopeo installed
#   - scripts/cfn-helpers.sh sourced before calling
#   - STACK_NAME_PREFIX, AWS_DEFAULT_REGION, AWS_ACCOUNT_ID, DOCKER_IMAGE_TAG env vars set
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/cfn-helpers.sh"

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID="${AWS_ACCOUNT_ID}"
SERVICES=("api" "agent" "ui")
TAG="${DOCKER_IMAGE_TAG:?DOCKER_IMAGE_TAG must be set}"

echo "========================================="
echo "  Promoting images: dev → prod"
echo "  Tag: $TAG"
echo "========================================="

# Login to ECR for skopeo
echo "Logging in to ECR..."
aws ecr get-login-password --region "$REGION" | \
  skopeo login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

for SERVICE in "${SERVICES[@]}"; do
  DEV_REPO="${STACK_NAME_PREFIX}-dev-${SERVICE}"
  PROD_REPO="${STACK_NAME_PREFIX}-prod-${SERVICE}"

  # Verify the image exists in dev with the expected tag
  EXISTING_DEV=$(aws ecr describe-images \
    --repository-name "$DEV_REPO" \
    --region "$REGION" \
    --image-ids imageTag="$TAG" \
    --query 'imageDetails[0].imageTags[0]' \
    --output text 2>/dev/null || echo "None")

  if [ "$EXISTING_DEV" = "None" ]; then
    echo "ERROR: Image $DEV_REPO:$TAG not found in dev ECR"
    exit 1
  fi

  echo "Promoting $SERVICE: $DEV_REPO:$TAG → $PROD_REPO:$TAG"

  # Check if tag already exists in prod (immutable tags — skip if present)
  EXISTING=$(aws ecr describe-images \
    --repository-name "$PROD_REPO" \
    --region "$REGION" \
    --image-ids imageTag="$TAG" \
    --query 'imageDetails[0].imageTags[0]' \
    --output text 2>/dev/null || echo "None")

  if [ "$EXISTING" != "None" ]; then
    echo "  Already exists in $PROD_REPO:$TAG — skipping"
  else
    DEV_IMAGE="docker://${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${DEV_REPO}:${TAG}"
    PROD_IMAGE="docker://${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROD_REPO}:${TAG}"

    echo "  Copying $DEV_IMAGE → $PROD_IMAGE..."
    skopeo copy --all "$DEV_IMAGE" "$PROD_IMAGE"
    echo "  Done"
  fi
done

echo ""
echo "========================================="
echo "  All images promoted to prod"
echo "========================================="
