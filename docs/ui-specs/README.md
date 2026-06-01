# Database Modernizer - UI Requirements

**Version:** 1.1
**Date:** February 5, 2026
**UI Framework:** React + TypeScript with AWS Cloudscape Design System

---

## Overview

Database Modernizer needs a user-friendly interface that allows users to:

1. Configure analysis settings (target databases, table filters, etc.)
2. Monitor agent execution in real-time
3. View results and recommendations
4. Export reports

**Key Principle:** Simple, intuitive interface that showcases the tool's capabilities at a glance.

---

## UI Development Approach

**Technology Stack:**

- **Framework:** React 18 + TypeScript
- **Component Library:** AWS Cloudscape Design System (AWS-native components)
- **State Management:** React Context or Redux
- **Styling:** Cloudscape design tokens

**Why This Approach?**

- Cloudscape provides AWS-native, accessible components
- Consistent with AWS console experience
- React/TypeScript ensures maintainability and type safety

**Documentation:**

- Cloudscape: <https://cloudscape.design/>

---

## Core UI Views

### 1. Dashboard / Home View

**Purpose:** Single-page overview of system status and recent jobs

**Components:**

```
┌─────────────────────────────────────────────────────────────┐
│  Database Modernizer                    [New Analysis] [⚙️]  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  📊 System Overview                                         │
│  ┌──────────────┬──────────────┬──────────────┬──────────┐ │
│  │ Active Jobs  │ Completed    │ Failed       │ Queued   │ │
│  │     3        │     127      │     2        │    5     │ │
│  └──────────────┴──────────────┴──────────────┴──────────┘ │
│                                                             │
│  🔄 Recent Jobs                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Job ID        │ Database    │ Status      │ Progress   ││
│  ├─────────────────────────────────────────────────────────┤│
│  │ job-abc-123   │ MySQL       │ ⚙️ Running  │ ████░░ 65% ││
│  │ job-def-456   │ PostgreSQL  │ ✅ Complete │ ██████ 100%││
│  │ job-ghi-789   │ SQL Server  │ ⏸️ Paused   │ ███░░░ 45% ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key Features:**

- Quick stats cards (active, completed, failed, queued)
- Recent jobs table with status indicators
- Click job to see details
- "New Analysis" button prominent

---

### 2. New Analysis Configuration View

**Purpose:** Configure a new database analysis job

**Components:**

```
┌─────────────────────────────────────────────────────────────┐
│  New Analysis                                    [Cancel] [Start Analysis] │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Step 1: Source Database                                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Database Type: [MySQL ▼]                                ││
│  │                                                          ││
│  │ Connection Details:                                      ││
│  │   RDS Instance: [my-prod-db.abc123.us-east-1.rds...]   ││
│  │   Database Name: [ecommerce]                            ││
│  │   Credentials: [IAM Authentication ▼]                   ││
│  │                                                          ││
│  │ [Test Connection]                                        ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  Step 2: Analysis Options                                  │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Target Databases (select one or more):                  ││
│  │   ☑️ DynamoDB        ☑️ DocumentDB      ☐ ElastiCache   ││
│  │   ☐ OpenSearch      ☐ Neptune          ☐ Keyspaces     ││
│  │   ☑️ Aurora                                              ││
│  │                                                          ││
│  │ Table Filters (optional):                               ││
│  │   Include tables: [users, orders, products]             ││
│  │   Exclude tables: [temp_*, backup_*]                    ││
│  │                                                          ││
│  │ Advanced Options:                                        ││
│  │   ☑️ Include sample data                                ││
│  │   ☑️ Anonymize PII                                      ││
│  │   Query log days: [7]                                   ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  Step 3: Output Options                                    │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Report Format: [PDF ▼] [HTML] [JSON]                   ││
│  │ S3 Bucket: [s3://my-bucket/modernizer-results/]        ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key Features:**

- Step-by-step configuration
- Target database multi-select (checkboxes)
- Table include/exclude filters
- Test connection button
- Advanced options collapsible
- Clear "Start Analysis" button

---

### 3. Job Monitoring View (Real-time)

**Purpose:** Monitor agent execution in real-time with visual indicators

**Components:**

```
┌─────────────────────────────────────────────────────────────┐
│  Job: job-abc-123                           [Pause] [Cancel]│
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Overall Progress: ████████░░░░░░░░ 65% (Stage 5 of 11)    │
│                                                             │
│  🔄 Agent Execution Pipeline                                │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                                                          ││
│  │  ✅ Collector Agent                                      ││
│  │     └─ Completed in 12m 34s                             ││
│  │        • 250 tables collected                           ││
│  │        • 1,500 queries analyzed                         ││
│  │        • 24.6 TB database size                          ││
│  │                                                          ││
│  │  ⚙️ Analysis Agents (Running - 5 of 7 complete)         ││
│  │     ├─ ✅ DynamoDB Analysis (8m 12s)                    ││
│  │     ├─ ✅ DocumentDB Analysis (6m 45s)                  ││
│  │     ├─ ✅ ElastiCache Analysis (4m 23s)                 ││
│  │     ├─ ✅ OpenSearch Analysis (7m 56s)                  ││
│  │     ├─ ✅ Neptune Analysis (5m 34s)                     ││
│  │     ├─ ⚙️ Keyspaces Analysis (running... 3m 12s)       ││
│  │     └─ ⏳ Aurora Analysis (queued)                      ││
│  │                                                          ││
│  │  ⏳ Referee Agent (Waiting for analysis agents)         ││
│  │                                                          ││
│  │  ⏳ Schema Design Agents (Not started)                  ││
│  │                                                          ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  📊 Performance Metrics                                     │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Elapsed Time: 28m 45s                                   ││
│  │ Estimated Remaining: 15m 30s                            ││
│  │ LLM API Calls: 47                                       ││
│  │ Data Processed: 2.3 GB                                  ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  📝 Recent Activity Log                                     │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 14:23:45 - Keyspaces analysis started                   ││
│  │ 14:20:12 - Neptune analysis completed                   ││
│  │ 14:18:34 - OpenSearch analysis completed                ││
│  │ 14:15:23 - ElastiCache analysis completed               ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key Features:**

- Visual pipeline showing all agents
- Status indicators: ✅ Complete, ⚙️ Running, ⏳ Queued, ❌ Failed
- Real-time progress updates (WebSocket)
- Execution time per agent
- Key metrics from each agent
- Activity log with timestamps
- Pause/Cancel controls

**Status Icons:**

- ✅ Complete (green)
- ⚙️ Running (blue, animated)
- ⏳ Queued (gray)
- ❌ Failed (red)
- ⏸️ Paused (yellow)

---

### 4. Results View

**Purpose:** Display analysis results and recommendations

**Components:**

```
┌─────────────────────────────────────────────────────────────┐
│  Analysis Results: job-abc-123              [Export Report] │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  📊 Executive Summary                                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Source Database: MySQL 8.0 (24.6 TB, 250 tables)        ││
│  │ Analysis Date: Feb 2, 2026                              ││
│  │ Recommended Architecture: Hybrid (DynamoDB + Aurora)    ││
│  │                                                          ││
│  │ 💰 TCO Analysis:                                        ││
│  │   Current Monthly Cost: $5,000                          ││
│  │   Projected Monthly Cost: $2,800                        ││
│  │   Savings: $2,200/month (44%)                           ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  🎯 Recommendations (Ranked by Priority)                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 🔴 High Priority                                        ││
│  │                                                          ││
│  │ 1. Migrate user sessions to DynamoDB                    ││
│  │    • Impact: High | Confidence: 95% | Effort: Low      ││
│  │    • Rationale: Key-value access pattern, high read    ││
│  │      throughput (141K IOPS)                             ││
│  │    • Tables: sessions, user_tokens (2 tables)          ││
│  │    • Estimated Savings: $800/month                      ││
│  │    [View Details] [Generate Schema]                     ││
│  │                                                          ││
│  │ 2. Migrate product catalog to DocumentDB               ││
│  │    • Impact: High | Confidence: 90% | Effort: Medium   ││
│  │    • Rationale: Flexible schema, nested documents      ││
│  │    • Tables: products, categories (2 tables)           ││
│  │    • Estimated Savings: $600/month                      ││
│  │    [View Details] [Generate Schema]                     ││
│  │                                                          ││
│  │ 🟡 Medium Priority                                      ││
│  │                                                          ││
│  │ 3. Keep transactional data in Aurora MySQL             ││
│  │    • Impact: Medium | Confidence: 85% | Effort: Low    ││
│  │    • Rationale: Complex transactions, referential      ││
│  │      integrity required                                 ││
│  │    • Tables: orders, payments, invoices (3 tables)     ││
│  │    • Estimated Savings: $400/month                      ││
│  │    [View Details]                                       ││
│  │                                                          ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  📈 Detailed Analysis by Target Database                   │
│  [DynamoDB] [DocumentDB] [Aurora] [ElastiCache] [Others]   │
│                                                             │
│  🏗️ Architecture Diagram                                   │
│  [View Interactive Diagram]                                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key Features:**

- Executive summary with TCO
- Prioritized recommendations (High/Medium/Low)
- Impact, confidence, effort indicators
- Estimated savings per recommendation
- Expandable details per recommendation
- Generate schema button
- Tabs for detailed analysis per target database
- Interactive architecture diagram
- Export report button (PDF/HTML/JSON)

---

### 5. Settings View

**Purpose:** Configure system-wide settings

**Components:**

```
┌─────────────────────────────────────────────────────────────┐
│  Settings                                        [Save]      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  🔧 General Settings                                        │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Default Target Databases:                               ││
│  │   ☑️ DynamoDB    ☑️ DocumentDB    ☑️ ElastiCache        ││
│  │   ☑️ OpenSearch  ☑️ Neptune       ☑️ Keyspaces          ││
│  │   ☑️ Aurora                                              ││
│  │                                                          ││
│  │ Default Analysis Options:                               ││
│  │   ☑️ Include sample data                                ││
│  │   ☑️ Anonymize PII                                      ││
│  │   Query log days: [7]                                   ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  ☁️ AWS Configuration                                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Default Region: [us-east-1 ▼]                           ││
│  │ S3 Bucket: [s3://my-bucket/modernizer-results/]        ││
│  │ IAM Role: [arn:aws:iam::123456789:role/ModernizerRole] ││
│  │                                                          ││
│  │ Bedrock (Optional):                                     ││
│  │   ☑️ Enable AI-powered analysis                         ││
│  │   Model: [Claude 3 Sonnet ▼]                            ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  🔔 Notifications                                           │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Email notifications: [user@example.com]                 ││
│  │   ☑️ Job completed                                      ││
│  │   ☑️ Job failed                                         ││
│  │   ☐ Job started                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Technical Implementation

### Cloudscape Components to Use

```typescript
import {
  AppLayout,
  Button,
  Cards,
  Container,
  Header,
  ProgressBar,
  SpaceBetween,
  StatusIndicator,
  Table,
  Tabs,
  FormField,
  Input,
  Select,
  Checkbox,
  Multiselect
} from '@cloudscape-design/components';
```

### Real-time Updates (WebSocket)

```typescript
// Connect to WebSocket for job updates
const ws = new WebSocket(`wss://api-server:8000/ws/analyses/${jobId}`);

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);

  // Update UI based on stage
  updateAgentStatus(update.stage, update.status);
  updateProgressBar(update.percent_complete);
  addActivityLog(update.message);
};
```

### State Management

```typescript
// Use React Context or Redux for state
interface AppState {
  jobs: Job[];
  currentJob: Job | null;
  settings: Settings;
  agentStatuses: AgentStatus[];
}
```

---

## UI Flow

```
Dashboard
    ↓
[New Analysis] button
    ↓
Configuration View
    ↓
[Start Analysis] button
    ↓
Job Monitoring View (real-time updates)
    ↓
Results View (when complete)
    ↓
[Export Report] or [New Analysis]
```

---

## Responsive Design

- Desktop: Full layout with sidebar navigation
- Tablet: Collapsible sidebar
- Mobile: Bottom navigation, stacked cards

---

## Accessibility

- WCAG 2.1 AA compliant
- Keyboard navigation
- Screen reader support
- High contrast mode
- Focus indicators

---

## Performance

- Lazy load results data
- Paginate large tables
- WebSocket for real-time updates (not polling)
- Cache static data (target database info)

---

## Next Steps

1. Set up React + TypeScript development environment
2. Install Cloudscape Design System
3. Create wireframes for each view
4. Implement Dashboard view first (most visible)
5. Add Job Monitoring view (most complex)
6. Integrate with FastAPI backend
7. Add WebSocket support for real-time updates
8. User testing with sample data

---

## Related Documentation

- Cloudscape Design System: <https://cloudscape.design/>
- API Specifications: `../architecture/high-level-design.md` (Section 6)
- WebSocket Protocol: `../architecture/decisions/ADR-003-progress-reporting-architecture.md`
