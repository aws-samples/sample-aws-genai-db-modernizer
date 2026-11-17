// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2312: the dashboard table's accessible names. Each row's selection radio says what it
 * selects, and a column's sort label uses the header the reader sees (not the field id,
 * which read "jobId, not sorted."). The table is the real Cloudscape Table.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import Dashboard from '../Dashboard';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = jest.requireActual('../../locales/en.json')[key];
      return text === undefined ? key : text.replace(/{{(\w+)}}/g, (_, k) => (opts && opts[k]) || '');
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

const STATS = {
  success: true, total_assessments: 6, active_jobs: 2, success_rate_percent: 83.3,
  average_duration_hours: 1.5, completed_today: 4, last_analysis_at: null,
};
const JOB = {
  job_id: 'job-1', database_name: 'wordpress', source_database_type: 'mysql',
  status: 'completed', created_at: '2026-11-01T10:00:00Z', duration_seconds: 60,
};

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


const rowsResult = () => ({ 'get-assessments': { success: true, assessments: [JOB] }, 'get-dashboard-stats': STATS });

test('each selection radio names its assessment', async () => {
  await renderDashboard(rowsResult());
  const radios = [...container.querySelectorAll('input[type="radio"]')];
  expect(radios).toHaveLength(1);
  const name = radios[0].getAttribute('aria-label') || '';
  expect(name).toMatch(/^Select /);
  expect(name).toContain('wordpress');
});

test('the table is named, so its region landmark is unique', async () => {
  await renderDashboard(rowsResult());
  expect(container.querySelector('[aria-label="Recent analyses"]')).not.toBeNull();
});

test('every column header is sortable by its displayed name, never its field id', async () => {
  await renderDashboard(rowsResult());
  const all = [...container.querySelectorAll('[aria-label]')].map((el) => el.getAttribute('aria-label'));
  expect(all).toContain('Job ID, not sorted.');
  expect(all).toContain('Database, not sorted.');
  expect(all.filter((l) => /^(jobId|databaseName|created|duration|database|status)\b/.test(l) && l.includes('sorted'))).toEqual([]);
});
