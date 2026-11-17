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
jest.mock('../../config/GlobalConfigurations', () => ({
  SideNavigationConfigurations: { header: {}, items: [] },
  ApiConfigurations: { baseUrl: 'http://api.invalid/api/v1/' },
  createLabelFunction: () => () => '',
  EmptyState: ({ title, subtitle, action }) => <div data-empty>{title}{subtitle}{action}</div>,
}));
// The table is its own component with its own tests; here it shows what the page hands it.
jest.mock('../../components/CustomTable', () => ({
  __esModule: true,
  default: ({ dataset, emptyState }) => <div data-table>{dataset.length === 0 ? emptyState : null}</div>,
}));
function mockStub(tag) {
  return { __esModule: true, default: ({ children }) => <div data-c={tag}>{children}</div> };
}
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: ({ content }) => <main>{content}</main> }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => mockStub('crumbs'));
jest.mock('@cloudscape-design/components/side-navigation', () => mockStub('nav'));
jest.mock('@cloudscape-design/components/space-between', () => mockStub('space'));
jest.mock('@cloudscape-design/components/copy-to-clipboard', () => mockStub('copy'));
jest.mock('@cloudscape-design/components/button-dropdown', () => mockStub('dropdown'));
jest.mock('@cloudscape-design/components/flashbar', () => mockStub('flashbar'));
jest.mock('@cloudscape-design/components/modal', () => mockStub('modal'));
jest.mock('@cloudscape-design/components/box', () => mockStub('box'));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: ({ header, children }) => <section>{header}{children}</section> }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: ({ children, actions }) => <h2>{children}{actions}</h2> }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: ({ type, children }) => <span data-status={type}>{children}</span> }));
jest.mock('@cloudscape-design/components/link', () => ({ __esModule: true, default: ({ href, children }) => <a data-link href={href}>{children}</a> }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, href, onClick }) => (href ? <a href={href}>{children}</a> : <button type="button" onClick={onClick}>{children}</button>),
}));
jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ type, header, action, children }) => <div role="alert" data-type={type}><b>{header}</b>{children}{action}</div>,
}));
jest.mock('@cloudscape-design/components/key-value-pairs', () => ({
  __esModule: true,
  default: ({ items }) => (
    <dl>{items.map((i) => <div key={i.label} data-stat={i.label}><dt>{i.label}</dt><dd>{i.value}</dd></div>)}</dl>
  ),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const DOWN = { error: { message: 'Network error' }, status: 'Unknown' };
const STATS = {
  success: true, total_assessments: 6, active_jobs: 2, success_rate_percent: 83.3,
  average_duration_hours: 1.5, completed_today: 4, last_analysis_at: null,
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

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  mockExecute.mockReset();
  jest.restoreAllMocks();
});

beforeEach(() => {
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

const stat = (label) => container.querySelector(`[data-stat="${label}"] dd`).textContent;
const STAT_LABELS = ['Total analyses', 'Active jobs', 'Success rate', 'Average time', 'Last analysis', 'Completed today'];

describe('API down', () => {
  const down = { 'get-assessments': DOWN, 'get-dashboard-stats': DOWN };

  test('one error alert with a retry button, and no request URL', async () => {
    await renderDashboard(down);
    const alerts = container.querySelectorAll('[role="alert"]');
    expect(alerts).toHaveLength(1);
    expect(alerts[0].dataset.type).toBe('error');
    expect(alerts[0].textContent).toContain('Check that the API is running');
    expect(alerts[0].textContent).not.toMatch(/http|api\.invalid|\/api\/v1/);
    expect(container.querySelector('[data-c="flashbar"]')).toBeNull();
    expect(alerts[0].querySelector('button').textContent).toBe('Retry');
  });

  test('no figure is a zero: every stat is a dash', async () => {
    await renderDashboard(down);
    for (const label of STAT_LABELS) expect(stat(label)).toBe('—');
    expect(container.textContent).not.toMatch(/0\.0%|0\.0h|\b0\b/);
  });

  test('the table says the list is unavailable, not that it is empty', async () => {
    await renderDashboard(down);
    expect(container.querySelector('[data-table]').textContent).toBe('Assessments unavailable');
    expect(container.textContent).not.toContain('No assessments yet');
    expect(container.textContent).not.toContain('No records');
  });

  test('retry loads again and clears the alert on success', async () => {
    await renderDashboard(down);
    expect(mockExecute).toHaveBeenCalledTimes(1);
    mockExecute.mockResolvedValue({ 'get-assessments': { success: true, assessments: [] }, 'get-dashboard-stats': STATS });
    await act(async () => { container.querySelector('[role="alert"] button').click(); });
    expect(mockExecute).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(stat('Total analyses')).toBe('6');
  });

  test('an unexpected exception also shows the alert, not zeros', async () => {
    mockExecute.mockRejectedValue(new Error('boom'));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    jest.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { root.render(<Dashboard />); });
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(stat('Total analyses')).toBe('—');
  });
});

describe('while the first load is pending', () => {
  test('stats show a loading indicator, not 0', async () => {
    mockExecute.mockReturnValue(new Promise(() => {}));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => { root.render(<Dashboard />); });
    for (const label of STAT_LABELS) {
      expect(container.querySelector(`[data-stat="${label}"] [data-status="loading"]`)).not.toBeNull();
    }
    expect(container.textContent).not.toMatch(/0\.0%|0\.0h/);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('API up', () => {
  const up = { 'get-assessments': { success: true, assessments: [] }, 'get-dashboard-stats': STATS };

  test('figures come from the stats, formatted by the shared formatter, in one container', async () => {
    await renderDashboard(up);
    expect(stat('Total analyses')).toBe('6');
    expect(stat('Active jobs')).toBe('2');
    expect(stat('Success rate')).toBe('83.3%');
    expect(stat('Average time')).toBe('1.5h');
    expect(stat('Completed today')).toBe('4');
    expect(stat('Last analysis')).toBe('—');
    // "Active jobs" appears once, not in a second container (#2306).
    expect(container.querySelectorAll('[data-stat="Active jobs"]')).toHaveLength(1);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  test('a real zero from the API still prints as 0', async () => {
    await renderDashboard({ ...up, 'get-dashboard-stats': { ...STATS, total_assessments: 0, active_jobs: 0, success_rate_percent: 0, completed_today: 0 } });
    expect(stat('Total analyses')).toBe('0');
    expect(stat('Success rate')).toBe('0.0%');
  });

  test('no stat is a link: none has a target to go to', async () => {
    await renderDashboard(up);
    expect(container.querySelectorAll('[data-stat] [data-link], [data-stat] a')).toHaveLength(0);
  });

  test('the empty list says "No assessments yet" and leads to a new assessment', async () => {
    await renderDashboard(up);
    const table = container.querySelector('[data-table]');
    expect(table.textContent).toContain('No assessments yet');
    expect(table.textContent).not.toContain('No records');
    expect(table.querySelector('a').getAttribute('href')).toBe('/analysis/create');
  });
});
