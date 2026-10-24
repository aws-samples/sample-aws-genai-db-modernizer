/**
 * #1373 (maintainer decision on #1381, 2026-10-22): the Assignment review page shows
 * "pinned (utility)" for a query the first assignment pinned as a utility or DDL
 * statement (`pinned: "utility"` on the served assignment) instead of its
 * confidence percentage, and every other query its percentage. On main a pinned
 * catalog read with a real score of 0 showed "0%".
 *
 * Renders the real AssignmentGate page with the same plain-DOM stand-ins as
 * AssignmentGateLockingDialect.test.js (Cloudscape ships ESM Jest cannot transform);
 * `t` returns its key, so the label shows as its i18n key.
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
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options }) => (
    <div className="engine-picker" data-options={options.map((o) => o.value).join(',')} />
  ),
}));
jest.mock('../../components/SummaryFallbackNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
// One stable `t`: the page refetches when it changes identity.
jest.mock('react-i18next', () => {
  const mockResult = { t: (key) => key };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

function payloads() {
  return {
    job: { database_name: 'discourse' },
    assignments: {
      success: true,
      assignment: {
        version: 2,
        query_assignments: [
          {
            query_id: 'pgcatalog0000aaaa',
            assigned_engine: 'aurora_postgresql',
            confidence: 0,
            pinned: 'utility',
            assignment_reason: 'utility/metadata statement, kept on the source-compatible relational engine (Aurora PostgreSQL)',
            source_tables: ['pg_index', 'pg_attribute'],
            in_scope: true,
          },
          {
            query_id: 'scored000000bbbb',
            assigned_engine: 'dynamodb',
            confidence: 78,
            assignment_reason: 'highest fit for DynamoDB',
            source_tables: ['discourse.users'],
            in_scope: true,
          },
          {
            query_id: 'zeroscore000cccc',
            assigned_engine: 'aurora_postgresql',
            confidence: 0,
            assignment_reason: 'no engine scored this query',
            source_tables: ['discourse.t'],
            in_scope: true,
          },
        ],
      },
    },
    'reality-check': { success: true },
    phases: { success: true },
    triage: { success: true },
    collector: {
      success: true,
      metadata: { source_database: { engine: 'postgresql' } },
      queries: {
        query_patterns: [
          { query_id: 'pgcatalog0000aaaa', query_type: 'SELECT', query_text: 'SELECT a.attname FROM pg_index' },
          { query_id: 'scored000000bbbb', query_type: 'SELECT', query_text: 'SELECT 1' },
          { query_id: 'zeroscore000cccc', query_type: 'SELECT', query_text: 'SELECT 2' },
        ],
      },
    },
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

/** Renders the detailed view and returns each row's id prefix and fit cell text. */
async function rows() {
  const data = payloads();
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => {
    root.render(<AssignmentGate />);
  });
  const toDetail = [...container.querySelectorAll('button')].find((b) =>
    b.textContent.includes('assignment-gate.actions.review-in-detail'),
  );
  await act(async () => {
    toDetail.click();
  });
  return Object.fromEntries(
    [...container.querySelectorAll('.query-list-row__summary')].map((row) => [
      row.querySelector('.query-list-id').textContent.slice(0, 8),
      row.querySelector('.query-list-col--confidence').textContent.trim(),
    ]),
  );
}

test('a utility pin shows the pinned label, and a scored query its percentage', async () => {
  const byId = await rows();
  expect(byId['pgcatalo']).toBe('common.fit.pinned-utility');
  // A query placed by its score keeps its percentage, a real 0 included.
  expect(byId['scored00']).toBe('78%');
  expect(byId['zeroscor']).toBe('0%');
});
