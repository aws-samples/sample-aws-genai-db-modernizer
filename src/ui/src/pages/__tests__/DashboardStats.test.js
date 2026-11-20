// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2306: the dashboard's figures. With the API down the page shows one error
 * alert with a retry and dashes, never 0 / 0.0% / 0.0h or "No records"; the
 * stats are plain values, not links without a target; the empty list says
 * "No assessments yet" and leads to a new assessment. Cloudscape ships ESM this
 * repo's Jest config cannot transform, so its components are DOM stand-ins.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import Dashboard from '../Dashboard';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = jest.requireActual('../../locales/en.json')[key];
      return text === undefined ? key : text.replace('{{count}}', (opts && opts.count) || '');
    },
  }),
}));

const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
// The page shell is Cloudscape's layout machinery, not what is under test; the table,
// the alert, the key-value pairs, the buttons and the containers are the real components.
function mockStub(tag) {
  return { __esModule: true, default: ({ children }) => <div data-c={tag}>{children}</div> };
}
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: ({ content }) => <main>{content}</main> }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => mockStub('crumbs'));
jest.mock('@cloudscape-design/components/side-navigation', () => mockStub('nav'));
jest.mock('@cloudscape-design/components/modal', () => mockStub('modal'));

global.IS_REACT_ACT_ENVIRONMENT = true;
// jsdom has no ResizeObserver, which the real table's sticky header and resizable columns use.
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const DOWN = { error: { message: 'Network error' }, status: 'Unknown' };
const STATS = {
  success: true, total_assessments: 6, active_jobs: 2, success_rate_percent: 83.3,
  average_duration_hours: 1.5, completed_today: 4, last_analysis_at: null,
};
const JOB = {
  job_id: 'job-1', database_name: 'wordpress', source_database_type: 'mysql',
  status: 'completed', created_at: '2026-11-01T10:00:00Z', duration_seconds: 60,
};
const UP = { 'get-assessments': { success: true, assessments: [] }, 'get-dashboard-stats': STATS };
const DOWN_ALL = { 'get-assessments': DOWN, 'get-dashboard-stats': DOWN };

let container;
let root;

async function renderDashboard(results) {
  mockExecute.mockResolvedValue(results);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<Dashboard />); });
}

beforeEach(() => {
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  mockExecute.mockReset();
  jest.useRealTimers();
  jest.restoreAllMocks();
});

const terms = () => [...container.querySelectorAll('dt')].map((d) => d.textContent);
const stat = (label) => container.querySelectorAll('dd')[terms().indexOf(label)].textContent;
const STAT_LABELS = ['Total assessments', 'Active assessments', 'Success rate', 'Average time', 'Last assessment', 'Completed today'];
const tableHeading = () => [...container.querySelectorAll('h2')].find((h) => h.textContent.includes('Recent assessments'));
const alertWrapper = () => container.querySelector('[role="alert"]');

describe('API down', () => {
  test('one error alert with a retry button, and no request URL', async () => {
    await renderDashboard(DOWN_ALL);
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    const alert = alertWrapper();
    expect(alert.textContent).toContain('Check that the API is running');
    expect(alert.textContent).not.toMatch(/http|api\.invalid|\/api\/v1/);
    expect(alert.querySelector('button').textContent).toBe('Retry');
    expect(container.textContent).not.toContain('API Error');
  });

  test('the alert is announced: the real Alert sits inside a live region', async () => {
    await renderDashboard(DOWN_ALL);
    const realAlert = alertWrapper().querySelector('[role="group"]');
    // Cloudscape's own Alert is role=group with no live region of its own.
    expect(realAlert.getAttribute('role')).toBe('group');
    expect(realAlert.querySelector('[aria-live]')).toBeNull();
    expect(realAlert.closest('[role="alert"], [aria-live="assertive"]')).not.toBeNull();
    expect(alertWrapper().textContent).toContain('The dashboard could not load');
  });

  test('no figure is a zero: every stat is a dash', async () => {
    await renderDashboard(DOWN_ALL);
    for (const label of STAT_LABELS) expect(stat(label)).toBe('—');
    expect(container.textContent).not.toMatch(/0\.0%|0\.0h/);
  });

  test('the table says the list is unavailable, not empty, and shows no count', async () => {
    await renderDashboard(DOWN_ALL);
    expect(container.textContent).toContain('Assessments unavailable');
    expect(container.textContent).not.toContain('No assessments yet');
    expect(container.textContent).not.toContain('No records');
    expect(tableHeading().textContent).toBe('Recent assessments');
    expect(container.textContent).not.toContain('(0)');
  });

  test('retry loads again, shows a loading button meanwhile, and clears the alert on success', async () => {
    await renderDashboard(DOWN_ALL);
    expect(mockExecute).toHaveBeenCalledTimes(1);
    let finish;
    mockExecute.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    expect(alertWrapper().querySelector('button').className).not.toMatch(/disabled/);
    await act(async () => { alertWrapper().querySelector('button').click(); });
    expect(mockExecute).toHaveBeenCalledTimes(2);
    // Cloudscape's loading button takes the disabled style and cannot be pressed twice.
    expect(alertWrapper().querySelector('button').className).toMatch(/disabled/);
    await act(async () => { finish(UP); });
    expect(alertWrapper()).toBeNull();
    expect(stat('Total assessments')).toBe('6');
  });

  test('an unexpected exception also shows the alert, not zeros', async () => {
    mockExecute.mockRejectedValue(new Error('boom'));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    jest.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { root.render(<Dashboard />); });
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(stat('Total assessments')).toBe('—');
  });
});

