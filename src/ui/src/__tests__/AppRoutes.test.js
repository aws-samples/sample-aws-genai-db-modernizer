/**
 * The app's real route table (src/AppRoutes.js) mounted in a MemoryRouter.
 * Pages are stubbed so the test exercises routing only: which page renders,
 * the final location, and that :jobId reaches the page via useParams.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, useLocation } from 'react-router-dom';
import AppRoutes from '../AppRoutes';

// jest.mock calls are hoisted above the imports, so the stub factory must be a
// hoisted function declaration (and `mock`-prefixed to be allowed in a factory).
function mockPage(name) {
  return function StubPage() {
    const { jobId } = jest.requireActual('react-router-dom').useParams();
    return <div data-page={name} data-job-id={jobId || ''} />;
  };
}
jest.mock('../pages/Dashboard', () => mockPage('Dashboard'));
jest.mock('../pages/CreateAnalysis', () => mockPage('CreateAnalysis'));
jest.mock('../pages/JobMonitoring', () => mockPage('JobMonitoring'));
jest.mock('../pages/JobMonitoringSummary', () => mockPage('JobMonitoringSummary'));
jest.mock('../pages/PatternAnalysis', () => mockPage('PatternAnalysis'));
jest.mock('../pages/ReportResults', () => mockPage('ReportResults'));
jest.mock('../pages/Settings', () => mockPage('Settings'));
jest.mock('../pages/Debug', () => mockPage('Debug'));
jest.mock('../pages/LocalAnalysis', () => mockPage('LocalAnalysis'));
jest.mock('../pages/EngineAnalysis', () => mockPage('EngineAnalysis'));
jest.mock('../pages/AssignmentGate', () => mockPage('AssignmentGate'));
jest.mock('../pages/AnalysisResults-02', () => mockPage('AnalysisResultsV2'));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let location;

function LocationProbe() {
  location = useLocation();
  return null;
}

function renderAt(path) {
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>,
    );
  });
  const page = container.querySelector('[data-page]');
  return {
    page: page && page.getAttribute('data-page'),
    jobId: page && page.getAttribute('data-job-id'),
    pathname: location.pathname,
  };
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('/ redirects to the dashboard', () => {
  expect(renderAt('/')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('a job route renders its page with :jobId from the URL', () => {
  expect(renderAt('/analysis/monitor/job-42')).toEqual({
    page: 'JobMonitoring', jobId: 'job-42', pathname: '/analysis/monitor/job-42',
  });
});

test('nested monitor/summary route wins over monitor/:jobId', () => {
  expect(renderAt('/analysis/monitor/summary/job-42')).toEqual({
    page: 'JobMonitoringSummary', jobId: 'job-42', pathname: '/analysis/monitor/summary/job-42',
  });
});

test('legacy /analysis/results/:jobId redirects to results-v2, keeping the job id', () => {
  expect(renderAt('/analysis/results/job-42')).toEqual({
    page: 'AnalysisResultsV2', jobId: 'job-42', pathname: '/analysis/results-v2/job-42',
  });
});

test('unknown paths fall through * to / and then the dashboard', () => {
  expect(renderAt('/no/such/route')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});
