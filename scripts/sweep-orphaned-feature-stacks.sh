#!/usr/bin/env bash
# =============================================================================
# Sweep orphaned feature branch stacks
#
# Finds all CloudFormation stacks matching the feature naming pattern and
# checks if the corresponding git branch still exists. If the branch is gone,
# the stack is orphaned and gets cleaned up.
#
# Usage:
#   ./scripts/sweep-orphaned-feature-stacks.sh [--dry-run]
#
# Requires:
#   - scripts/cleanup-feature-stacks.sh available
#   - STACK_NAME_PREFIX env vars set
#   - AWS credentials and git remote access configured
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false

if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=true
  echo "[DRY RUN] No stacks will be deleted"
fi

FEAT_PREFIX="${STACK_NAME_PREFIX}-feat-"

echo "========================================="
echo "  Sweeping orphaned feature stacks"
echo "  Looking for prefix: ${FEAT_PREFIX}"
echo "========================================="

# Get all feature stack names (deduplicate by branch suffix)
FEATURE_BRANCHES=$(aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, '${FEAT_PREFIX}')].StackName" \
  --output text | tr '\t' '\n' | \
  sed "s|^${FEAT_PREFIX}||" | \
  sed 's/-\(ui\|orch\|storage\|api-service\)$//' | \
  sort -u)

if [ -z "$FEATURE_BRANCHES" ]; then
  echo "No feature stacks found — nothing to sweep"
  exit 0
fi

# Get all remote branch names (sanitized the same way deploy does)
echo "Fetching remote branches..."
REMOTE_BRANCHES=$(git ls-remote --heads origin 2>/dev/null | \
  awk '{print $2}' | sed 's|refs/heads/||' | \
  tr '[:upper:]' '[:lower:]' | \
  sed 's/[^a-z0-9-]/-/g;s/^feat-//;s/^feature-//;s/^fix-//;s/^hotfix-//;s/^ci-//' | \
  cut -c1-15 | sort -u)

ORPHANED=0
KEPT=0

for BRANCH in $FEATURE_BRANCHES; do
  if echo "$REMOTE_BRANCHES" | grep -qx "$BRANCH"; then
    echo "  ACTIVE: ${BRANCH} (branch still exists)"
    KEPT=$((KEPT + 1))
  else
    echo "  ORPHANED: ${BRANCH} (branch deleted)"
    ORPHANED=$((ORPHANED + 1))
    if [ "$DRY_RUN" = false ]; then
      "${SCRIPT_DIR}/cleanup-feature-stacks.sh" "$BRANCH"
    fi
  fi
done

echo ""
echo "========================================="
echo "  Sweep complete: ${ORPHANED} orphaned, ${KEPT} active"
echo "========================================="
