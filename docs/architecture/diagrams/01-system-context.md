# System Context

External actors and systems for Database Modernizer Assessment (Phase 0).

```mermaid
graph TB
    subgraph "Users"
        DBA[Database Architect]
        ITL[IT Leader]
        DEV[Developer]
    end

    subgraph "Customer AWS Account"
        DBM[Database Modernizer Assessment]
        RDS[(Customer RDS<br/>Read-only)]
        REDIS[(Customer Redis<br/>Read-only)]

        subgraph "AWS Services"
            CW[CloudWatch] & PI[Perf Insights] & RDS_API[RDS API]
            SM[Secrets Manager] & S3[S3] & DDB[DynamoDB]
            EB[EventBridge] & SF[Step Functions]
            COGNITO[Cognito] & BEDROCK[Bedrock]
        end
    end

    DBA -->|Deploy & Configure| DBM
    ITL -->|Review Reports| DBM
    DEV -->|Implement Migrations| DBM

    DBM --> RDS & REDIS
    DBM --> CW & PI & RDS_API & SM
    DBM --> S3 & DDB & EB
    DBM --> COGNITO & BEDROCK

    style DBM fill:#f9f,stroke:#333,stroke-width:4px
    style BEDROCK fill:#9cf,stroke:#333,stroke-width:2px
```

## Key Points

- Runs in customer VPC with direct private connectivity to their RDS/Redis (read-only)
- Does NOT deploy its own database instances
- Bedrock and Cognito are required
- S3/DynamoDB accessed via VPC gateway endpoints

---

**Related:** [VPC Deployment](02-vpc-deployment.md) | [High-Level Design](../high-level-design.md)
