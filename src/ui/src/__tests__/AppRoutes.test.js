// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

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
jest.mock('../pages/AssignmentGate', () => mockPage('AssignmentGate'));
jest.mock('../pages/AnalysisResults', () => mockPage('AnalysisResults'));
jest.mock('../pages/GraphExplorer', () => mockPage('GraphExplorer'));

// The job routes look the assessment up before mounting their page (#2307).
const mockExecute = jest.fn();
jest.mock('../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../components/AssessmentNotFound', () => ({
  __esModule: true,
  default: ({ jobId }) => <div data-page="AssessmentNotFound" data-job-id={jobId} />,
  AssessmentLookupPending: () => <div data-page="AssessmentLookupPending" />,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let location;

function LocationProbe() {
  location = useLocation();
  return null;
}

async function renderAt(path) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>,
    );
  });
  await act(async () => {});
  const page = container.querySelector('[data-page]');
  return {
    page: page && page.getAttribute('data-page'),
    jobId: page && page.getAttribute('data-job-id'),
    pathname: location.pathname,
  };
}

beforeEach(() => {
  mockExecute.mockReset();
  mockExecute.mockResolvedValue({ job: { success: true, status: 200 } });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('/ redirects to the dashboard', async () => {
  expect(await renderAt('/')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('/dashboard renders the dashboard directly', async () => {
  expect(await renderAt('/dashboard')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('/analysis/create renders the create-analysis page', async () => {
  expect(await renderAt('/analysis/create')).toEqual({
    page: 'CreateAnalysis', jobId: '', pathname: '/analysis/create',
  });
});

test('a job route renders its page with :jobId from the URL', async () => {
  expect(await renderAt('/analysis/assignments/job-7')).toEqual({
    page: 'AssignmentGate', jobId: 'job-7', pathname: '/analysis/assignments/job-7',
  });
});

test('/analysis/monitor/summary/:jobId renders the assessment summary', async () => {
  expect(await renderAt('/analysis/monitor/summary/job-42')).toEqual({
    page: 'JobMonitoringSummary', jobId: 'job-42', pathname: '/analysis/monitor/summary/job-42',
  });
});

test('/analysis/assignments/:jobId renders the assignment gate', async () => {
  expect(await renderAt('/analysis/assignments/job-42')).toEqual({
    page: 'AssignmentGate', jobId: 'job-42', pathname: '/analysis/assignments/job-42',
  });
});

// #2300: the pattern-analysis page is gone; its old URL sends the visitor to the
// engine's part of the results page (the same tab, no history entry), so saved links work.
test('old /analysis/patterns/:jobId?target=<engine> links redirect to that engine on the results page (#2300)', async () => {
  expect(await renderAt('/analysis/patterns/job-42?target=dynamodb')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
  expect(location.search).toBe('?engine=dynamodb');
  expect(location.hash).toBe('#access-patterns-section');
});

test('an old /analysis/patterns/:jobId link without a target redirects to the results page (#2300)', async () => {
  expect(await renderAt('/analysis/patterns/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
  expect(location.search).toBe('');
  expect(location.hash).toBe('#access-patterns-section');
});

test('the removed pattern-analysis page module is gone (#2300)', () => {
  expect(() => jest.requireActual('../pages/PatternAnalysis')).toThrow();
});

test('/analysis/results/:jobId renders the results page directly', async () => {
  expect(await renderAt('/analysis/results/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

test('old /analysis/results-v2/:jobId bookmarks redirect to /analysis/results, keeping the job id', async () => {
  expect(await renderAt('/analysis/results-v2/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

test('legacy /analysis/report/:jobId redirects to /analysis/results, keeping the job id (#356)', async () => {
  expect(await renderAt('/analysis/report/job-42')).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
});

// #2313 review: an old link to a section of the results page keeps it, so it lands on
// the section's tab.
test.each(['results-v2', 'report'])('/analysis/%s/:jobId keeps the query and the section the link names', async (route) => {
  expect(await renderAt(`/analysis/${route}/job-42?tab=plan#close-calls-section`)).toEqual({
    page: 'AnalysisResults', jobId: 'job-42', pathname: '/analysis/results/job-42',
  });
  expect(location.search).toBe('?tab=plan');
  expect(location.hash).toBe('#close-calls-section');
});

test('the version-suffixed results page module is gone', () => {
  expect(() => jest.requireActual('../pages/AnalysisResultsV2')).toThrow(/Cannot find module/);
});

test('the removed job-monitoring page and its /analysis/monitor/:jobId route are gone', async () => {
  expect(() => jest.requireActual('../pages/JobMonitoring')).toThrow();
  expect(await renderAt('/analysis/monitor/job-42')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the removed engine-analysis page and its route are gone', async () => {
  expect(() => jest.requireActual('../pages/EngineAnalysis')).toThrow();
  expect(() => jest.requireActual('../hooks/useEngineAnalysis')).toThrow();
  expect(await renderAt('/analysis/engine-analysis/job-42')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

// #437: the Settings page never saved or tested anything, and its fields belonged to
// the retired hosted deployment; the page, its route and its API are gone.
test('the removed Settings page and its /settings route are gone (#437)', async () => {
  expect(() => jest.requireActual('../pages/Settings')).toThrow();
  expect(await renderAt('/settings')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the side nav no longer links to Settings (#437)', () => {
  expect(SideNavigationConfigurations.items.map(item => item.href)).not.toContain('/settings');
});

test('the retired ReportResults page module is gone (#356)', () => {
  expect(() => jest.requireActual('../pages/ReportResults')).toThrow();
});

test('the removed /debug page and route are gone (#357)', async () => {
  expect(() => jest.requireActual('../pages/Debug')).toThrow();
  expect(await renderAt('/debug')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

test('the removed /analysis/local page and route are gone (#357)', async () => {
  expect(() => jest.requireActual('../pages/LocalAnalysis')).toThrow();
  expect(await renderAt('/analysis/local')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
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
  async ({ href }) => {
    expect((await renderAt(href)).pathname).toBe(href);
  },
);

test('unknown paths fall through * to / and then the dashboard', async () => {
  expect(await renderAt('/no/such/route')).toEqual({ page: 'Dashboard', jobId: '', pathname: '/dashboard' });
});

// #308: the experimental explorer has its own job route; the page itself
// decides (from the server flag) whether to render or send the user home.
// The page is loaded on demand (React.lazy), so the route renders it once the
// import resolves.
async function renderLazyAt(path) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>,
    );
  });
  await act(async () => {});
  const page = container.querySelector('[data-page]');
  return {
    page: page && page.getAttribute('data-page'),
    jobId: page && page.getAttribute('data-job-id'),
    pathname: location.pathname,
  };
}

test('/analysis/explore/:jobId renders the experimental explorer page', async () => {
  expect(await renderLazyAt('/analysis/explore/job-42')).toEqual({
    page: 'GraphExplorer', jobId: 'job-42', pathname: '/analysis/explore/job-42',
  });
});

test('the side nav has no explorer entry (#308)', () => {
  expect(SideNavigationConfigurations.items.map(item => item.href).join(' ')).not.toMatch(/explore/);
});

// #2307: an unknown id shows one not-found state on every job route.
test.each([
  '/analysis/results/doesnotexist',
  '/analysis/monitor/summary/doesnotexist',
  '/analysis/assignments/doesnotexist',
])('%s shows the not-found state, not the page (#2307)', async (path) => {
  mockExecute.mockResolvedValue({ job: { success: false, status: 404 } });
  expect(await renderAt(path)).toEqual({
    page: 'AssessmentNotFound', jobId: 'doesnotexist', pathname: path,
  });
});

test('the graph explorer route is not behind the job lookup (#2307)', async () => {
  mockExecute.mockResolvedValue({ job: { success: false, status: 404 } });
  expect(await renderLazyAt('/analysis/explore/job-42')).toEqual({
    page: 'GraphExplorer', jobId: 'job-42', pathname: '/analysis/explore/job-42',
  });
});
