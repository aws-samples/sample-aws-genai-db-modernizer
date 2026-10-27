# UI route inventory

The local UI is a React single-page app (JavaScript, Cloudscape Design System) in `src/ui/`. It polls the FastAPI backend; there is no WebSocket. This page is the inventory of its routes and pages, with a keep or remove call for each. (The February 2026 requirements spec that used to precede it described a TypeScript UI with a WebSocket and an S3 output option; it is removed, and git history keeps it. The results page sections are described in [Understand the results](../understand-the-results.md).)

## Routes (#357)

This table reflects what `src/ui/src/AppRoutes.js`
actually serves today, with a keep/merge/remove call for each route and page.
Every kept route has a routing-table test in `src/ui/src/__tests__/AppRoutes.test.js`.

| Route | Page | Decision | Why |
|---|---|---|---|
| `/` | — (redirect) | Keep | Entry redirect to `/dashboard`. |
| `/dashboard` | `Dashboard.js` | Keep | Job list and hub; linked from the side nav and from every page's "back to dashboard" action. |
| `/analysis/create` | `CreateAnalysis.js` | Keep | Starts a new analysis; linked from the side nav and the Dashboard's "New analysis" button. |
| `/analysis/monitor/:jobId` | `JobMonitoring.js` | **Removed** (#957) | No link reached it except the engine-analysis page's breadcrumb; its "Download logs" and "Download artifact" buttons only logged to the console. `JobMonitoringSummary.js` is the job page. |
| `/analysis/monitor/summary/:jobId` | `JobMonitoringSummary.js` | Keep | Post-phase hub; linked from `CreateAnalysis`, `AssignmentGate`, `Dashboard`. |
| `/analysis/assignments/:jobId` | `AssignmentGate.js` | Keep | Assignment approval gate; linked from `JobMonitoringSummary`; the intended UI for the `/assign` gate. |
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
- The API: [high-level design, section 5](../architecture/high-level-design.md#5-api-architecture) (the running API serves `/docs`)
