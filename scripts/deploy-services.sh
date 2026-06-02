#!/usr/bin/env bash
# =============================================================================
# Deploy all service stacks for a given environment
#
# Usage:
#   ./scripts/deploy-services.sh \
#     --env dev \
#     --stack-prefix modernizer-dev \
#     --core-stack modernizer-dev \
#     --desired-count 1 \
#     [--ecr-env dev] \
#     [--ecr-stack modernizer-dev] \
#     [--kms-key ARN] \
#     [--logs-bucket NAME] \
#     [--create-test-user]
#
# Requires:
#   - scripts/cfn-helpers.sh sourced before calling
#   - STACK_NAME_PREFIX, DNS_STACK_NAME env vars set
# =============================================================================

set -euo pipefail

# Load helpers (get_output, deploy_stack, etc.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/cfn-helpers.sh"

# Parse arguments
CREATE_TEST_USER=false
KMS_KEY=""
LOGS_BUCKET="" # gitleaks:allow
ECR_ENV=""
AUTH_STACK_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --env)            ENV="$2"; shift 2 ;;
    --stack-prefix)   STACK_PREFIX="$2"; shift 2 ;;
    --core-stack)     CORE_STACK="$2"; shift 2 ;;
    --desired-count)  DESIRED_COUNT="$2"; shift 2 ;;
    --ecr-env)        ECR_ENV="$2"; shift 2 ;;
    --auth-stack)     AUTH_STACK_OVERRIDE="$2"; shift 2 ;;
    --kms-key)        KMS_KEY="$2"; shift 2 ;;
    --logs-bucket)    LOGS_BUCKET="$2"; shift 2 ;;
    --create-test-user) CREATE_TEST_USER=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Default ECR env to ENV if not specified (dev/prod match their ECR repos)
ECR_ENV="${ECR_ENV:-$ENV}"

# Stack names
if [ -n "$AUTH_STACK_OVERRIDE" ]; then
  AUTH_STACK="$AUTH_STACK_OVERRIDE"
  REUSE_AUTH=true
else
  AUTH_STACK="${STACK_PREFIX}-auth"
  REUSE_AUTH=false
fi
API_STACK="${STACK_PREFIX}-api-service"
STORAGE_STACK="${STACK_PREFIX}-storage"
ORCH_STACK="${STACK_PREFIX}-orchestration"
UI_STACK="${STACK_PREFIX}-ui"

# Resolve shared outputs
VPC_ID=$(get_output "$CORE_STACK" "VpcId")
PUBLIC_1=$(get_output "$CORE_STACK" "PublicSubnet1Id")
PUBLIC_2=$(get_output "$CORE_STACK" "PublicSubnet2Id")
PRIVATE_1=$(get_output "$CORE_STACK" "PrivateAppSubnet1Id")
PRIVATE_2=$(get_output "$CORE_STACK" "PrivateAppSubnet2Id")
ECR_URI=$(get_output "$CORE_STACK" "EcrRepositoryUri")
ECR_AGENT_URI=$(get_output "$CORE_STACK" "EcrRepositoryAgentUri")
ECR_UI_URI=$(get_output "$CORE_STACK" "EcrRepositoryUiUri")
ECR_AGENT_LOAD_TEST_URI=$(get_output "$CORE_STACK" "EcrRepositoryAgentLoadTestUri" 2>/dev/null || echo "")
CERT_ARN=$(get_output "$DNS_STACK_NAME" "CertificateArn")
DOMAIN=$(get_output "$DNS_STACK_NAME" "DomainName")
ZONE_ID=$(get_output "$DNS_STACK_NAME" "HostedZoneId")

# Image tag: use DOCKER_IMAGE_TAG from CI if available, otherwise query ECR
if [ -n "${DOCKER_IMAGE_TAG:-}" ]; then
  API_IMAGE_TAG="$DOCKER_IMAGE_TAG"
  AGENT_IMAGE_TAG="$DOCKER_IMAGE_TAG"
  UI_IMAGE_TAG="$DOCKER_IMAGE_TAG"
else
  # Fallback: get latest image tags from ECR (for local/manual deploys)
  API_IMAGE_TAG=$(get_latest_image_tag "${STACK_NAME_PREFIX}-${ECR_ENV}-api")
  AGENT_IMAGE_TAG=$(get_latest_image_tag "${STACK_NAME_PREFIX}-${ECR_ENV}-agent")
  UI_IMAGE_TAG=$(get_latest_image_tag "${STACK_NAME_PREFIX}-${ECR_ENV}-ui")
fi

# Verify image exists in ECR — fail loudly if not found. The pipeline is
# responsible for building images; a missing tag here means the build failed
# or was skipped, and silently deploying stale code hides real problems.
require_image() {
  local repo="$1" tag="$2" label="$3"
  if aws ecr describe-images --repository-name "$repo" --image-ids imageTag="$tag" \
      --region "${AWS_DEFAULT_REGION:-us-east-1}" > /dev/null 2>&1; then
    echo "$tag"
  else
    echo "❌ $label image $tag not found in ECR repository $repo" >&2
    echo "   This usually means the corresponding build job failed or was skipped." >&2
    echo "   Check the build-backend / build-frontend / build-agent pipeline jobs." >&2
    exit 1
  fi
}

