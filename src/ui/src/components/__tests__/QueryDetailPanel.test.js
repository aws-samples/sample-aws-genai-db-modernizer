// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2319: what the query detail split panel shows, on the real Cloudscape components.
 * The SplitPanel needs the page's app layout, so this renders its body and builds
 * its title; the panel in the browser is covered by tests/e2e/test_ui.py.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import { QueryDetailBody, queryDetailTitle, humanizeKey } from '../QueryDetailPanel';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const t = i18n.t.bind(i18n);
const ID = '0452bed2' + 'z'.repeat(56);
const journey = (extra = {}) => ({
  query_id: ID,
  source: {
    query_text: "UPDATE shop.wp_woocommerce_api_keys SET last_access = NOW()\nWHERE key_id = ?",
    query_type: 'UPDATE',
    tables_accessed: ['shop.wp_woocommerce_api_keys'],
    frequency_per_hour: 29795.38123,
    calls_per_second: 8.2765,
    performance: { avg_latency_ms: 1.23456, rows_examined: 12 },
    characteristics: { has_where: true, columns_read: ['a', 'b'] },
  },
  assignment: { assigned_engine: 'dynamodb', confidence: 82, in_scope: true },
  ...extra,
});
const noTrace = { data: null, error: null };

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
const render = (props) => act(() => {
  root.render(<QueryDetailBody queryId={ID} journey={journey()} loading={false} trace={noTrace} t={t} {...props} />);
});
// The open tab's text (the collapsed developer section holds the raw JSON in the DOM).
const text = () => createWrapper(container).findTabs().findTabContent().getElement().textContent;
const all = () => container.textContent;

test('the title is the short id, the operation and the first table, without the schema', () => {
  expect(queryDetailTitle(t, ID, journey())).toBe('Query 0452bed2 · UPDATE wp_woocommerce_api_keys');
  expect(queryDetailTitle(t, ID, null)).toBe('Query 0452bed2');
  expect(queryDetailTitle(t, ID, { source: { query_type: 'SET' } })).toBe('Query 0452bed2 · SET');
});

test('the general tab shows rounded figures, the engine by its name and sentence case labels', () => {
  render();
  expect(text()).toContain('29,795.38');
  expect(text()).not.toContain('29795.38123');
  expect(text()).toContain('8.28');
  expect(text()).not.toContain('8.2765');
  expect(text()).toContain('Calls per hour');
  expect(text()).toContain('Calls per second');
  expect(text()).not.toContain('Calls per Second');
  expect(text()).not.toContain('Assigned Engine');
  expect(text()).not.toContain('dynamodb');
  expect(text()).toContain('DynamoDB');
  expect(text()).toContain('Workload fit');
  expect(text()).toContain('82%');
  expect(text()).toContain('In scope');
  expect(text()).toContain('Yes');
});

test('the full statement and the full id are in the panel', () => {
  render();
  expect(text()).toContain(ID);
  expect(text()).toContain('WHERE key_id = ?');
  expect(text()).toContain('Statement');
  expect(text()).not.toContain('Query Text');
});

test('the performance tab labels its fields in sentence case and rounds its numbers', () => {
  render();
  act(() => createWrapper(container).findTabs().findTabLinkById('performance').click());
  expect(text()).toContain('Avg latency ms');
  expect(text()).toContain('1.23');
  expect(text()).not.toContain('1.23456');
  expect(text()).not.toContain('Avg Latency Ms');
});

test('the characteristics tab shows yes and no and lists', () => {
  render();
  act(() => createWrapper(container).findTabs().findTabLinkById('characteristics').click());
  expect(text()).toContain('Has where');
  expect(text()).toContain('a, b');
});

test('a query with no performance data says so instead of an empty tab', () => {
  render({ journey: journey({ source: { query_text: 'SELECT 1', query_type: 'SELECT', tables_accessed: [] } }) });
  act(() => createWrapper(container).findTabs().findTabLinkById('performance').click());
  expect(text()).toContain('No performance data was collected for this query.');
  act(() => createWrapper(container).findTabs().findTabLinkById('characteristics').click());
  expect(text()).toContain('No characteristics were collected for this query.');
});

test('the raw JSON is behind a collapsed Developer details section, and no JSON tab exists', () => {
  render();
  const tabs = createWrapper(container).findTabs().findTabLinks().map(l => l.getElement().textContent);
  expect(tabs).toEqual(['General', 'Performance', 'Characteristics', 'Why this engine']);
  const section = createWrapper(container).findExpandableSection();
  expect(section.getElement().textContent).toContain('Developer details');
  expect(section.findExpandedContent()).toBeNull();
  act(() => section.findHeader().click());
  expect(section.findExpandedContent().getElement().textContent).toContain('"query_type": "UPDATE"');
});

test('markup in a statement or a table name is shown as text', () => {
  render({ journey: journey({ source: { query_text: "SELECT '<script>x()</script>'", query_type: 'SELECT', tables_accessed: ['<b>t</b>'] } }) });
  expect(container.querySelector('script')).toBeNull();
  expect(container.querySelector('b')).toBeNull();
  expect(text()).toContain('<script>x()</script>');
});

test('loading and a failed read show their own state, not a blank panel', () => {
  render({ loading: true, journey: null });
  expect(all()).toContain('Loading query history...');
  render({ loading: false, journey: { error: 'server' } });
  expect(all()).toContain('Query history could not be loaded.');
  expect(all()).toContain('The service returned an error.');
});

test('humanizeKey writes a snake case key as one sentence', () => {
  expect(humanizeKey('avg_latency_ms')).toBe('Avg latency ms');
  expect(humanizeKey('rows')).toBe('Rows');
});
