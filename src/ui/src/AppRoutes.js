// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { lazy, Suspense } from "react";
import { Routes, Route, Navigate, useLocation, useParams, useSearchParams } from "react-router-dom";

// Pages
import Dashboard from "./pages/Dashboard";
import CreateAnalysis from "./pages/CreateAnalysis";
import JobMonitoringSummary from "./pages/JobMonitoringSummary";
import AssignmentGate from "./pages/AssignmentGate";
import AnalysisResults from "./pages/AnalysisResults";
import JobRouteGuard from "./components/JobRouteGuard";
import { resultsEngineHref } from "./utils/resultsLinks";
// Experimental (#308): renders only when the server flag is on, otherwise it
// redirects home like an unknown path. Loaded on demand, so its graph library
// (cytoscape, cola) stays out of the main bundle.
const GraphExplorer = lazy(() => import("./pages/GraphExplorer"));

// Old results URLs redirect to /analysis/results/:jobId so bookmarks keep
// working: /analysis/results-v2/:jobId was the results page's name until its
// version suffix was dropped (#957), and /analysis/report/:jobId was the
// earlier report page, retired in #356.
function LegacyResultsRedirect() {
  const { jobId } = useParams();
  // The query and the section a link names stay (#2313: the hash opens its tab).
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: `/analysis/results/${jobId}`, search, hash }} replace />;
}

// The pattern-analysis page was removed (#2300): its content lives in the results
// page. An old /analysis/patterns/:jobId?target=<engine> link goes to that engine's
// part of the results page, so saved links keep working.
function LegacyPatternsRedirect() {
  const { jobId } = useParams();
  const [params] = useSearchParams();
  return <Navigate to={resultsEngineHref(jobId, params.get("target"))} replace />;
}

// The app's route table, kept separate from index.js (which renders into the
// DOM on import) so tests can mount it inside a MemoryRouter.
export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="/analysis/results/:jobId" element={<JobRouteGuard><AnalysisResults /></JobRouteGuard>} />
      <Route path="/analysis/results-v2/:jobId" element={<LegacyResultsRedirect />} />
      <Route path="/analysis/report/:jobId" element={<LegacyResultsRedirect />} />
      <Route path="/analysis/create" element={<CreateAnalysis />} />
      <Route path="/analysis/monitor/summary/:jobId" element={<JobRouteGuard><JobMonitoringSummary /></JobRouteGuard>} />
      <Route path="/analysis/patterns/:jobId" element={<LegacyPatternsRedirect />} />
      <Route path="/analysis/assignments/:jobId" element={<JobRouteGuard><AssignmentGate /></JobRouteGuard>} />
      <Route
        path="/analysis/explore/:jobId"
        element={
          <Suspense fallback={null}>
            <GraphExplorer />
          </Suspense>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
