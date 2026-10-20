/**
 * #1143: the Assignment review page reads the source engine from the collector
 * payload (`metadata.source_database.engine`) and hands it to the locking-read
 * check behind the per-query engine picker, so the picker reads `# FOR UPDATE`
 * and `$$ FOR UPDATE $$` as the server does (`query_shape.locking_clause`).
 *
 * Renders the real AssignmentGate page. Cloudscape ships ESM this repo's Jest
 * config cannot transform (see AssignmentGateSummaryFallback.test.js), so its
 * components are plain DOM stand-ins; `Select` exposes the options it is given.
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

const MYSQL_COMMENT = 'SELECT * FROM t WHERE id = 1 # FOR UPDATE';
const POSTGRES_DOLLAR = 'SELECT $$ FOR UPDATE $$ FROM t WHERE id = 1';
const PLAIN_LOCK = 'SELECT * FROM t WHERE id = 1 FOR UPDATE';

function payloads(engine, queryText) {
  const metadata = engine === undefined ? {} : { source_database: { engine } };
  return {
    job: { database_name: 'db' },
    assignments: {
      success: true,
      assignment: {
        query_assignments: [
          { query_id: 'q1', assigned_engine: 'aurora_postgresql', in_scope: true },
        ],
      },
    },
    'reality-check': { success: true },
    phases: { success: true },
    triage: { success: true },
    collector: {
      success: true,
      metadata,
      queries: {
        query_patterns: [{ query_id: 'q1', query_type: 'SELECT', query_text: queryText }],
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

/** Renders the page on the advanced view and returns the row picker's option values. */
async function pickerOptions(engine, queryText) {
  const data = payloads(engine, queryText);
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
  const picker = container.querySelector('.engine-picker');
  expect(picker).not.toBeNull();
  return picker.dataset.options.split(',');
}

test('a MySQL `#` comment hides the lock, so OpenSearch stays offered', async () => {
  expect(await pickerOptions('mysql', MYSQL_COMMENT)).toContain('opensearch');
});

test('the same text from a PostgreSQL source is a lock, so OpenSearch is dropped', async () => {
  expect(await pickerOptions('postgresql', MYSQL_COMMENT)).not.toContain('opensearch');
});

test('a PostgreSQL `$$` string hides the lock, so OpenSearch stays offered', async () => {
  expect(await pickerOptions('postgresql', POSTGRES_DOLLAR)).toContain('opensearch');
});

test('the same `$$` text from a MySQL source is a lock', async () => {
  expect(await pickerOptions('mysql', POSTGRES_DOLLAR)).not.toContain('opensearch');
});

test('a plain locking read drops OpenSearch from a MySQL source', async () => {
  expect(await pickerOptions('mysql', PLAIN_LOCK)).not.toContain('opensearch');
});

test('a plain locking read drops OpenSearch when the source engine is unknown', async () => {
  expect(await pickerOptions(undefined, PLAIN_LOCK)).not.toContain('opensearch');
});

test('a plain read keeps OpenSearch offered', async () => {
  expect(await pickerOptions('mysql', 'SELECT * FROM t WHERE id = 1')).toContain('opensearch');
});
