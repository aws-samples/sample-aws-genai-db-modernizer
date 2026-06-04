# Database Modernizer Assessment - Deployment Makefile
# =====================================================================
# One-command deployment for fresh AWS accounts.
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials for the target account
#   - Docker Desktop running (required for "make build")
#   - Python 3.12+ and uv (for local development)
#   - A domain you own (see "make deploy-dns" output for NS records)
#
# Quick start:
#   1. cp .env.example .env       # Edit with your values
#   2. make deploy-dns            # Deploy DNS + cert (one-time, add NS records to parent)
#   3. make deploy-infra          # Deploy VPC, ECR, KMS
#   4. make build                 # Build and push Docker images (requires Docker Desktop)
#   5. make deploy-services       # Deploy ECS, ALB, Cognito, S3, Step Functions
#   6. make create-test-user      # Create a Cognito test user
#
# To tear everything down (empties S3 and ECR automatically):
#   make destroy                  # Deletes services + infra (preserves DNS)
#   make destroy-dns              # Also delete DNS stack (if done with the project)
# =====================================================================

# Load environment from .env if it exists
-include .env
export

# Required variables (override via .env or environment)
PROJECT_NAME    ?= modernizer
ENV             ?= dev
AWS_REGION      ?= us-east-1
PARENT_DOMAIN   ?= example.com
SUBDOMAIN       ?= modernizer
DESIRED_COUNT   ?= 1

# Derived names
STACK_NAME_PREFIX := $(PROJECT_NAME)
STACK_PREFIX      := $(PROJECT_NAME)-$(ENV)
DNS_STACK         := $(PROJECT_NAME)-dns
DNS_STACK_NAME    := $(DNS_STACK)
CORE_STACK        := $(STACK_PREFIX)
AWS_DEFAULT_REGION := $(AWS_REGION)
IMAGE_TAG         ?= $(shell git rev-parse --short HEAD)
CFN_DIR           := infrastructure/cloudformation

# =====================================================================
# Top-level targets
# =====================================================================

.PHONY: help deploy deploy-all destroy local test lint

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

deploy: deploy-infra build deploy-services ## Full deployment (infra + build + services)
	@echo ""
	@echo "✅ Deployment complete!"
	@echo "   App URL: https://app-$(ENV).$(SUBDOMAIN).$(PARENT_DOMAIN)"
	@echo ""
	@echo "   Run 'make create-test-user' to create a login."

deploy-all: deploy-dns deploy ## Full deployment including DNS (first-time setup)

# =====================================================================
# DNS and Certificate (one-time, shared across environments)
# =====================================================================

.PHONY: deploy-dns

deploy-dns: ## Deploy Route 53 hosted zone and ACM certificate
	@echo "=== Deploying DNS stack: $(DNS_STACK) ==="
	@echo "    Domain: $(SUBDOMAIN).$(PARENT_DOMAIN)"
	@echo ""
	source scripts/cfn-helpers.sh && \
	deploy_stack "$(DNS_STACK)" "dns.yaml" \
		Environment="shared" \
		ProjectName="$(PROJECT_NAME)" \
		ParentDomainName="$(PARENT_DOMAIN)" \
		SubDomainPrefix="$(SUBDOMAIN)"
	@echo ""
	@echo "⚠️  ACTION REQUIRED: Add these NS records to your parent domain ($(PARENT_DOMAIN)):"
	@aws cloudformation describe-stacks \
		--stack-name $(DNS_STACK) \
		--query 'Stacks[0].Outputs[?OutputKey==`HostedZoneNameServers`].OutputValue' \
		--output text --region $(AWS_REGION)
	@echo ""
	@echo "   Wait for ACM certificate validation (usually 2-5 minutes after NS delegation)."

# =====================================================================
# Core Infrastructure
# =====================================================================

.PHONY: deploy-infra

deploy-infra: ## Deploy VPC, ECR, KMS (core infrastructure)
	@echo "=== Deploying core infrastructure: $(CORE_STACK) ==="
	source scripts/cfn-helpers.sh && \
	deploy_stack "$(CORE_STACK)" "core-infra.yaml" \
		Environment="$(ENV)" \
		ProjectName="$(PROJECT_NAME)"

# =====================================================================
# Docker Image Builds
# =====================================================================

.PHONY: build build-api build-agent build-ui

build: build-api build-agent build-ui ## Build and push all Docker images