API_IMAGE_TAG=$(require_image "${STACK_NAME_PREFIX}-${ECR_ENV}-api" "$API_IMAGE_TAG" "API")
AGENT_IMAGE_TAG=$(require_image "${STACK_NAME_PREFIX}-${ECR_ENV}-agent" "$AGENT_IMAGE_TAG" "Agent")
UI_IMAGE_TAG=$(require_image "${STACK_NAME_PREFIX}-${ECR_ENV}-ui" "$UI_IMAGE_TAG" "UI")

# Load test image is optional — not all envs have it built yet
LOAD_TEST_IMAGE=""
if [ -n "$ECR_AGENT_LOAD_TEST_URI" ]; then
  LOAD_TEST_REPO="${STACK_NAME_PREFIX}-${ECR_ENV}-agent-load-test"
  if [ -n "${DOCKER_IMAGE_TAG:-}" ]; then
    LOAD_TEST_TAG="$DOCKER_IMAGE_TAG"
  else
    LOAD_TEST_TAG=$(get_latest_image_tag "$LOAD_TEST_REPO" 2>/dev/null || echo "")
  fi
  if [ -n "$LOAD_TEST_TAG" ] && \
     aws ecr describe-images --repository-name "$LOAD_TEST_REPO" --image-ids imageTag="$LOAD_TEST_TAG" \
       --region "${AWS_DEFAULT_REGION:-us-east-1}" > /dev/null 2>&1; then
    LOAD_TEST_IMAGE="$ECR_AGENT_LOAD_TEST_URI:$LOAD_TEST_TAG"
  fi
fi

echo "Image tags:"
echo "  API:        $API_IMAGE_TAG"
echo "  Agent:      $AGENT_IMAGE_TAG"
echo "  UI:         $UI_IMAGE_TAG"
echo "  Load Test:  ${LOAD_TEST_IMAGE:-<not available>}"

# Optional outputs (not all stacks have KMS/logs)
if [ -z "$KMS_KEY" ]; then
  KMS_KEY=$(get_output "$CORE_STACK" "CoreInfraKmsKeyArn" 2>/dev/null || echo "")
fi
if [ -z "$LOGS_BUCKET" ]; then
  LOGS_BUCKET=$(get_output "$CORE_STACK" "CentralLogsBucketName" 2>/dev/null || echo "")
fi

echo "========================================="
echo "  Deploying services for: $ENV"
echo "  Stack prefix: $STACK_PREFIX"
echo "  Core stack: $CORE_STACK"
echo "  Domain: $DOMAIN"
echo "  Reuse auth: $REUSE_AUTH"
echo "========================================="

# 1. ECS Infrastructure - shared cluster and security groups
ECS_INFRA_STACK="${STACK_PREFIX}-ecs-infra"
echo "=== Deploying ECS infrastructure: $ECS_INFRA_STACK ==="
deploy_stack "$ECS_INFRA_STACK" "ecs-infrastructure.yaml" \
  Environment="$ENV" \
  ProjectName="$STACK_NAME_PREFIX" \
  VpcId="$VPC_ID"

# 1. Auth - deploy or reuse existing
if [ "$REUSE_AUTH" = true ]; then
  echo "=== Reusing existing auth stack: $AUTH_STACK ==="

  # Add callback URL to existing Cognito pool
  POOL_ID=$(get_output "$AUTH_STACK" "UserPoolId")
  POOL_CLIENT=$(get_output "$AUTH_STACK" "UserPoolClientId")
  API_CALLBACK_URL="https://app-${ENV}.${DOMAIN}/oauth2/idpresponse"

  ./scripts/update-cognito-callbacks.sh add "$POOL_ID" "$POOL_CLIENT" "$API_CALLBACK_URL"
else
  echo "=== Deploying auth stack: $AUTH_STACK ==="
  deploy_stack "$AUTH_STACK" "auth.yaml" \
    Environment="$ENV" \
    ProjectName="$STACK_NAME_PREFIX" \
    CallbackUrl="https://app-${ENV}.${DOMAIN}/oauth2/idpresponse"
fi

POOL_ARN=$(get_output "$AUTH_STACK" "UserPoolArn")
POOL_CLIENT=$(get_output "$AUTH_STACK" "UserPoolClientId")
POOL_DOMAIN=$(get_output "$AUTH_STACK" "UserPoolDomainPrefix")

if [ "$CREATE_TEST_USER" = true ]; then
  POOL_ID=$(get_output "$AUTH_STACK" "UserPoolId")
  ./scripts/create-test-user.sh "$POOL_ID"
fi

# 3. Storage
STORAGE_PARAMS=(
  Environment="$ENV"
  ProjectName="$STACK_NAME_PREFIX"
)
[ -n "$KMS_KEY" ] && STORAGE_PARAMS+=(KmsKeyArn="$KMS_KEY")
[ -n "$DOMAIN" ] && STORAGE_PARAMS+=(AppDomainName="app-${ENV}.${DOMAIN}")

