/**
 * The app's real route table (src/AppRoutes.js) mounted in a MemoryRouter.
 * Pages are stubbed so the test exercises routing only: which page renders,
 * the final location, and that :jobId reaches the page via useParams.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, useLocation } from 'react-router-dom';
import AppRoutes from '../AppRoutes';

// GlobalConfigurations.js imports Cloudscape's Box (ESM, not transformed by
// Jest) for its table helpers; only its plain config objects are read here.
jest.mock('@cloudscape-design/components/box', () => () => null);
import { SideNavigationConfigurations } from '../config/GlobalConfigurations';

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
jest.mock('../pages/JobMonitoringSummary', () => mockPage('JobMonitoringSummary'));
jest.mock('../pages/PatternAnalysis', () => mockPage('PatternAnalysis'));
jest.mock('../pages/AssignmentGate', () => mockPage('AssignmentGate'));
jest.mock('../pages/AnalysisResults', () => mockPage('AnalysisResults'));

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
  expect(renderAt('/analysis/assignments/job-7')).toEqual({
    page: 'AssignmentGate', jobId: 'job-7', pathname: '/analysis/assignments/job-7',
  });
});

test('/analysis/monitor/summary/:jobId renders the assessment summary', () => {
  expect(renderAt('/analysis/monitor/summary/job-42')).toEqual({
    page: 'JobMonitoringSummary', jobId: 'job-42', pathname: '/analysis/monitor/summary/job-42',
  });
});

test('/analysis/assignments/:jobId renders the assignment gate', () => {
  expect(renderAt('/analysis/assignments/job-42')).toEqual({
    page: 'AssignmentGate', jobId: 'job-42', pathname: '/analysis/assignments/job-42',
  });
});

test('/analysis/patterns/:jobId renders pattern analysis (with its required ?target=)', () => {
  expect(renderAt('/analysis/patterns/job-42?target=dynamodb')).toEqual({
    page: 'PatternAnalysis', jobId: 'job-42', pathname: '/analysis/patterns/job-42',
  });
});

test('/analysis/results/:jobId renders the results page directly', () => {
  expect(renderAt('/analysis/results/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

test('old /analysis/results-v2/:jobId bookmarks redirect to /analysis/results, keeping the job id', () => {
  expect(renderAt('/analysis/results-v2/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

test('legacy /analysis/report/:jobId redirects to /analysis/results, keeping the job id (#356)', () => {
  expect(renderAt('/analysis/report/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

test('the version-suffixed results page module is gone', () => {
  expect(() => jest.requireActual('../pages/AnalysisResults')).toThrow();
});

test('the removed job-monitoring page and its /analysis/monitor/:jobId route are gone', () => {
  expect(() => jest.requireActual('../pages/JobMonitoring')).toThrow();
  expect(renderAt('/analysis/monitor/job-42')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the removed engine-analysis page and its route are gone', () => {
  expect(() => jest.requireActual('../pages/EngineAnalysis')).toThrow();
  expect(() => jest.requireActual('../hooks/useEngineAnalysis')).toThrow();
  expect(renderAt('/analysis/engine-analysis/job-42')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

// #437: the Settings page never saved or tested anything, and its fields belonged to
// the retired hosted deployment; the page, its route and its API are gone.
test('the removed Settings page and its /settings route are gone (#437)', () => {
  expect(() => jest.requireActual('../pages/Settings')).toThrow();
  expect(renderAt('/settings')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the side nav no longer links to Settings (#437)', () => {
  expect(SideNavigationConfigurations.items.map(item => item.href)).not.toContain('/settings');
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

// #438: the side-nav Settings link (since removed with the page, #437) pointed at
// /settings/s, which matched no route, so the catch-all sent the user back to the
// dashboard. Every side-nav link must land on its own page ("/" is the one
// deliberate redirect).
test.each(SideNavigationConfigurations.items.filter(item => item.type === 'link' && item.href !== '/'))(
  'the side-nav "$text" link ($href) opens its own route',
  ({ href }) => {
    expect(renderAt(href).pathname).toBe(href);
  },
);

test('unknown paths fall through * to / and then the dashboard', () => {
  expect(renderAt('/no/such/route')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});
