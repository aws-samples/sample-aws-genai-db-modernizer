# ADR-012: Use CloudFormation Over CDK for Infrastructure as Code

**Status:** Accepted

**Date:** 2026-02-11

**Decision Makers:** Database Modernizer Team

## Context

We need to choose an Infrastructure as Code (IaC) solution for deploying Database Modernizer into customer AWS accounts. The two options are AWS CloudFormation (native AWS IaC) and AWS CDK (higher-level abstraction).

## Decision

We will use **AWS CloudFormation** as our Infrastructure as Code solution.

## Rationale

### Simplicity

**CloudFormation:**

- Direct YAML/JSON templates, no build step
- What you write is what gets deployed
- Only requires AWS CLI

**CDK:**

- Requires Node.js/TypeScript/Python environment
- Needs `cdk synth` to generate CloudFormation
- Additional dependencies and toolchain

### Customer Experience

**CloudFormation advantages:**

- Customers can inspect templates before deployment
- No programming knowledge required
- Standard AWS deployment method
- Easy to customize per customer

**CDK disadvantages:**

- Requires CDK toolkit installation
- Generated CloudFormation harder to review
- Programming language barrier
- Version compatibility issues

### Transparency and Security

**CloudFormation:**

- Clear visibility of resources being created
- No hidden abstractions
- Easier security audits

**CDK:**

- Generated code may include unexpected resources
- Abstractions hide implementation details

### Project-Specific Fit

- Target audience: database administrators and architects (not just developers)
- Customers need to trust what's deployed in their accounts
- Infrastructure is straightforward (VPC, ECS, RDS access, S3)
- Transparency more valuable than code reusability

## Consequences

### Positive

- Faster onboarding for team and customers
- Easier code reviews
- No toolchain to maintain
- Better customer trust
- Simpler CI/CD (no build step)

### Negative

- More verbose for complex infrastructure
- Less code reusability
- Manual parameter management
- No compile-time type checking

### Mitigation

- Use nested stacks for reusable components
- Leverage CloudFormation parameters
- Add template validation in CI/CD
- Use CloudFormation linting tools (cfn-lint)

## Alternatives Considered

### AWS CDK

- **Pros:** Concise code, better reusability, type safety
- **Cons:** Additional complexity, toolchain dependencies, harder to audit
- **Rejected:** Complexity outweighs benefits

### Terraform

- **Pros:** Multi-cloud support
- **Cons:** Not AWS-native, requires state management
- **Rejected:** Unnecessary for AWS-only deployment

## Implementation

1. Create CloudFormation templates in `infrastructure/cloudformation/`
2. Update all documentation
3. Remove CDK references
4. Add CloudFormation validation to CI/CD
5. Create deployment guide with examples

## Review Criteria

This decision can be revisited if:

- Infrastructure complexity significantly increases
- Customer feedback indicates CDK preference
- Multi-cloud support becomes required

---

**Last Updated:** February 11, 2026
