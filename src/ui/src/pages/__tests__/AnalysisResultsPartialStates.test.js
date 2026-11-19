// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2308: data the page could not read, or read only in part, is not a true "found
 * nothing". Query routing, the explorer and the Table design tab each say what is
 * missing, and the design tab's label and body take their count from one source.
 *
 * Renders the real page with plain-DOM stand-ins for the Cloudscape components (it ships
 * ESM Jest cannot transform); the alert stand-in shows its type and header. The page's
 * strings are keys, so the checks read keys.
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
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/button', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/code-editor', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/copy-to-clipboard', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, headerCounter, children }) => (
    <section><h3>{headerText}</h3><span data-counter>{headerCounter}</span>{children}</section>
  ),
}));
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
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
jest.mock('@cloudscape-design/components/segmented-control', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/table', () => ({ __esModule: true, default: () => null }));
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

const ENGINE = 'aurora_mysql';
const queryGroups = [{
  group_name: 'wp_posts',
  engines: [ENGINE],
  source_queries: [{ query_id: 'q1', query_text: 'SELECT 1', query_type: 'SELECT' }],
  access_patterns: [{ engine: ENGINE, query_ids: ['q1'], design_rps: 1 }],
}];
const table = (name) => ({ table_name: name, columns: [], primary_key: [] });
const design = (names) => ({ target_type: ENGINE, content: { table_definitions: names.map(table) } });

const report = ({ facts, groups = queryGroups } = {}) => ({
  success: true,
  synthesis: { database_name: 'wp', query_groups: groups, ...(facts ? { facts } : {}) },
});
const FACTS_20 = {
  engines: [{ engine: ENGINE, target_objects: 20 }],
  access_pattern_totals: { owned_pattern_count: 12, cache_layer_pattern_count: 0, listed_pattern_count: 12 },
};

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

// The saved help-site job: every request but the report and the (empty) designs is a 404.
async function renderWith({ results, schemas, journeys, page2 }) {
  const answers = {
    results,
    schemas: schemas || { success: true, schema_designs: [] },
    collector: { success: false },
    p2: page2,
    p1: journeys || { success: false, error: { message: 'Not Found' } },
  };
  ApiManager.mockImplementation(() => ({
    execute: async (calls) => Object.fromEntries(calls.map(({ id }) => [id, answers[id] || { success: false }])),
  }));
  await act(async () => {
    root.render(<AnalysisResults />);
  });
}

const alerts = () => [...container.querySelectorAll('[data-alert]')].map((n) => n.textContent);
const tabLabels = () => [...container.querySelectorAll('[data-tab-label]')].map((n) => n.textContent);
const counters = () => [...container.querySelectorAll('[data-counter]')].map((n) => n.textContent);
const has = (text) => container.textContent.includes(text);

test('query routing that could not be read is an alert with no count, not "(0)"', async () => {
  await renderWith({ results: report({ facts: FACTS_20 }), journeys: { success: false, status: 404, error: { message: 'Not Found' } } });
  expect(alerts().some((a) => a.includes('analysis-results.query-routing.unavailable-title'))).toBe(true);
  expect(alerts().some((a) => a.includes('Not Found'))).toBe(false);
  expect(alerts().some((a) => a.includes('errors.request.not-found'))).toBe(true);
  expect(counters()).not.toContain('(0)');
  expect(has('analysis-results.query-routing.empty')).toBe(false);
});

test('a server error on the query list reads differently from not found', async () => {
  await renderWith({ results: report({ facts: FACTS_20 }), journeys: { success: false, status: 500 } });
  expect(alerts().some((a) => a.includes('errors.request.server'))).toBe(true);
  expect(alerts().some((a) => a.includes('errors.request.not-found'))).toBe(false);
});

test('an explorer the design files cannot fill is an alert, not an empty table', async () => {
  await renderWith({ results: report({ facts: FACTS_20 }) });
  expect(alerts().some((a) => a.includes('analysis-results.explorer.unavailable-title'))).toBe(true);
  expect(has('analysis-results.explorer.no-matching-source-tables')).toBe(false);
});

test('a design the report counts but the page cannot read is "not available", never "0 tables"', async () => {
  await renderWith({ results: report({ facts: FACTS_20 }) });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label-unavailable');
  expect(alerts().some((a) => a.includes('analysis-results.aurora-design.unavailable-title'))).toBe(true);
  expect(has('analysis-results.aurora-design.no-tables')).toBe(false);
  expect(has('analysis-results.aurora-design.tables-header|0')).toBe(false);
});

test('a failed design request is "not available" even when the report has no count', async () => {
  await renderWith({ results: report({ facts: { engines: [{ engine: ENGINE, target_objects: null }] } }), schemas: { success: false } });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label-unavailable');
  // The report records no count: the body states none, never "counts 0 tables".
  expect(has('analysis-results.aurora-design.unavailable-body-nocount')).toBe(true);
  expect(has('analysis-results.aurora-design.unavailable-body|')).toBe(false);
});

test('a run with no design says there is none yet and why', async () => {
  // synthesis writes null for an engine with no design
  const facts = { engines: [{ engine: ENGINE, target_objects: null }] };
  await renderWith({ results: report({ facts }) });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label-none');
  expect(alerts().some((a) => a.includes('analysis-results.aurora-design.none-title'))).toBe(true);
  expect(has('analysis-results.aurora-design.no-tables')).toBe(false);
});

test('part of a design is listed with the tab and the body on one count', async () => {
  await renderWith({ results: report({ facts: FACTS_20 }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label|20');
  expect(has('analysis-results.aurora-design.tables-header|20')).toBe(true);
  expect(alerts().some((a) => a.includes('analysis-results.aurora-design.partial-title'))).toBe(true);
});

test('a design with more tables than the report counts shows the report figure and a note', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 1 }] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label|1');
  expect(has('analysis-results.aurora-design.tables-header|1')).toBe(true);
  expect(alerts().some((a) => a.includes('analysis-results.aurora-design.longer-title'))).toBe(true);
  expect(has('analysis-results.aurora-design.longer-body|1')).toBe(true);
});

test('a complete design has no longer-list note', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 2 }] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  expect(has('analysis-results.aurora-design.longer')).toBe(false);
});

