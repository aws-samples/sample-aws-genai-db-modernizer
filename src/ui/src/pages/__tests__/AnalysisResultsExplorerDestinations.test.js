// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2320: the explorer's "By source table" view lists each destination once per source
 * table, shows the first three and puts the rest behind "+N more", and wraps its lines.
 * #2446: the view is the Data model tab's Source tables section now, with no mode to
 * pick, and the cache layer's key designs join their source table's row.
 * The page's own grouping feeds the cell; the cell itself is covered in
 * components/__tests__/DestinationTables.test.js.
 *
 * Renders the real page with plain-DOM stand-ins for most Cloudscape components; Popover
 * and the cell's Box are real. The page's strings are keys, so the checks read keys.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AnalysisResults from '../AnalysisResults';
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
jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ type, header, children }) => <div data-alert={type}><b>{header}</b>{children}</div>,
}));
jest.mock('@cloudscape-design/components/anchor-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/badge', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/button', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/code-editor', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/copy-to-clipboard', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, headerCounter, children }) => (
    <section><h3>{headerText}</h3><span data-counter>{headerCounter}</span>{children}</section>
  ),
}));
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, actions }) => <div>{children}{actions}</div>,
}));
jest.mock('@cloudscape-design/components/help-panel', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/icon', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/input', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/key-value-pairs', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/link', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/modal', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/multiselect', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pagination', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pie-chart', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/property-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/segmented-control', () => ({
  __esModule: true,
  default: ({ options, onChange }) => (
    <div>
      {options.map((o) => (
        <button key={o.id} data-mode={o.id} onClick={() => onChange({ detail: { selectedId: o.id } })} />
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
// The source-table view's Table: each row's Destination tables cell, and whether it wraps.
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ columnDefinitions, items, wrapLines }) => {
    const col = columnDefinitions.find((c) => c.id === 'dest_tables');
    if (!col || !items) return null;
    return (
      <div data-source-table data-wrap={String(!!wrapLines)}>
        {items.map((item) => (
          <div key={item.table} data-row={item.table}>
            {col.cell(item)}
            <span data-converge>{columnDefinitions.find((c) => c.id === 'merged_with').cell(item)}</span>
          </div>
        ))}
      </div>
    );
  },
}));
// Every tab: its label and its content.
jest.mock('@cloudscape-design/components/tabs', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div>{tabs.map((tab) => <div key={tab.id}><span data-tab-label>{tab.label}</span>{tab.content}</div>)}</div>
  ),
}));
jest.mock('@cloudscape-design/components/text-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/toggle-button', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/top-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/global-styles', () => ({}));
jest.mock('../../components/ChartSankey', () => ({
  __esModule: true,
  default: ({ data }) => (
    <div>
      {data.links.map((l) => (
        <span key={l.target} className="sankey-link" data-engine={l.target} data-value={l.value} />
      ))}
    </div>
  ),
}));
jest.mock('ace-builds', () => ({}));
jest.mock('ace-builds/webpack-resolver', () => ({}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useLocation: () => ({ search: '', hash: '' }),
  useNavigate: () => jest.fn(),
}));
jest.mock('react-i18next', () => {
  const mockResult = { t: (key, opts) => (opts && opts.count !== undefined ? `${key}|${opts.count}` : key) };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

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

const pat = (id, engine, tableName, sources) => ({
  pattern_id: id,
  engine,
  operation: 'GetItem',
  table_name: tableName,
  source_tables: sources,
});
const designOf = (engine, patterns) => ({ target_type: engine, content: { access_patterns: patterns } });

async function openSourceView(designs, cacheOverlay, synthesis) {
  ApiManager.mockImplementation(() => ({
    execute: async (calls) => Object.fromEntries(calls.map(({ id }) => [id, {
      results: {
        success: true,
        synthesis: synthesis || { database_name: 'wp', query_groups: [], ...(cacheOverlay ? { cache_overlay: cacheOverlay } : {}) },
      },
      schemas: { success: true, schema_designs: designs },
    }[id] || { success: false }])),
  }));
  await act(async () => {
    root.render(<AnalysisResults />);
  });
}

const rows = () => [...container.querySelectorAll('[data-row]')];
const rowNames = (table) =>
  [...container.querySelector(`[data-row="${table}"]`).querySelectorAll('[data-testid="dest-name"]')].map((n) => n.textContent);

test('a source table lists each destination once per engine, in first-seen order', async () => {
  await openSourceView([
    designOf('dynamodb', [
      pat('A', 'dynamodb', 'topics', ['public.topics']),
      pat('B', 'dynamodb', 'posts', ['public.topics']),
      pat('C', 'dynamodb', 'topics', ['public.topics']),
    ]),
    designOf('documentdb', [pat('D', 'documentdb', 'topics', ['public.topics'])]),
  ]);
  // DynamoDB topics and DocumentDB topics are two destinations; each is labelled.
  expect(rowNames('topics')).toEqual(['topics (DynamoDB)', 'posts', 'topics (DocumentDB)']);
});

test('an Aurora pattern over several source tables lists each table, not one joined name', async () => {
  const aur = { pattern_id: 'E', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.topics', 'public.posts'] };
  const aur2 = { pattern_id: 'F', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.topics'] };
  await openSourceView([designOf('aurora_mysql', [aur, aur2])]);
  expect(rowNames('topics')).toEqual(['topics', 'posts']);
});

test('more than three destinations show three and a +N more', async () => {
  const pats = ['a', 'b', 'c', 'd', 'e'].map((n, i) => pat(`P${i}`, 'dynamodb', n, ['public.src']));
  await openSourceView([designOf('dynamodb', pats)]);
  expect(rowNames('src')).toEqual(['a', 'b', 'c']);
  expect(container.querySelector('[data-row="src"]').textContent).toContain('analysis-results.explorer.destinations-more|2');
});

test('the source-table rows wrap', async () => {
  await openSourceView([designOf('dynamodb', [pat('A', 'dynamodb', 'topics', ['public.topics'])])]);
  expect(rows().length).toBe(1);
  expect(container.querySelector('[data-source-table]').getAttribute('data-wrap')).toBe('true');
});

// The cache layer's key designs join their source table's row (#2446) and read the same cell.
const cachePattern = (id, keyPattern, sources) => ({
  pattern_id: id,
  engine: 'elasticache',
  operation: 'GET',
  key_pattern: keyPattern,
  source_tables: sources,
});

test('the cache layer rows list their key patterns', async () => {
  await openSourceView(
    [
      designOf('dynamodb', [pat('A', 'dynamodb', 'topics', ['public.topics'])]),
      designOf('elasticache', [
        cachePattern('C1', 'topics:{id}', ['public.topics']),
        cachePattern('C2', 'topics:by_user:{uid}', ['public.topics']),
        cachePattern('C3', 'topics:{id}', ['public.topics']),
      ]),
    ],
    { engine: 'elasticache', query_count: 3, owners: { dynamodb: 3 }, patterns: {} },
  );
  const sections = [...container.querySelectorAll('[data-source-table]')];
  expect(sections.length).toBe(1);
  expect(rowNames('topics')).toEqual(['topics', 'topics:{id}', 'topics:by_user:{uid}']);
});

const fs = require('fs');
const path = require('path');

test('the saved wordpress run: no cache row loses its keys, and rows with a destination show one', async () => {
  const root = path.resolve(__dirname, '../../../../../tests/fixtures/real_runs/wordpress-both');
  const report = JSON.parse(fs.readFileSync(`${root}/report.json`, 'utf8'));
  // The three designed engines of the saved run, each at its latest version.
  const designs = ['aurora_mysql', 'dynamodb', 'elasticache'].map((engine) => {
    const out = JSON.parse(fs.readFileSync(`${root}/job/schema-${engine}/v2/schema_output.json`, 'utf8'));
    return { target_type: out.target_engine, content: out };
  });
  await openSourceView(designs, report.cache_overlay, report);
  const sections = [...container.querySelectorAll('[data-source-table]')];
  expect(sections.length).toBe(1);
  const cached = new Set(designs.find((d) => d.target_type === 'elasticache').content.access_patterns
    .flatMap((ap) => ap.source_tables || []).map((t) => t.split('.').pop()));
  expect(cached.size).toBeGreaterThan(0);
  cached.forEach((table) => {
    const row = container.querySelector(`[data-row="${table}"]`);
    expect(row).not.toBeNull();
    expect(row.querySelectorAll('[data-testid="dest-name"]').length).toBeGreaterThan(0);
  });
});

const converge = (table) => container.querySelector(`[data-row="${table}"] [data-converge]`).textContent;

// Guards behaviour main already has: two source tables on one named destination converge.
test('two source tables on one destination of one engine converge', async () => {
  await openSourceView([
    designOf('dynamodb', [pat('A', 'dynamodb', 'orders', ['public.a']), pat('B', 'dynamodb', 'orders', ['public.b'])]),
  ]);
  expect(converge('a')).toBe('b');
  expect(converge('b')).toBe('a');
});

test('the same name on two engines is not a convergence', async () => {
  await openSourceView([
    designOf('dynamodb', [pat('A', 'dynamodb', 'orders', ['public.a'])]),
    designOf('documentdb', [pat('B', 'documentdb', 'orders', ['public.b'])]),
  ]);
  expect(converge('a')).toBe('—');
  expect(converge('b')).toBe('—');
});

test('Aurora patterns name no destination table, so a join never makes tables converge', async () => {
  const join = { pattern_id: 'E', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.a', 'public.b'] };
  const solo = { pattern_id: 'F', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.c'] };
  const link = { pattern_id: 'G', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.b', 'public.c'] };
  await openSourceView([designOf('aurora_mysql', [join, solo, link])]);
  ['a', 'b', 'c'].forEach((t) => expect(converge(t)).toBe('—'));
});

test('a named destination still converges when an Aurora join reads the same tables', async () => {
  const join = { pattern_id: 'E', engine: 'aurora_mysql', operation: 'SELECT', source_tables: ['public.a', 'public.b'] };
  await openSourceView([
    designOf('aurora_mysql', [join]),
    designOf('dynamodb', [pat('A', 'dynamodb', 'orders', ['public.a']), pat('B', 'dynamodb', 'orders', ['public.b'])]),
  ]);
  expect(converge('a')).toBe('b');
});
