# Deployment Guide

**Document Type:** Implementation Guide
**Last Updated:** March 27, 2026
**Version:** 2.0

---

## Overview

Deployment instructions for Database Modernizer. The platform runs on ECS Fargate with Step Functions orchestration, deployed via CloudFormation stacks through a GitHub Actions pipeline.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [ECS Fargate Deployment](#2-ecs-fargate-deployment)
3. [Local Development](#3-local-development)
4. [Configuration](#4-configuration)
5. [Troubleshooting](#5-troubleshooting)

---

## 1. Prerequisites

### Required Tools

```bash
# AWS CLI v2
aws --version

# Docker Desktop 4.0+
docker --version

# Python 3.12
python --version

# Node.js 18+
node --version

# uv (Python package manager)
uv --version

# Git
git --version
```

### AWS Permissions

Required IAM permissions for deployment and runtime:

- `cloudformation:*` (stack management)
- `ecr:*` (image push/pull)
- `ecs:RunTask`, `ecs:DescribeTasks`, `ecs:UpdateService`
- `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:Query`
- `states:StartExecution`, `states:DescribeExecution`, `states:ListExecutions`
- `cognito-idp:*` (user pool management)
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`
- `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey`
- `route53:ChangeResourceRecordSets`
- `elasticloadbalancing:*`
- `iam:PassRole`, `iam:CreateRole` (with `CAPABILITY_NAMED_IAM`)

---

## 2. ECS Fargate Deployment

### Architecture Overview

The platform is deployed as a set of CloudFormation stacks with the naming convention `{prefix}-{stack}` (e.g., `modernizer-dev-storage`, `modernizer-dev-api-service`).

- **ALB** is shared between API and UI with path-based routing: `/api/*` → API service, `/*` → UI service
- **Cognito** authentication is enforced on ALB listener rules (except `/health`)
- **API** runs FastAPI on port 8000
- **UI** runs React via `serve` on port 8080
- **Step Functions** orchestrates the pipeline: Collector → Triage → Map(Analysis → Schema Design per engine) → Synthesis

### CloudFormation Stacks (Deployment Order)

| Order | Template | Stack Name Example | Resources |
|-------|----------|--------------------|-----------|
| 1 | `dns.yaml` | `modernizer-dns` | Route 53 hosted zone, ACM certificate |
| 2 | `core-infra.yaml` | `modernizer-dev` | VPC, subnets, ECR repositories, KMS key |
| 3 | `ecs-infrastructure.yaml` | `modernizer-dev-ecs-infra` | ECS cluster, security groups, task execution role |
| 4 | `auth.yaml` | `modernizer-dev-auth` | Cognito User Pool, app client |
| 5 | `storage.yaml` | `modernizer-dev-storage` | S3 bucket (artifacts + uploads), DynamoDB table (job metadata), S3 logging bucket |
| 6 | `orchestration.yaml` | `modernizer-dev-orchestration` | Step Functions state machine, ECS task definitions for agents |
| 7 | `api-service.yaml` | `modernizer-dev-api-service` | ALB, ECS Fargate service (FastAPI), Route 53 DNS record, Cognito auth on ALB |
| 8 | `ui-service.yaml` | `modernizer-dev-ui` | ECS Fargate service (React UI), ALB listener rule |

Additional stacks (deployed independently):

| Template | Stack Name Example | Resources |
|----------|--------------------|-----------|
| `automation.yaml` | `modernizer-dev-automation` | EC2 automation instance for live mode collection |
| `ci-runner-iam.yaml` | `modernizer-ci-runner-iam` | IAM role for CI/CD pipeline (GitHub Actions OIDC) |

### CI/CD Pipeline (Automated)

Deployment is fully automated via GitHub Actions. The workflow is defined in:

- **`.github/workflows/deploy.yml`** — main deployment workflow
- **`scripts/deploy-services.sh`** — deploys stacks 3–8 in order

Pipeline stages for a push to main:

```
security → lint → test → deploy-shared → build → deploy-services → integration-test
```

The `deploy-shared` job deploys the DNS and core-infra stacks:

```bash
# Deploy shared infrastructure
deploy_stack "$DNS_STACK_NAME" "dns.yaml" \
  Environment="shared" \
  ProjectName="$STACK_NAME_PREFIX"

deploy_stack "$DEV_STACK_NAME" "core-infra.yaml" \
  Environment="dev" \
  ProjectName="$STACK_NAME_PREFIX"
```

The `deploy-services` job runs `deploy-services.sh`:

```bash
./scripts/deploy-services.sh \
  --env dev \
  --stack-prefix modernizer-dev \
  --core-stack modernizer-dev \
  --desired-count 1
```

### Manual Deployment

For manual or local deploys outside CI, source the helpers and run the same scripts:

```bash
# Set required environment variables
export STACK_NAME_PREFIX="modernizer"
export DNS_STACK_NAME="modernizer-dns"
export AWS_DEFAULT_REGION="us-east-1"
export ENV="dev"

# Source helpers
source scripts/cfn-helpers.sh

# 1. Deploy DNS (one-time, shared across environments)
deploy_stack "modernizer-dns" "dns.yaml" \
  Environment="shared" \
  ProjectName="modernizer"

# 2. Deploy core infrastructure
deploy_stack "modernizer-dev" "core-infra.yaml" \
  Environment="dev" \
  ProjectName="modernizer"

# 3–8. Deploy all service stacks
./scripts/deploy-services.sh \
  --env dev \
  --stack-prefix modernizer-dev \
  --core-stack modernizer-dev \
  --desired-count 1
```

#### deploy-services.sh Options

| Flag | Required | Description |
|------|----------|-------------|
| `--env` | Yes | Environment name (`dev`, `prod`, feature branch slug) |
| `--stack-prefix` | Yes | Prefix for service stack names (e.g., `modernizer-dev`) |
| `--core-stack` | Yes | Name of the core-infra stack to read VPC/ECR outputs from |
| `--desired-count` | Yes | ECS desired task count per service |
| `--ecr-env` | No | ECR environment if different from `--env` |
| `--auth-stack` | No | Reuse an existing auth stack instead of deploying a new one |
| `--kms-key` | No | KMS key ARN override |
| `--logs-bucket` | No | S3 logging bucket name override |
| `--create-test-user` | No | Create a test user in Cognito after auth stack deploy |

### Accessing the Deployed Application

```bash
# Get the API domain
aws cloudformation describe-stacks \
  --stack-name modernizer-dev-api-service \
  --query 'Stacks[0].Outputs[?OutputKey==`DomainName`].OutputValue' \
  --output text

# Get the ALB URL
aws cloudformation describe-stacks \
  --stack-name modernizer-dev-api-service \
  --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' \
  --output text
```

Open `https://app-dev.{domain}` in a browser. Cognito login is required.

---

## 3. Local Development

Local development runs the React UI dev server locally and proxies API calls to a deployed ALB. There is no local Docker Compose or Postgres setup.

### UI Development (React)

```bash
cd src/ui

# Install dependencies
npm install

# Start dev server on port 3000
npm start
```

The dev server uses `src/ui/src/setupProxy.js` to proxy `/api/*` requests to the deployed ALB:

```javascript
// setupProxy.js — proxies /api/* to the deployed ALB
const target = process.env.REACT_APP_API_PROXY || DEFAULT_TARGET;
```

To point at a specific ALB:

```bash
REACT_APP_API_PROXY=https://api-dev.your-domain.example.com npm start
```

The proxy forwards all `/api/*` requests to the target with `changeOrigin: true` and `secure: false` (ALB uses self-signed cert internally).

### API Development (FastAPI)

The Python API can run locally with `uvicorn`, but requires valid AWS credentials for S3, Step Functions, DynamoDB, and CloudWatch:

```bash
# From the repository root
uv sync

# Run the API server locally on port 8000
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

You need the following environment variables (or AWS profile) configured:

```bash
export AWS_DEFAULT_REGION=us-east-1
export STATE_MACHINE_ARN=<from modernizer-dev-orchestration stack>
export S3_BUCKET=<from modernizer-dev-storage stack>
```

### Running Tests

```bash
# Unit tests
uv run pytest tests/ -v

# Integration tests (requires deployed dev stack)
export STACK_NAME="modernizer-dev"
uv run pytest tests/integration/ -v --tb=short
```

---

## 4. Configuration

### Key Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |
| `STACK_NAME_PREFIX` | Project prefix for stack naming | `modernizer` |
| `DNS_STACK_NAME` | Name of the DNS stack | `modernizer-dns` |
| `DOCKER_IMAGE_TAG` | Image tag for ECS deployments | `$CI_COMMIT_SHORT_SHA` |
| `REACT_APP_API_PROXY` | ALB URL for local UI proxy | See `setupProxy.js` |

### Stack Parameter Conventions

All service stacks receive these common parameters:

- `Environment` — environment name (`dev`, `prod`, feature slug)
- `ProjectName` — project prefix (`modernizer`)

Cross-stack references are resolved at deploy time via `get_output` from `scripts/cfn-helpers.sh`:

```bash
# Example: get VPC ID from core-infra stack
VPC_ID=$(get_output "modernizer-dev" "VpcId")
```

### S3 Bucket

The storage stack creates a single S3 bucket for both artifacts and browser uploads. CORS is configured for presigned URL uploads from the browser. The bucket name is output as `S3BucketName` from the storage stack.

### Cognito

The auth stack creates a Cognito User Pool with an app client. The callback URL is set to `https://app-{env}.{domain}/oauth2/idpresponse`. ALB listener rules enforce Cognito authentication on all paths except `/health`.

---

## 5. Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Stack deploy fails with `ROLLBACK_COMPLETE` | Parameter mismatch or resource conflict | Delete the failed stack, fix parameters, redeploy |
| ECS tasks keep restarting | Container crash or health check failure | Check CloudWatch logs for the task |
| ALB returns 502 | ECS tasks not healthy or not registered | Verify target group health, check security groups |
| Cognito redirect loop | Callback URL mismatch | Verify `CallbackUrl` parameter matches ALB domain |
| Step Functions execution failed | Agent ECS task failed | Check execution history, then agent task CloudWatch logs |
| S3 presigned upload fails with CORS error | CORS config mismatch | Verify S3 CORS allows the app domain origin |
| Image not found in ECR | Build stage skipped or failed | Check `build-backend` / `build-frontend` CI jobs |

### Debug Commands

```bash
# ── Stack inspection ──

# List all modernizer stacks
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?starts_with(StackName, `modernizer-dev`)].{Name:StackName,Status:StackStatus}' \
  --output table

# Get outputs from a specific stack
aws cloudformation describe-stacks \
  --stack-name modernizer-dev-storage \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

# Check stack events for deploy failures
aws cloudformation describe-stack-events \
  --stack-name modernizer-dev-api-service \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED` || ResourceStatus==`UPDATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table

# ── ECS ──

# Get ECS cluster ARN
CLUSTER=$(aws cloudformation describe-stacks \
  --stack-name modernizer-dev-ecs-infra \
  --query 'Stacks[0].Outputs[?OutputKey==`ClusterArn`].OutputValue' \
  --output text)

# List running services
aws ecs list-services --cluster "$CLUSTER" --output table

# Describe API service tasks
aws ecs list-tasks --cluster "$CLUSTER" --service-name modernizer-dev-api-service \
  --query 'taskArns' --output text | \
  xargs -I{} aws ecs describe-tasks --cluster "$CLUSTER" --tasks {}

# ── Step Functions ──

# Get state machine ARN
SFN_ARN=$(aws cloudformation describe-stacks \
  --stack-name modernizer-dev-orchestration \
  --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
  --output text)

# List recent executions
aws stepfunctions list-executions \
  --state-machine-arn "$SFN_ARN" \
  --max-results 10 \
  --query 'executions[*].{Name:name,Status:status,Start:startDate}' \
  --output table

# Describe a specific execution
aws stepfunctions describe-execution \
  --execution-arn "arn:aws:states:us-east-1:754955336423:execution:modernizer-dev-workflow:JOB_ID"

# ── CloudWatch Logs ──

# Tail API service logs
aws logs tail /ecs/modernizer-dev-api --follow

# Tail agent task logs
aws logs tail /ecs/modernizer-dev-agents --follow

# ── Cognito ──

# Get User Pool ID
POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name modernizer-dev-auth \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)

# List users
aws cognito-idp list-users --user-pool-id "$POOL_ID"

# ── S3 ──

# Get bucket name
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name modernizer-dev-storage \
  --query 'Stacks[0].Outputs[?OutputKey==`S3BucketName`].OutputValue' \
  --output text)

# List job artifacts
aws s3 ls "s3://${BUCKET}/jobs/" --recursive
```

### Upgrades

Upgrades are handled by the CI pipeline on mainline push. For manual upgrades:

```bash
git pull origin main

# Rebuild and push images
# (handled by CI build job — see .github/workflows/deploy.yml)

# Redeploy all service stacks
source scripts/cfn-helpers.sh
export STACK_NAME_PREFIX="modernizer"
export DNS_STACK_NAME="modernizer-dns"
export ENV="dev"

./scripts/deploy-services.sh \
  --env dev \
  --stack-prefix modernizer-dev \
  --core-stack modernizer-dev \
  --desired-count 1
```

---

## Related Documentation

- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [High-Level Design](../architecture/high-level-design.md)
- [VPC Deployment Diagram](../architecture/architecture-diagrams/02-vpc-deployment.md)

---

**Last Updated:** March 27, 2026
**Version:** 2.0
**Maintained By:** Database Modernizer Engineering Team
