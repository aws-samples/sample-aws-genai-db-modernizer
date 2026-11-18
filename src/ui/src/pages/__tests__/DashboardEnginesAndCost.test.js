// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2345: each row of the assessments list names its source engine, shows one engine
 * badge per target engine and the projected monthly cost the API read from the job's
 * report facts. A job with no report shows dashes, never "$0.00". Under 600 px the list
 * keeps the columns that fit a phone. The real Cloudscape table renders; only the page
 * shell is stubbed (Cloudscape ships ESM this repo's Jest config cannot transform for
 * the shell's layout machinery).
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
function mockStub(tag) {
  return { __esModule: true, default: ({ children }) => <div data-c={tag}>{children}</div> };
}
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: ({ content }) => <main>{content}</main> }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => mockStub('crumbs'));
jest.mock('@cloudscape-design/components/side-navigation', () => mockStub('nav'));
jest.mock('@cloudscape-design/components/modal', () => mockStub('modal'));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };

let phone = false;
const listeners = new Set();
function setPhone(isPhone) {
  phone = isPhone;
  window.matchMedia = (query) => ({
    get matches() { return phone && /max-width:\s*599/.test(query); },
    media: query,
    addListener() {},
    removeListener() {},
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
  });
}

const STATS = {
  success: true, total_assessments: 2, active_jobs: 0, success_rate_percent: 100,
  average_duration_hours: 1, completed_today: 1, last_analysis_at: null,
};
const WITH_REPORT = {
  job_id: 'job-a', database_name: 'wordpress', source_database_type: 'mysql', source_database_label: 'MySQL',
  status: 'SUCCEEDED', created_at: '2026-11-01T10:00:00Z', duration_seconds: 60,
  target_engines: ['aurora_mysql', 'dynamodb'], projected_monthly_cost_usd: 1234.5,
};
const OLD_JOB = {
  job_id: 'job-b', database_name: 'discourse', source_database_type: null,
  status: 'SUCCEEDED', created_at: '2026-10-01T10:00:00Z', duration_seconds: 60,
  target_engines: [], projected_monthly_cost_usd: null,
};
const ZERO_COST = { ...OLD_JOB, job_id: 'job-c', database_name: 'free', projected_monthly_cost_usd: 0 };

let container;
let root;

async function renderDashboard(assessments) {
  mockExecute.mockResolvedValue({
    'get-assessments': { success: true, assessments },
    'get-dashboard-stats': STATS,
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<Dashboard />); });
}

const rowOf = (name) => [...container.querySelectorAll('tbody tr')].find((r) => r.textContent.includes(name));
const headers = () => [...container.querySelectorAll('thead th')].map((h) => h.textContent.trim());

beforeEach(() => {
  setPhone(false);
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  mockExecute.mockReset();
  jest.restoreAllMocks();
});

describe('desktop list', () => {
  test('has the engine and cost columns', async () => {
    await renderDashboard([WITH_REPORT]);
    expect(headers()).toEqual(expect.arrayContaining(['Source', 'Target engines', 'Projected cost']));
  });

  test('a row shows the source engine, one catalog badge per target and the cost', async () => {
    await renderDashboard([WITH_REPORT]);
    const row = rowOf('wordpress');
    const sourceCell = row.querySelectorAll('td')[headers().indexOf('Source')];
    expect(sourceCell.textContent).toBe('MySQL');
    expect(row.textContent).not.toContain('mysql');
    const badges = [...row.querySelectorAll('[data-engine-badge]')].map((b) => [b.getAttribute('data-engine-badge'), b.textContent]);
    expect(badges).toEqual([['aurora_mysql', 'Aurora MySQL'], ['dynamodb', 'DynamoDB']]);
    expect(row.textContent).toContain('$1,234.50/mo');
  });

  test('a job with no report facts shows dashes, never $0 or a badge', async () => {
    await renderDashboard([OLD_JOB]);
    const row = rowOf('discourse');
    expect(row.querySelectorAll('[data-engine-badge]')).toHaveLength(0);
    expect(row.textContent).not.toContain('$');
    expect(row.textContent.match(/—/g).length).toBeGreaterThanOrEqual(3);
  });

  test('a stated cost of zero is shown as stated', async () => {
    await renderDashboard([ZERO_COST]);
    expect(rowOf('free').textContent).toContain('$0.00/mo');
  });
});

describe('phone list', () => {
  test('keeps the database, engines, cost and status, and drops the id, dates and source', async () => {
    setPhone(true);
    await renderDashboard([WITH_REPORT]);
    expect(headers()).toEqual(expect.arrayContaining(['Database', 'Target engines', 'Projected cost', 'Status']));
    for (const dropped of ['Job ID', 'Created', 'Duration', 'Source']) {
      expect(headers()).not.toContain(dropped);
    }
  });

  test('follows the window across 600 px without a reload', async () => {
    await renderDashboard([WITH_REPORT]);
    expect(headers()).toContain('Job ID');
    phone = true;
    await act(async () => { listeners.forEach((fn) => fn()); });
    expect(headers()).not.toContain('Job ID');
    expect(headers()).toContain('Target engines');
    phone = false;
    await act(async () => { listeners.forEach((fn) => fn()); });
    expect(headers()).toContain('Job ID');
  });
});
