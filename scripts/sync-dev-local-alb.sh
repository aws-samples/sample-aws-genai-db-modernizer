#!/usr/bin/env bash
# =============================================================================
# Sync dev-local ALB targets with the current dev ECS service tasks.
#
# After every main merge, the dev ECS service gets new tasks (new IPs).
# The dev-local ALB (elb-01) — Sergio's no-Cognito workaround for local
# development — still points at the old task IPs and returns 504s.
#
# This script:
#   1. Reads the current target group from the dev ECS service
#   2. Gets the healthy target IPs from that target group
#   3. Deregisters stale targets from the dev-local target group
#   4. Registers the fresh targets
#
# Usage:
#   ./scripts/sync-dev-local-alb.sh
#
# Env vars (all optional — sensible defaults for the dev environment):
#   ECS_CLUSTER        — ECS cluster name
#   ECS_SERVICE         — ECS service name
#   DEV_LOCAL_TG_ARN   — ARN of the dev-local ALB's target group
#   REGION             — AWS region
# =============================================================================

set -euo pipefail

REGION="${REGION:-us-east-1}"
ECS_CLUSTER="${ECS_CLUSTER:-modernizer-dev-ecs-cluster}"
ECS_SERVICE="${ECS_SERVICE:-modernizer-dev-api-service}"
DEV_LOCAL_TG_ARN="${DEV_LOCAL_TG_ARN:-arn:aws:elasticloadbalancing:us-east-1:754955336423:targetgroup/trg-elb-api-dev/81732fdf8b09eae6}"

echo "============================================="
echo " Sync dev-local ALB"
echo " Cluster:  $ECS_CLUSTER"
echo " Service:  $ECS_SERVICE"
echo " Dev-local TG: $DEV_LOCAL_TG_ARN"
echo "============================================="

# ---------------------------------------------------------
# 1. Discover the dev service's target group from ECS
# ---------------------------------------------------------
echo ""
echo ">>> Step 1: Discover dev service target group"

SERVICE_TG_ARN=$(aws ecs describe-services \
  --cluster "$ECS_CLUSTER" \
  --services "$ECS_SERVICE" \
  --region "$REGION" \
  --query 'services[0].loadBalancers[0].targetGroupArn' \
  --output text)

if [ -z "$SERVICE_TG_ARN" ] || [ "$SERVICE_TG_ARN" = "None" ]; then
  echo "  ❌ Could not find target group for $ECS_SERVICE"
  exit 1
fi
echo "  Service TG: $SERVICE_TG_ARN"

# ---------------------------------------------------------
# 2. Get healthy targets from the dev service's TG
# ---------------------------------------------------------
echo ""
echo ">>> Step 2: Get healthy targets from service TG"

FRESH_TARGETS=$(aws elbv2 describe-target-health \
  --target-group-arn "$SERVICE_TG_ARN" \
  --region "$REGION" \
  --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`].Target.[Id,Port,AvailabilityZone]' \
  --output text)

if [ -z "$FRESH_TARGETS" ]; then
  echo "  ❌ No healthy targets found in service TG"
  echo "  The ECS tasks may still be starting. Wait a minute and retry."
  exit 1
fi

echo "  Healthy targets:"
echo "$FRESH_TARGETS" | while IFS=$'\t' read -r ip port az; do
  echo "    $ip:$port ($az)"
done

# ---------------------------------------------------------
# 3. Deregister stale targets from dev-local TG
# ---------------------------------------------------------
echo ""
echo ">>> Step 3: Deregister stale targets from dev-local TG"

STALE_TARGETS=$(aws elbv2 describe-target-health \
  --target-group-arn "$DEV_LOCAL_TG_ARN" \
  --region "$REGION" \
  --query 'TargetHealthDescriptions[].Target.[Id,Port,AvailabilityZone]' \
  --output text)

if [ -n "$STALE_TARGETS" ]; then
  echo "$STALE_TARGETS" | while IFS=$'\t' read -r ip port az; do
    echo "  Deregistering $ip:$port ($az)"
    aws elbv2 deregister-targets \
      --target-group-arn "$DEV_LOCAL_TG_ARN" \
      --targets "Id=$ip,Port=$port,AvailabilityZone=$az" \
      --region "$REGION"
  done
else
  echo "  No existing targets to remove"
fi

# ---------------------------------------------------------
# 4. Register fresh targets into dev-local TG
# ---------------------------------------------------------
echo ""
echo ">>> Step 4: Register fresh targets into dev-local TG"

echo "$FRESH_TARGETS" | while IFS=$'\t' read -r ip port az; do
  echo "  Registering $ip:$port ($az)"
  aws elbv2 register-targets \
    --target-group-arn "$DEV_LOCAL_TG_ARN" \
    --targets "Id=$ip,Port=$port,AvailabilityZone=$az" \
    --region "$REGION"
done

# ---------------------------------------------------------
# 5. Verify
# ---------------------------------------------------------
echo ""
echo ">>> Step 5: Verify dev-local TG health"

# Give ALB a moment to start health checks
sleep 5

aws elbv2 describe-target-health \
  --target-group-arn "$DEV_LOCAL_TG_ARN" \
  --region "$REGION" \
  --query 'TargetHealthDescriptions[].{IP:Target.Id,Port:Target.Port,State:TargetHealth.State}' \
  --output table

echo ""
echo "✅ Done. Dev-local ALB should be serving traffic shortly."
