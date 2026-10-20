import { Routes, Route, Navigate, useParams } from "react-router-dom";

// Pages
import Dashboard from "./pages/Dashboard";
import CreateAnalysis from "./pages/CreateAnalysis";
import JobMonitoringSummary from "./pages/JobMonitoringSummary";
import PatternAnalysis from "./pages/PatternAnalysis";
import AssignmentGate from "./pages/AssignmentGate";
import AnalysisResults from "./pages/AnalysisResults";

// Old results URLs redirect to /analysis/results/:jobId so bookmarks keep
// working: /analysis/results-v2/:jobId was the results page's name until its
// version suffix was dropped (#957), and /analysis/report/:jobId was the
// earlier report page, retired in #356.
function LegacyResultsRedirect() {
  const { jobId } = useParams();
  return <Navigate to={`/analysis/results/${jobId}`} replace />;
}

// The app's route table, kept separate from index.js (which renders into the
// DOM on import) so tests can mount it inside a MemoryRouter.
export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="/analysis/results/:jobId" element={<AnalysisResults />} />
      <Route path="/analysis/results-v2/:jobId" element={<LegacyResultsRedirect />} />
      <Route path="/analysis/report/:jobId" element={<LegacyResultsRedirect />} />
      <Route path="/analysis/create" element={<CreateAnalysis />} />
      <Route path="/analysis/monitor/summary/:jobId" element={<JobMonitoringSummary />} />
      <Route path="/analysis/patterns/:jobId" element={<PatternAnalysis />} />
      <Route path="/analysis/assignments/:jobId" element={<AssignmentGate />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