deploy_stack "$STORAGE_STACK" "storage.yaml" "${STORAGE_PARAMS[@]}"

# 4. Orchestration
ORCH_PARAMS=(
  Environment="$ENV"
  ProjectName="$STACK_NAME_PREFIX"
  EcsClusterArn="$(get_output "$ECS_INFRA_STACK" ClusterArn)"
  EcsSecurityGroupId="$(get_output "$ECS_INFRA_STACK" EcsSecurityGroupId)"
  PrivateAppSubnet1Id="$PRIVATE_1"
  PrivateAppSubnet2Id="$PRIVATE_2"
  ContainerImage="$ECR_AGENT_URI:$AGENT_IMAGE_TAG"
  TaskExecutionRoleArn="$(get_output "$ECS_INFRA_STACK" TaskExecutionRoleArn)"
  S3BucketName="$(get_output "$STORAGE_STACK" S3BucketName)"
  DynamoDbTableName="$(get_output "$STORAGE_STACK" DynamoDbTableName)"
)
[ -n "$KMS_KEY" ] && ORCH_PARAMS+=(KmsKeyArn="$KMS_KEY")
[ -n "$LOAD_TEST_IMAGE" ] && ORCH_PARAMS+=(LoadTestContainerImage="$LOAD_TEST_IMAGE")

deploy_stack "$ORCH_STACK" "orchestration.yaml" "${ORCH_PARAMS[@]}"

# 5. API service - now with all backend dependencies available
echo "=== Deploying API service: $API_STACK ==="
API_PARAMS=(
  Environment="$ENV"
  ProjectName="$STACK_NAME_PREFIX"
  VpcId="$VPC_ID"
  PublicSubnet1Id="$PUBLIC_1"
  PublicSubnet2Id="$PUBLIC_2"
  PrivateAppSubnet1Id="$PRIVATE_1"
  PrivateAppSubnet2Id="$PRIVATE_2"
  EcsClusterArn="$(get_output "$ECS_INFRA_STACK" ClusterArn)"
  EcsSecurityGroupId="$(get_output "$ECS_INFRA_STACK" EcsSecurityGroupId)"
  TaskExecutionRoleArn="$(get_output "$ECS_INFRA_STACK" TaskExecutionRoleArn)"
  ContainerImage="$ECR_URI:$API_IMAGE_TAG"
  DesiredCount="$DESIRED_COUNT"
  CertificateArn="$CERT_ARN"
  UserPoolArn="$POOL_ARN"
  UserPoolClientId="$POOL_CLIENT"
  UserPoolDomain="$POOL_DOMAIN"
  CommitSha="$API_IMAGE_TAG"
  HostedZoneId="$ZONE_ID"
  DomainName="api-${ENV}.${DOMAIN}"
  AppDomainName="app-${ENV}.${DOMAIN}"
  StateMachineArn="$(get_output "$ORCH_STACK" StateMachineArn)"
  S3BucketName="$(get_output "$STORAGE_STACK" S3BucketName)"
)
[ -n "$KMS_KEY" ] && API_PARAMS+=(KmsKeyArn="$KMS_KEY")
[ -n "$LOGS_BUCKET" ] && API_PARAMS+=(LogsBucketName="$LOGS_BUCKET")

deploy_stack "$API_STACK" "api-service.yaml" "${API_PARAMS[@]}"

# 6. UI
echo "=== Deploying UI service: $UI_STACK ==="
UI_PARAMS=(
  Environment="$ENV"
  ProjectName="$STACK_NAME_PREFIX"
  VpcId="$VPC_ID"
  PrivateAppSubnet1Id="$PRIVATE_1"
  PrivateAppSubnet2Id="$PRIVATE_2"
  EcsClusterArn="$(get_output "$ECS_INFRA_STACK" ClusterArn)"
  EcsSecurityGroupId="$(get_output "$ECS_INFRA_STACK" EcsSecurityGroupId)"
  TaskExecutionRoleArn="$(get_output "$ECS_INFRA_STACK" TaskExecutionRoleArn)"
  ContainerImage="$ECR_UI_URI:$UI_IMAGE_TAG"
  DesiredCount="$DESIRED_COUNT"
  AlbListenerArn="$(get_output "$API_STACK" AlbListenerArn)"
  AlbSecurityGroupId="$(get_output "$API_STACK" AlbSecurityGroupId)"
  UserPoolArn="$POOL_ARN"
  UserPoolClientId="$POOL_CLIENT"
  UserPoolDomain="$POOL_DOMAIN"
)
[ -n "$KMS_KEY" ] && UI_PARAMS+=(KmsKeyArn="$KMS_KEY")

deploy_stack "$UI_STACK" "ui-service.yaml" "${UI_PARAMS[@]}"

echo ""
echo "========================================="
echo "  All stacks deployed for: $ENV"
echo "========================================="
echo ""
echo "  🌐 Application URL:  https://app-${ENV}.${DOMAIN}"
echo "  🔌 API URL:          https://api-${ENV}.${DOMAIN}"
echo "  📋 API Health:       https://api-${ENV}.${DOMAIN}/health"
echo ""
echo "========================================="
