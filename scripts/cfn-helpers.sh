#!/usr/bin/env bash
# =============================================================================
# CloudFormation deploy helpers
# Source this file in pipeline jobs: source scripts/cfn-helpers.sh
#
# Requires these env vars to be set by the caller:
#   STACK_NAME_PREFIX  — project prefix (e.g. "modernizer")
#   ENV                — environment name (e.g. "dev", "prod", "feat-xyz")
#   CI_PIPELINE_ID     — GitLab pipeline ID (set automatically by GitLab)
#
# Optional (for feature branch tags):
#   CI_COMMIT_REF_NAME — branch name
#   GITLAB_USER_LOGIN  — who triggered the pipeline
# =============================================================================

set -euo pipefail

# Fetch a single output value from a CloudFormation stack
get_output() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

# Get the most recent image tag from an ECR repository
#
# Usage: get_latest_image_tag <repository-name>
#
# Example:
#   API_TAG=$(get_latest_image_tag "modernizer-dev-api")
get_latest_image_tag() {
  local repo_name="$1"
  aws ecr describe-images \
    --repository-name "$repo_name" \
    --region "${AWS_REGION:-us-east-1}" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' \
    --output text | head -1
}

# Deploy a CloudFormation stack and print all outputs on success
#
# Usage: deploy_stack <stack-name> <template-file> [Key=Value ...]
#
# Example:
#   deploy_stack "my-auth" "auth.yaml" \
#     Environment="dev" \
#     ProjectName="modernizer"
deploy_stack() {
  local stack_name="$1"
  local template="$2"
  shift 2

  # Build tags — include branch/creator info if available
  local tags="Project=$STACK_NAME_PREFIX Environment=$ENV Pipeline=${CI_PIPELINE_ID:-local}"
  if [ -n "${CI_COMMIT_REF_NAME:-}" ]; then
    tags="$tags Branch=$CI_COMMIT_REF_NAME"
  fi
  if [ -n "${GITLAB_USER_LOGIN:-}" ]; then
    tags="$tags Creator=$GITLAB_USER_LOGIN"
  fi

  echo ""
  echo "=== Deploying: $stack_name ==="

  # Use S3 bucket for large templates (>51,200 bytes)
  local template_path="infrastructure/cloudformation/${template}"
  local s3_bucket_flag=""
  local template_size
  template_size=$(wc -c < "$template_path" | tr -d ' ')
  if [ "$template_size" -gt 50000 ]; then
    # Use the project's storage bucket for template packaging
    local s3_bucket="${STACK_NAME_PREFIX}-${ENV}-storage-bucket"
    s3_bucket_flag="--s3-bucket $s3_bucket --s3-prefix cfn-templates"
    echo "  (template ${template_size} bytes — using S3 bucket for packaging)"
  fi

  # shellcheck disable=SC2086
  # nosemgrep: unquoted-variable-expansion-in-command
  aws cloudformation deploy \
    --template-file "$template_path" \
    --stack-name "$stack_name" \
    --parameter-overrides "$@" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    $s3_bucket_flag \
    --tags $tags # nosemgrep: unquoted-variable-expansion-in-command

  echo "--- Outputs: $stack_name ---"
  aws cloudformation describe-stacks --stack-name "$stack_name" \
    --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
    --output table 2>/dev/null || echo "(no outputs)"
  echo ""
}

# Delete a CloudFormation stack if it exists, wait for completion
#
# Usage: delete_stack_if_exists <stack-name>
delete_stack_if_exists() {
  local stack_name="$1"
  if aws cloudformation describe-stacks --stack-name "$stack_name" > /dev/null 2>&1; then
    echo "Deleting stack: $stack_name"
    aws cloudformation delete-stack --stack-name "$stack_name"
    aws cloudformation wait stack-delete-complete --stack-name "$stack_name"
    echo "Stack $stack_name deleted"
  else
    echo "Stack $stack_name does not exist, skipping"
  fi
}
