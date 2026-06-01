# ECS Fargate Deployment (Primary)

Customer VPC deployment using ECS Fargate. Deployed via CloudFormation.

```mermaid
graph TB
    subgraph "Customer AWS Account / VPC"
        subgraph "Public Subnets"
            ALB[ALB<br/>HTTPS + Cognito Auth<br/>Path-based routing]
        end

        subgraph "Private Subnets - App Tier"
            UI[UI Service<br/>React SPA :8080]
            API[API Service<br/>FastAPI :8000]
            WORKER[Workers<br/>1-10 auto-scaling]
        end

        subgraph "Private Subnets - Data Tier"
            RDS[(Customer RDS<br/>Read-only)]
            REDIS[(Customer Redis<br/>Read-only)]
        end

        subgraph "Managed Services (VPC Endpoints)"
            S3[S3] & DDB[DynamoDB] & EB[EventBridge]
        end

        ROLE[IAM Task Role]
    end

    USER[Browser] -->|HTTPS| ALB
    ALB -->|/api/*| API
    ALB -->|/*| UI
    API --> WORKER
    WORKER --> RDS & REDIS & S3 & DDB & EB
    WORKER -.->|Uses| ROLE

    style ALB fill:#f96,stroke:#333,stroke-width:2px
    style WORKER fill:#9cf,stroke:#333,stroke-width:2px
```

## Why ECS Fargate

- Direct VPC connectivity to private databases (no VPN needed)
- IAM roles for AWS service access (no credential management)
- Auto-scaling workers, serverless containers
- S3/DynamoDB via VPC gateway endpoints (no NAT traversal)

## Key Details

- Customer RDS/Redis accessed read-only — we don't deploy our own databases
- ALB uses path-based routing (`/api/*` → API, `/*` → UI) with Cognito auth on all routes
- Python 3.12+, Bedrock required, OpenTelemetry for tracing

---

**Related:** [CloudFormation Deployment](12-cloudformation-deployment.md) | [Docker Compose](03-docker-compose-deployment.md)