build-api: ## Build and push API image
	@echo "=== Building API image ==="
	$(eval ECR_URI := $(shell aws cloudformation describe-stacks \
		--stack-name $(CORE_STACK) \
		--query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryUri`].OutputValue' \
		--output text --region $(AWS_REGION)))
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR_URI)
	docker build --platform linux/amd64 -t $(ECR_URI):$(IMAGE_TAG) -f infrastructure/docker/core/Dockerfile .
	docker push $(ECR_URI):$(IMAGE_TAG)

build-agent: ## Build and push Agent image
	@echo "=== Building Agent image ==="
	$(eval ECR_URI := $(shell aws cloudformation describe-stacks \
		--stack-name $(CORE_STACK) \
		--query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryAgentUri`].OutputValue' \
		--output text --region $(AWS_REGION)))
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR_URI)
	docker build --platform linux/amd64 -t $(ECR_URI):$(IMAGE_TAG) -f infrastructure/docker/agent/Dockerfile .
	docker push $(ECR_URI):$(IMAGE_TAG)

build-ui: ## Build and push UI image
	@echo "=== Building UI image ==="
	$(eval ECR_URI := $(shell aws cloudformation describe-stacks \
		--stack-name $(CORE_STACK) \
		--query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryUiUri`].OutputValue' \
		--output text --region $(AWS_REGION)))
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR_URI)
	docker build --platform linux/amd64 -t $(ECR_URI):$(IMAGE_TAG) -f infrastructure/docker/ui/Dockerfile .
	docker push $(ECR_URI):$(IMAGE_TAG)

# =====================================================================
# Service Stacks (ECS, ALB, Step Functions, etc.)
# =====================================================================

.PHONY: deploy-services

deploy-services: ## Deploy all service stacks (ECS, ALB, Cognito, S3, SFN)
	@echo "=== Deploying service stacks ==="
	export STACK_NAME_PREFIX="$(PROJECT_NAME)" && \
	export DNS_STACK_NAME="$(DNS_STACK)" && \
	export AWS_DEFAULT_REGION="$(AWS_REGION)" && \
	export DOCKER_IMAGE_TAG="$(IMAGE_TAG)" && \
	./scripts/deploy-services.sh \
		--env $(ENV) \
		--stack-prefix $(STACK_PREFIX) \
		--core-stack $(CORE_STACK) \
		--desired-count $(DESIRED_COUNT)

# =====================================================================
# User Management
# =====================================================================

.PHONY: create-test-user

create-test-user: ## Create a test user in Cognito
	$(eval POOL_ID := $(shell aws cloudformation describe-stacks \
		--stack-name $(STACK_PREFIX)-auth \
		--query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
		--output text --region $(AWS_REGION)))
	./scripts/create-test-user.sh "$(POOL_ID)"

# =====================================================================
# Local Development
# =====================================================================

.PHONY: local local-api local-ui test lint setup

setup: ## Install dependencies and pre-commit hooks
	./scripts/setup_dev.sh

local: ## Start local API + UI for development
	@echo "Starting local services..."
	@echo "  API:  http://localhost:8000"
	@echo "  UI:   http://localhost:3000"
	@STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts \
		uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 &
	@cd src/ui && npm start

local-api: ## Start only the local API server
	STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts \
		uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

test: ## Run all tests
	uv run pytest tests/unit/ tests/contract/ -v --cov=src --cov-report=term

lint: ## Run all linters
	uv run pre-commit run --all-files

assess: ## Run sample assessment (WordPress)
	uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json

# =====================================================================
# Teardown
# =====================================================================

.PHONY: destroy destroy-services destroy-infra destroy-dns

destroy: destroy-services destroy-infra ## Destroy all stacks (except DNS)
	@echo "✅ All service and infra stacks deleted."
	@echo "   DNS stack ($(DNS_STACK)) preserved. Run 'make destroy-dns' to remove it."

destroy-services: cleanup-s3 ## Destroy service stacks (empties S3 first)
	@echo "=== Destroying service stacks ==="
	source scripts/cfn-helpers.sh && \
	delete_stack_if_exists "$(STACK_PREFIX)-ui" && \
	delete_stack_if_exists "$(STACK_PREFIX)-api-service" && \
	delete_stack_if_exists "$(STACK_PREFIX)-orchestration" && \
	delete_stack_if_exists "$(STACK_PREFIX)-storage" && \
	delete_stack_if_exists "$(STACK_PREFIX)-auth" && \
	delete_stack_if_exists "$(STACK_PREFIX)-ecs-infra"

destroy-infra: cleanup-ecr cleanup-logs-bucket ## Destroy core infrastructure stack (empties ECR and logs bucket first)
	source scripts/cfn-helpers.sh && \
	delete_stack_if_exists "$(CORE_STACK)"

cleanup-logs-bucket: ## Empty the central logs bucket from core-infra stack
	@echo "=== Emptying logs bucket (core-infra) ==="
	@BUCKET=$$(aws cloudformation describe-stacks \
		--stack-name $(CORE_STACK) \
		--query 'Stacks[0].Outputs[?OutputKey==`CentralLogsBucketName`].OutputValue' \
		--output text --region $(AWS_REGION) 2>/dev/null) && \
	if [ -n "$$BUCKET" ] && [ "$$BUCKET" != "None" ]; then \
		echo "  Emptying: $$BUCKET"; \
		aws s3 rm "s3://$$BUCKET" --recursive --region $(AWS_REGION) 2>/dev/null || true; \
		aws s3api list-object-versions --bucket "$$BUCKET" --region $(AWS_REGION) \
			--query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null | \
			aws s3api delete-objects --bucket "$$BUCKET" --delete file:///dev/stdin --region $(AWS_REGION) 2>/dev/null || true; \
		aws s3api list-object-versions --bucket "$$BUCKET" --region $(AWS_REGION) \
			--query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null | \
			aws s3api delete-objects --bucket "$$BUCKET" --delete file:///dev/stdin --region $(AWS_REGION) 2>/dev/null || true; \
		echo "  ✓ Logs bucket emptied"; \
	else \
		echo "  No core-infra stack found, skipping."; \
	fi

destroy-dns: ## Destroy DNS stack (removes hosted zone and certificate)
	source scripts/cfn-helpers.sh && \
	delete_stack_if_exists "$(DNS_STACK)"

cleanup-s3: ## Empty all S3 buckets before stack deletion
	@echo "=== Emptying S3 buckets ==="
	@for output_key in S3BucketName LoggingBucketName; do \
		BUCKET=$$(aws cloudformation describe-stacks \
			--stack-name $(STACK_PREFIX)-storage \
			--query "Stacks[0].Outputs[?OutputKey==\`$$output_key\`].OutputValue" \
			--output text --region $(AWS_REGION) 2>/dev/null); \
		if [ -n "$$BUCKET" ] && [ "$$BUCKET" != "None" ]; then \
			echo "  Emptying $$output_key: $$BUCKET"; \
			aws s3 rm "s3://$$BUCKET" --recursive --region $(AWS_REGION) 2>/dev/null || true; \
			aws s3api list-object-versions --bucket "$$BUCKET" --region $(AWS_REGION) \
				--query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null | \
				aws s3api delete-objects --bucket "$$BUCKET" --delete file:///dev/stdin --region $(AWS_REGION) 2>/dev/null || true; \
			aws s3api list-object-versions --bucket "$$BUCKET" --region $(AWS_REGION) \
				--query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null | \
				aws s3api delete-objects --bucket "$$BUCKET" --delete file:///dev/stdin --region $(AWS_REGION) 2>/dev/null || true; \
			echo "  ✓ $$BUCKET emptied"; \
		fi; \
	done

cleanup-ecr: ## Delete all images from ECR repositories
	@echo "=== Cleaning ECR repositories ==="
	@for repo in $(PROJECT_NAME)-$(ENV)-api $(PROJECT_NAME)-$(ENV)-agent $(PROJECT_NAME)-$(ENV)-ui $(PROJECT_NAME)-$(ENV)-agent-load-test; do \
		IMAGES=$$(aws ecr list-images --repository-name "$$repo" --region $(AWS_REGION) --query 'imageIds[*]' --output json 2>/dev/null); \
		if [ -n "$$IMAGES" ] && [ "$$IMAGES" != "[]" ]; then \
			echo "  Deleting images from $$repo"; \
			aws ecr batch-delete-image --repository-name "$$repo" --image-ids "$$IMAGES" --region $(AWS_REGION) > /dev/null; \
		fi; \
	done
	@echo "  ✓ ECR repositories emptied"

# =====================================================================
# Status
# =====================================================================

.PHONY: status

status: ## Show deployment status
	@echo "=== Stack Status ==="
	@aws cloudformation list-stacks \
		--stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
		--query 'StackSummaries[?starts_with(StackName, `$(PROJECT_NAME)`)].{Name:StackName,Status:StackStatus,Updated:LastUpdatedTime}' \
		--output table --region $(AWS_REGION)
