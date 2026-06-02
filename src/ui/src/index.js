import { render } from "react-dom";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";

// Cloudscape Styles
import '@cloudscape-design/global-styles/index.css';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import './styles/global.css';

// i18n
import './i18n';

// Pages
import LandingPage from "./pages/LandingPage";
import Dashboard from "./pages/Dashboard";
import AnalysisResults from "./pages/AnalysisResults";
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

// Suppress ResizeObserver errors (benign warning from Cloudscape components)
const resizeObserverErrorHandler = (e) => {
  if (e.message === 'ResizeObserver loop completed with undelivered notifications.') {
    const resizeObserverErrDiv = document.getElementById('webpack-dev-server-client-overlay-div');
    const resizeObserverErr = document.getElementById('webpack-dev-server-client-overlay');
    if (resizeObserverErr) {
      resizeObserverErr.setAttribute('style', 'display: none');
    }
    if (resizeObserverErrDiv) {
      resizeObserverErrDiv.setAttribute('style', 'display: none');
    }
  }
};
window.addEventListener('error', resizeObserverErrorHandler);

// Theme setup - read from localStorage, default to dark
const savedTheme = localStorage.getItem('dbm-theme') || 'dark';
applyMode(savedTheme === 'light' ? Mode.Light : Mode.Dark);

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/analysis/results/:jobId" element={<AnalysisResults />} />
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
    </BrowserRouter>
  );
}

render(<App />, document.getElementById("root"));
