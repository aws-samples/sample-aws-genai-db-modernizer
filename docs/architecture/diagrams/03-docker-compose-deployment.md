# Docker Compose Deployment (Development)

Local development and demos only. Not for production.

```mermaid
graph TB
    subgraph "Developer Laptop (Docker Compose)"
        UI[UI Service :8080]
        API[API Service :8000]
        WORKER[Worker]
        ARTIFACTS[./artifacts/<br/>LocalArtifactStore]
    end

    subgraph "Customer AWS Account"
        RDS[(RDS<br/>Public endpoint or VPN)]
        CW[CloudWatch] & PI[Perf Insights] & SM[Secrets Manager]
        BEDROCK[Bedrock]
    end

    USER[Browser] --> UI --> API --> WORKER
    WORKER --> ARTIFACTS
    WORKER -->|boto3| RDS & CW & PI & SM & BEDROCK
```

## Limitations

- Requires public RDS endpoint or VPN (private RDS won't work)
- AWS credentials stored on laptop
- Limited to 1-2 concurrent jobs
- Best for: local dev, demos, quick evaluation

## Prerequisites

- Docker Desktop, AWS CLI configured
- Network access to RDS
- IAM permissions: rds:Describe*, cloudwatch:*, pi:*, secretsmanager:*, bedrock:*, cognito-idp:*, events:*

---

**Related:** [ECS Fargate Deployment](02-vpc-deployment.md)
