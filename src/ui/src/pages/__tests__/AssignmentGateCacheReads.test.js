/**
 * #462: the assignment gate page lists the hottest cache candidates under the Query flow, the
 * same compact list the chat gate prints (`cache_reads` of the assignments response; the UI
 * only formats it). Same plain-DOM stand-ins as AssignmentGatePinnedFit.test.js.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssignmentGate from '../AssignmentGate';
import ApiManager from '../../classes/ApiManager';

function mockPassThrough({ children, content }) {
  const React = require('react');
  return (
    <div>
      {React.isValidElement(content) ? content : null}
      {children}
    </div>
  );
}

jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/badge', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/alert', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/table', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/property-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pagination', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/modal', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/tabs', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/expandable-section', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/checkbox', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick }) => <button onClick={onClick}>{children}</button>,
}));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/SummaryFallbackNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
// One stable `t` that fills in the default wording, so the page text can be read.
jest.mock('react-i18next', () => {
  const t = (key, opts = {}) => {
    const { defaultValue, ...vars } = opts;
    let text = defaultValue !== undefined ? defaultValue : key;
    Object.entries(vars).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
    return text;
  };
  const mockResult = { t };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

function payloads(cacheReads) {
  return {
    job: { database_name: 'wordpress' },
    assignments: {
      success: true,
      cache_reads: cacheReads,
      assignment: {
        version: 1,
        cache_overlay: { engine: 'elasticache', query_count: 20, call_share_percent: 83.4 },
        query_assignments: [
          {
            query_id: 'q1', assigned_engine: 'aurora_mysql', confidence: 80, assignment_reason: 'fit',
            source_tables: ['wp_options'], in_scope: true,
          },
        ],
      },
    },
    'reality-check': {
      success: true,
      before_distribution: { aurora_mysql: 1 },
      after_distribution: { aurora_mysql: 1 },
    },
    phases: { success: true },
    triage: { success: true },
    collector: { success: true, queries: { query_patterns: [{ query_id: 'q1', query_type: 'SELECT', query_text: 'SELECT 1' }] } },
  };
}

let container;
let root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(cacheReads) {
  const data = payloads(cacheReads);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => {
    root.render(<AssignmentGate />);
  });
  return container.querySelector('.cache-candidate-reads');
}

test('lists the hottest cache candidates and how many more, as the chat gate does', async () => {
  const block = await render({
    reads: [
      { query_id: 'q1', tables: ['wp_options'], owner: 'aurora_mysql', pattern: 'point_lookup', calls_per_second: 41.26, call_share_percent: 19.3 },
    ],
    more: 19,
  });
  const lines = [...block.querySelectorAll('li')].map((li) => li.textContent);
  expect(lines).toEqual([
    'wp_options (Aurora MySQL, point lookup): 41.3 calls/s, 19.3% of calls',
    'and 19 more',
  ]);
});

test('shows no list when the response names no cache candidates', async () => {
  expect(await render(null)).toBeNull();
});
