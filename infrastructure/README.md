# Infrastructure

This directory contains deployment configurations.

## Structure

- **docker/** - Docker and Docker Compose configurations
- **cloudformation/** - AWS CloudFormation templates for infrastructure as code

## Local Development

```bash
# Start all services with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

## AWS Deployment

CloudFormation templates are provided for production deployment to AWS.

See [Deployment Guide](../docs/guides/deployment-guide.md) for detailed instructions.

### Prerequisites

Before deploying CloudFormation templates, ensure you have the required tools installed:

```bash
# Install cfn-lint for CloudFormation template validation
# This is included in the dev dependencies
uv sync

# Or install separately
pip install cfn-lint

# Verify installation
cfn-lint --version
```

For security scanning, install cfn-nag (Ruby gem):

```bash
# Install cfn-nag
gem install cfn-nag

# Verify installation
cfn_nag_scan --version
```

### Validate Templates

Before deploying, validate your CloudFormation templates:

```bash
# Lint templates
cfn-lint infrastructure/cloudformation/*.yaml

# Security scan
cfn_nag_scan --input-path infrastructure/cloudformation/

# AWS validation
aws cloudformation validate-template --template-body file://infrastructure/cloudformation/core-infra.yaml
```

### Quick Deploy

```bash
cd cloudformation
aws cloudformation deploy \
  --template-file database-modernizer.yaml \
  --stack-name DatabaseModernizerStack \
  --capabilities CAPABILITY_IAM
```
