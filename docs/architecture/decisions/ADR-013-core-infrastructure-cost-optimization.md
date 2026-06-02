# ADR-013: Core Infrastructure Cost Optimization for Dev Environment

**Status:** Accepted

**Date:** 2026-02-13

**Decision Makers:** Database Modernizer Assessment Team

## Context

The core infrastructure stack (`core-infra.yaml`) provides VPC networking, container registry, and storage foundations for the project. During implementation, we evaluated the cost profile and identified that NAT gateways are the dominant cost driver at ~$33/month each. We needed to balance cost efficiency for the dev environment against operational simplicity and security best practices.

## Decisions

### 1. Single NAT Gateway for Dev Environment

Use one NAT gateway instead of the HA pair (one per AZ) recommended for production.

**Cost impact:** Saves ~$33/month ($0.045/hr + $0.045/GB processed).

**Tradeoff:** If the NAT gateway's AZ goes down, private subnets lose outbound internet temporarily. Acceptable for dev.

**Action for prod:** Add a second NAT gateway in PublicSubnet2 with its own EIP, and split route tables back to one-per-AZ so each AZ routes through its local NAT.

### 2. VPC Gateway Endpoints for S3 and DynamoDB

Added free gateway endpoints that route S3 and DynamoDB traffic over the AWS backbone instead of through the NAT gateway.

**Cost impact:** $0/month. Reduces NAT data processing charges ($0.045/GB saved on all S3/DynamoDB traffic).

### 3. Shared KMS Key for Core Infrastructure

Use a single KMS key for all encryption in the core stack (VPC flow logs, ECR repository) instead of per-resource keys.

**Cost impact:** $1/month instead of $2+/month for multiple keys.

**Rationale:** All resources share the same lifecycle and access patterns. The key ARN is exported for other stacks to consume.

### 4. VPC Flow Logs with KMS Encryption

Added VPC flow logs to a KMS-encrypted CloudWatch Log Group with 30-day retention, rather than suppressing the cfn-nag W60 warning.

**Cost impact:** ~$0.50/GB ingested (minimal in dev).

### 5. ECR with KMS Encryption and Immutable Tags

ECR repository uses KMS encryption (via shared key) and immutable image tags instead of AES256 and mutable tags.

**Rationale:** Resolves Checkov CKV_AWS_136 and CKV_AWS_51. Immutable tags prevent overwriting tagged images with different content.

### 6. cfn-nag Warnings Treated as Failures

All cfn-nag warnings must be resolved with real security implementations, not suppressions. The only pre-approved suppression is W28 (explicit resource naming) when following the project naming convention for cross-stack references.

## Monthly Cost Summary (Dev Environment)

| Resource | Monthly Cost |
|---|---|
| NAT Gateway + EIP | ~$33 |
| KMS Key (shared) | ~$1 |
| VPC Flow Logs (CloudWatch) | ~$0.50/GB |
| ECR Storage | $0.10/GB |
| S3 Storage | $0.023/GB |
| Everything else (VPC, subnets, IGW, route tables, gateway endpoints) | Free |

**Estimated total: ~$35/month**

## Alternatives Considered

### Remove NAT Gateway Entirely

Replace with interface VPC endpoints for ECR, CloudWatch, SSM, etc.

- **Cost:** ~$21+/month (3+ endpoints at $7/ea/AZ)
- **Rejected:** Doesn't cover external API calls, more complex to manage, and savings are marginal.

### EC2 Instance Connect Endpoint for Dev Access

Free endpoint for SSH/tunneling to private instances from laptop.

- **Cost:** Free
- **Not needed now:** Doesn't replace NAT for ECS task outbound. Can be added later if direct private subnet access is needed.

### Bastion Host

t4g.micro in public subnet for SSH jump access.

- **Cost:** ~$6/month
- **Not needed now:** ECS Exec provides container access through existing NAT. Can be added later.

## Review Criteria

This decision should be revisited when:

- Moving to staging/production (add second NAT gateway for HA)
- NAT data processing costs become significant (add more gateway/interface endpoints)
- Team needs direct access to private subnet resources (add EC2 Instance Connect Endpoint)

---

**Last Updated:** February 13, 2026
