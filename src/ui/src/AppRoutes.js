import { Routes, Route, Navigate, useParams } from "react-router-dom";

// Pages
import Dashboard from "./pages/Dashboard";
import CreateAnalysis from "./pages/CreateAnalysis";
import JobMonitoring from "./pages/JobMonitoring";
import JobMonitoringSummary from "./pages/JobMonitoringSummary";
import PatternAnalysis from "./pages/PatternAnalysis";
import ReportResults from "./pages/ReportResults";
import Settings from "./pages/Settings";
import Debug from "./pages/Debug";
import LocalAnalysis from "./pages/LocalAnalysis";
import EngineAnalysis from "./pages/EngineAnalysis";
import AssignmentGate from "./pages/AssignmentGate";
import AnalysisResultsV2 from "./pages/AnalysisResults-02";

// The legacy /analysis/results/:jobId page was retired (see #185); redirect
// old bookmarks/links to the current results-v2 experience.
function LegacyResultsRedirect() {
  const { jobId } = useParams();
  return <Navigate to={`/analysis/results-v2/${jobId}`} replace />;
}

// The app's route table, kept separate from index.js (which renders into the
// DOM on import) so tests can mount it inside a MemoryRouter.
export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="/analysis/results/:jobId" element={<LegacyResultsRedirect />} />
      <Route path="/analysis/results-v2/:jobId" element={<AnalysisResultsV2 />} />
      <Route path="/analysis/create" element={<CreateAnalysis />} />
      <Route path="/analysis/monitor/:jobId" element={<JobMonitoring />} />
      <Route path="/analysis/monitor/summary/:jobId" element={<JobMonitoringSummary />} />
      <Route path="/analysis/patterns/:jobId" element={<PatternAnalysis />} />
      <Route path="/analysis/report/:jobId" element={<ReportResults />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/analysis/local" element={<LocalAnalysis />} />
      <Route path="/analysis/assignments/:jobId" element={<AssignmentGate />} />
      <Route path="/analysis/engine-analysis/:jobId" element={<EngineAnalysis />} />
      <Route path="/debug" element={<Debug />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
