# Database Modernizer Assessment - UI Requirements

**Version:** 1.1
**Date:** February 5, 2026
**UI Framework:** React + TypeScript with AWS Cloudscape Design System

---

## Overview

Database Modernizer Assessment needs a user-friendly interface that allows users to:

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
│  Database Modernizer Assessment                    [New Analysis] [⚙️]  │
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

## Current Route Inventory (#357)

The views above describe the original pre-build requirements doc; the actual
implementation diverged (no WebSocket, no TypeScript, polling instead of a
live socket) once the hosted deployment was retired and the UI moved to the
local FastAPI backend. This table reflects what `src/ui/src/AppRoutes.js`
actually serves today, with a keep/merge/remove call for each route and page.
Every kept route has a routing-table test in `src/ui/src/__tests__/AppRoutes.test.js`.

| Route | Page | Decision | Why |
|---|---|---|---|
| `/` | — (redirect) | Keep | Entry redirect to `/dashboard`. |
| `/dashboard` | `Dashboard.js` | Keep | Job list and hub; linked from the side nav and from every page's "back to dashboard" action. |
| `/analysis/create` | `CreateAnalysis.js` | Keep | Starts a new analysis; linked from the side nav and the Dashboard's "New analysis" button. |
| `/analysis/monitor/:jobId` | `JobMonitoring.js` | **Removed** (#957) | No link reached it except the engine-analysis page's breadcrumb; its "Download logs" and "Download artifact" buttons only logged to the console. `JobMonitoringSummary.js` is the job page. |
| `/analysis/monitor/summary/:jobId` | `JobMonitoringSummary.js` | Keep | Post-phase hub; linked from `CreateAnalysis`, `AssignmentGate`, `Dashboard`. |
| `/analysis/assignments/:jobId` | `AssignmentGate.js` | Keep | Assignment approval gate; linked from `JobMonitoringSummary`; the intended UI for the `/assign` gate (the command currently prints a wrong URL, #439). |
| `/analysis/engine-analysis/:jobId` | `EngineAnalysis.js` | **Removed** (#957) | Pre-assignment per-engine figures (query count, average fit) that contradicted the results page; reached only from the Dashboard's "View analysis" action, removed with it. |
| `/analysis/patterns/:jobId` (requires `?target=`) | `PatternAnalysis.js` | Keep | Access-pattern drill-down; linked from `JobMonitoringSummary` and `AssignmentGate`, always with a `target`. |
| `/analysis/results/:jobId` | `AnalysisResults.js` | Keep | Final deliverable page (named `/analysis/results-v2/:jobId` and `AnalysisResults-02.js` until #957); linked from `JobMonitoringSummary` and the Dashboard's "View results" action. Shows the migration wave plan from report.json's waves and a download panel for the job's `deliverables/` folder. |
| `/analysis/results-v2/:jobId` | — (redirect to `/analysis/results/:jobId`) | Keep | Thin redirect so old bookmarks still resolve (#957). |
| `/analysis/report/:jobId` | — (redirect to `/analysis/results/:jobId`) | Keep | Thin redirect so old bookmarks still resolve (#356). The earlier `/analysis/results/:jobId` page it replaced was retired in #185; that URL now serves the current page. |
| `/settings` | `Settings.js` | **Removed** (#437) | Save and Test only logged to the console, and its fields (S3 bucket, DynamoDB table, IAM role, default analysis options) belonged to the retired hosted deployment; nothing in the local pipeline read them. The page, its side-nav link and `src/api/routes/settings.py` are gone. |
| `/analysis/local` | `LocalAnalysis.js` | **Removed** | Fetched static files (`/data/collector-output.json`, `/data/analysis-*.json`) that no script or build step has ever produced; always rendered empty. Dead since the local API backend replaced this prototype. |
| `/debug` | `Debug.js` | **Removed** | Dev-only raw API console, not part of the `/modernize` flow or any deliverable. |
| `*` | — (redirect to `/`) | Keep | Fallback for unknown paths. |
| *(no route)* | `LandingPage.js` | **Removed** | Not imported by `AppRoutes.js` or anywhere else; unreachable. |

Side navigation (`src/ui/src/config/GlobalConfigurations.js`,
`SideNavigationConfigurations`) dropped the "Local analysis" and "API Debug"
entries along with their pages. The Dashboard's "View analysis" action went
with the engine-analysis page (#957). Every side-nav link opens its own route
(`src/ui/src/__tests__/AppRoutes.test.js`).

---

## Related Documentation

- Cloudscape Design System: <https://cloudscape.design/>
- API Specifications: `../architecture/high-level-design.md` (Section 6)
- WebSocket Protocol: `../architecture/decisions/ADR-003-progress-reporting-architecture.md`
