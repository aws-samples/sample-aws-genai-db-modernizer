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
jest.mock('../pages/Settings', () => mockPage('Settings'));
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

test('/dashboard renders the dashboard directly', () => {
  expect(renderAt('/dashboard')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('/analysis/create renders the create-analysis page', () => {
  expect(renderAt('/analysis/create')).toEqual({
    page: 'CreateAnalysis', jobId: '', pathname: '/analysis/create',
  });
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

test('/analysis/assignments/:jobId renders the assignment gate', () => {
  expect(renderAt('/analysis/assignments/job-42')).toEqual({
    page: 'AssignmentGate', jobId: 'job-42', pathname: '/analysis/assignments/job-42',
  });
});

test('/analysis/engine-analysis/:jobId renders the engine-analysis drill-down', () => {
  expect(renderAt('/analysis/engine-analysis/job-42')).toEqual({
    page: 'EngineAnalysis', jobId: 'job-42', pathname: '/analysis/engine-analysis/job-42',
  });
});

test('/analysis/patterns/:jobId renders pattern analysis (with its required ?target=)', () => {
  expect(renderAt('/analysis/patterns/job-42?target=dynamodb')).toEqual({
    page: 'PatternAnalysis', jobId: 'job-42', pathname: '/analysis/patterns/job-42',
  });
});

test('/analysis/results-v2/:jobId renders the results page directly', () => {
  expect(renderAt('/analysis/results-v2/job-42')).toEqual({
    page: 'AnalysisResultsV2', jobId: 'job-42', pathname: '/analysis/results-v2/job-42',
  });
});

test('legacy /analysis/results/:jobId redirects to results-v2, keeping the job id', () => {
  expect(renderAt('/analysis/results/job-42')).toEqual({
    page: 'AnalysisResultsV2', jobId: 'job-42', pathname: '/analysis/results-v2/job-42',
  });
});

test('legacy /analysis/report/:jobId redirects to results-v2, keeping the job id (#356)', () => {
  expect(renderAt('/analysis/report/job-42')).toEqual({
    page: 'AnalysisResultsV2', jobId: 'job-42', pathname: '/analysis/results-v2/job-42',
  });
});

test('/settings renders the settings page', () => {
  expect(renderAt('/settings')).toEqual({ page: 'Settings', jobId: '', pathname: '/settings' });
});

test('the retired ReportResults page module is gone (#356)', () => {
  expect(() => jest.requireActual('../pages/ReportResults')).toThrow();
});

test('the removed /debug page and route are gone (#357)', () => {
  expect(() => jest.requireActual('../pages/Debug')).toThrow();
  expect(renderAt('/debug')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the removed /analysis/local page and route are gone (#357)', () => {
  expect(() => jest.requireActual('../pages/LocalAnalysis')).toThrow();
  expect(renderAt('/analysis/local')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the unused, unrouted LandingPage module is gone (#357)', () => {
  expect(() => jest.requireActual('../pages/LandingPage')).toThrow();
});

test('unknown paths fall through * to / and then the dashboard', () => {
  expect(renderAt('/no/such/route')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});
