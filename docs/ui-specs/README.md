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
| `/analysis/create` | `CreateAnalysis.js` | Keep | The New assessment page; linked from the side nav and the Dashboard's "New assessment" button. With the local API it is one upload form (#2327); a deployment that is not local also offers the live-database Wizard. |
| `/analysis/monitor/:jobId` | `JobMonitoring.js` | **Removed** (#957) | No link reached it except the engine-analysis page's breadcrumb; its "Download logs" and "Download artifact" buttons only logged to the console. `JobMonitoringSummary.js` is the job page. |
| `/analysis/monitor/summary/:jobId` | `JobMonitoringSummary.js` | Keep | Post-phase hub; linked from `CreateAnalysis`, `AssignmentGate`, `Dashboard`. |
| `/analysis/assignments/:jobId` | `AssignmentGate.js` | Keep | Assignment approval gate; linked from `JobMonitoringSummary`; the intended UI for the `/assign` gate. It states the gate state: waiting, approved (only once an approval is recorded or the cloud history completed the gate), rerouted after schema design, a later version than the results, done, failed, or a neutral running state. It offers approval only while the job waits. It shows the recommendation in one sentence with its figures (queries, source tables, engines kept and removed, cache candidates) and the hottest cache candidates as a table. The figures are the assignments API's `gate_facts`, the chat gate's figures too, and describe the version the page states: its report's facts once synthesis wrote it, else its own in-scope queries (#2326). Queries is the report's `query_total_count`. A read past the gate writes no new version (#992). |
| `/analysis/engine-analysis/:jobId` | `EngineAnalysis.js` | **Removed** (#957) | Pre-assignment per-engine figures (query count, average fit) that contradicted the results page; reached only from the Dashboard's "View analysis" action, removed with it. |
| `/analysis/patterns/:jobId` | `PatternAnalysis.js` | **Removed** (#2300) | Its content moved into the results page; the two Sankey clicks that opened it now go to the engine's part of the results page, in the same tab. The route stays as a redirect to `/analysis/results/:jobId?engine=<target>#query-routing-section` (#2446: the query table, filtered to the engine), so old links still resolve. A link to the old `#access-patterns-section` lands on the query table too. |
| `/analysis/results/:jobId` | `AnalysisResults.js` | Keep | Final deliverable page (named `/analysis/results-v2/:jobId` and `AnalysisResults-02.js` until #957); linked from `JobMonitoringSummary` and the Dashboard's "View results" action. Shows the migration wave plan from report.json's waves and a download panel for the job's `deliverables/` folder. |
| `/analysis/results-v2/:jobId` | — (redirect to `/analysis/results/:jobId`) | Keep | Thin redirect so old bookmarks still resolve (#957). |
| `/analysis/report/:jobId` | — (redirect to `/analysis/results/:jobId`) | Keep | Thin redirect so old bookmarks still resolve (#356). The earlier `/analysis/results/:jobId` page it replaced was retired in #185; that URL now serves the current page. |
| `/settings` | `Settings.js` | **Removed** (#437) | Save and Test only logged to the console, and its fields (S3 bucket, DynamoDB table, IAM role, default analysis options) belonged to the retired hosted deployment; nothing in the local pipeline read them. The page, its side-nav link and `src/api/routes/settings.py` are gone. |
| `/analysis/local` | `LocalAnalysis.js` | **Removed** | Fetched static files (`/data/collector-output.json`, `/data/analysis-*.json`) that no script or build step has ever produced; always rendered empty. Dead since the local API backend replaced this prototype. |
| `/debug` | `Debug.js` | **Removed** | Dev-only raw API console, not part of the `/modernize` flow or any deliverable. |
| `*` | `NotFound.js` | Keep | Unknown paths show a not-found page at the address asked for, with links to the dashboard and the assessments list (#2393). It was a silent redirect to `/`. The deliberate redirects stay: `/`, `/analysis/results-v2/:jobId`, `/analysis/report/:jobId`, `/analysis/patterns/:jobId`, and the explorer's own redirect home when its server flag is off. |
| *(no route)* | `LandingPage.js` | **Removed** | Not imported by `AppRoutes.js` or anywhere else; unreachable. |

Side navigation (`src/ui/src/config/GlobalConfigurations.js`,
`SideNavigationConfigurations`) dropped the "Local analysis" and "API Debug"
entries along with their pages. The Dashboard's "View analysis" action went
with the engine-analysis page (#957). Every side-nav link opens its own route
(`src/ui/src/__tests__/AppRoutes.test.js`).

The side navigation lists two entries, **Assessments** (the dashboard) and **New assessment** (#2328). Every page's breadcrumb starts at **Assessments**. <!-- claim: src/ui/src/__tests__/LocaleSentenceCase.test.js -->

## UI vocabulary (#2328)

The UI uses one word for each thing, and sentence case for headings, labels, buttons, tabs, table headers and alert headers (the Cloudscape writing guideline). Only the first word and proper nouns (engine, service and product names) start with a capital.

| Term | Means | Not |
|---|---|---|
| assessment | One run of the tool on one database | analysis, job |
| query | One statement the workload runs | pattern, query pattern |
| access pattern | The target design's own unit, which serves one or more queries | pattern (alone) |
| engine | An AWS database the assessment can route queries to | target database, AWS service |
| assessment id | The id of one assessment | Job ID, Assessment ID |

"Query pattern" stays where it means a distinct query shape ("107 queries in 97 distinct query patterns"). The reports, the chat summary and the help site still say "job" for an assessment in places; #2505 brings them in line. `src/ui/src/__tests__/LocaleSentenceCase.test.js` checks that `src/ui/src/locales/en.json` never calls an assessment an analysis or a job, and that headings and buttons are in sentence case. <!-- claim: src/ui/src/__tests__/LocaleSentenceCase.test.js -->

---

## Related Documentation

- Cloudscape Design System: <https://cloudscape.design/>
- The API: [high-level design, section 5](../architecture/high-level-design.md#5-api-architecture) (the running API serves `/docs`)