describe('a later refresh fails after a good load', () => {
  test('the old figures and rows are hidden, not left beside "did not load"', async () => {
    jest.useFakeTimers();
    await renderDashboard({ 'get-assessments': { success: true, assessments: [JOB] }, 'get-dashboard-stats': STATS });
    expect(stat('Total assessments')).toBe('6');
    expect(container.textContent).toContain('wordpress');
    expect(tableHeading().textContent).toContain('(1)');
    mockExecute.mockResolvedValue(DOWN_ALL);
    await act(async () => { jest.advanceTimersByTime(30000); });
    expect(alertWrapper()).not.toBeNull();
    expect(stat('Total assessments')).toBe('—');
    expect(container.textContent).not.toContain('wordpress');
    expect(container.textContent).toContain('Assessments unavailable');
    expect(tableHeading().textContent).not.toContain('(');
  });
});

describe('while the first load is pending', () => {
  test('stats show a loading indicator and the table title has no count', async () => {
    mockExecute.mockReturnValue(new Promise(() => {}));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => { root.render(<Dashboard />); });
    for (const label of STAT_LABELS) expect(stat(label)).toBe('Loading...');
    expect(container.textContent).not.toMatch(/0\.0%|0\.0h/);
    expect(tableHeading().textContent).toBe('Recent assessments');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('API up', () => {
  test('figures come from the stats, formatted by the shared formatter, in one container', async () => {
    await renderDashboard(UP);
    expect(stat('Total assessments')).toBe('6');
    expect(stat('Active assessments')).toBe('2');
    expect(stat('Success rate')).toBe('83.3%');
    expect(stat('Average time')).toBe('1.5h');
    expect(stat('Completed today')).toBe('4');
    expect(stat('Last assessment')).toBe('—');
    // "Active assessments" appears once, not in a second container (#2306).
    expect(terms().filter((x) => x === 'Active assessments')).toHaveLength(1);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  test('the figures keep the large-number style', async () => {
    await renderDashboard(UP);
    const value = container.querySelectorAll('dd')[0].firstElementChild;
    expect(value.className).toMatch(/value-large|display-l|large/);
  });

  test('a real zero from the API still prints as 0, and an empty list says (0)', async () => {
    await renderDashboard({ ...UP, 'get-dashboard-stats': { ...STATS, total_assessments: 0, active_jobs: 0, success_rate_percent: 0, completed_today: 0 } });
    expect(stat('Total assessments')).toBe('0');
    expect(stat('Success rate')).toBe('0.0%');
    expect(tableHeading().textContent).toContain('(0)');
  });

  test('no stat is a link: none has a target to go to', async () => {
    await renderDashboard(UP);
    expect(container.querySelectorAll('dl a, dd a')).toHaveLength(0);
  });

  test('the empty list says "No assessments yet" and leads to a new assessment', async () => {
    await renderDashboard(UP);
    expect(container.textContent).toContain('No assessments yet');
    expect(container.textContent).not.toContain('No records');
    const link = [...container.querySelectorAll('a')].find((a) => a.textContent.includes('New assessment') && a.closest('table'));
    expect(link.getAttribute('href')).toBe('/analysis/create');
  });
});

// #2393: the not-found page links to /dashboard#assessments.
describe('assessments anchor', () => {
  afterEach(() => window.history.replaceState(null, '', '/'));

  test('the list section has the id the not-found link targets', async () => {
    await renderDashboard(UP);
    const section = container.querySelector('#assessments');
    expect(section).not.toBeNull();
    expect(section.textContent).toContain('Recent assessments');
    expect(document.activeElement).not.toBe(section);
  });

  test('opening /dashboard#assessments moves focus to the list', async () => {
    window.history.replaceState(null, '', '/dashboard#assessments');
    await renderDashboard(UP);
    expect(document.activeElement).toBe(container.querySelector('#assessments'));
  });
});