test('a script error reading the query list reads as a generic error, not unreachable', async () => {
  // page 1's items is not a list: joining page 2 throws after two good responses
  await renderWith({ results: report({ facts: FACTS_20 }), journeys: { success: true, status: 200, items: 5, total_pages: 2 }, page2: { success: true, status: 200, items: [] } });
  expect(alerts().some((a) => a.includes('errors.request.unreachable'))).toBe(false);
  expect(alerts().some((a) => a.includes('errors.request.other'))).toBe(true);
});

test('an explorer with fewer rows than the report counts says it is partial', async () => {
  const withPattern = { target_type: ENGINE, content: { access_patterns: [{ pattern_id: 'P1', engine: ENGINE }], table_definitions: [table('a')] } };
  await renderWith({ results: report({ facts: FACTS_20 }), schemas: { success: true, schema_designs: [withPattern] } });
  expect(alerts().some((a) => a.includes('analysis-results.explorer.partial-title'))).toBe(true);
});

test('a complete design has no state alert', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 2 }] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label|2');
  expect(has('analysis-results.aurora-design.tables-header|2')).toBe(true);
  expect(alerts().filter((a) => a.includes('aurora-design'))).toEqual([]);
});

test('a design that really has no tables says so, with no alert', async () => {
  const empty = { target_type: ENGINE, content: { index_designs: [{ name: 'i' }] } };
  const facts = { engines: [{ engine: ENGINE, target_objects: 0 }] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [empty] } });
  expect(has('analysis-results.aurora-design.no-tables')).toBe(true);
  expect(alerts().filter((a) => a.includes('aurora-design'))).toEqual([]);
});

test('a saved report with no facts still loads and counts what the page loaded', async () => {
  await renderWith({ results: report(), schemas: { success: true, schema_designs: [design(['a', 'b', 'c'])] } });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label|3');
  expect(alerts().filter((a) => a.includes('aurora-design'))).toEqual([]);
});

test('a query list that loads shows its count and no alert', async () => {
  const journeys = { success: true, items: [{ query_id: 'q1', tables: [] }], total_pages: 1 };
  await renderWith({ results: report({ facts: FACTS_20 }), journeys });
  expect(alerts().some((a) => a.includes('query-routing.unavailable'))).toBe(false);
});

// #2484: the objects the design leaves out, from facts.design_left_out.
const LEFT_OUT_ROW = {
  engine: ENGINE,
  object_count: 1,
  objects: [{ name: 'Sales.orders', feature: 'colliding_table_name', label: 'Colliding table name', object: 'table', reason: 'why.' }],
  ddl_names_each: true,
};
const leftOutAlert = () => container.querySelector('#aurora-design-left-out');

test('the left-out alert sits above the table list', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 2 }], design_left_out: [LEFT_OUT_ROW] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  const list = container.textContent;
  const alertAt = list.indexOf('analysis-results.aurora-design.left-out-title|1');
  const tablesAt = list.indexOf('analysis-results.aurora-design.tables-header|2');
  expect(alertAt).toBeGreaterThanOrEqual(0);
  expect(tablesAt).toBeGreaterThan(alertAt);
  expect(has('analysis-results.aurora-design.left-out-body-ddl')).toBe(true);
});

test('the left-out alert leaves out the DDL sentence when the script has no comments', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 2 }], design_left_out: [{ ...LEFT_OUT_ROW, ddl_names_each: false }] };
  await renderWith({ results: report({ facts }), schemas: { success: true, schema_designs: [design(['a', 'b'])] } });
  expect(leftOutAlert()).not.toBeNull();
  expect(has('analysis-results.aurora-design.left-out-body-ddl')).toBe(false);
});

test('no left-out alert when the design is not available', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: 20 }], design_left_out: [LEFT_OUT_ROW] };
  await renderWith({ results: report({ facts }) });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label-unavailable');
  expect(leftOutAlert()).toBeNull();
});

test('no left-out alert when there is no design yet', async () => {
  const facts = { engines: [{ engine: ENGINE, target_objects: null }], design_left_out: [LEFT_OUT_ROW] };
  await renderWith({ results: report({ facts }) });
  expect(tabLabels()).toContain('analysis-results.aurora-design.tab-label-none');
  expect(leftOutAlert()).toBeNull();
});
