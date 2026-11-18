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
  expect(tabs).toEqual(['General', 'Target design', 'Performance', 'Characteristics', 'Why this engine']);
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

// #2446: the access pattern explorer's pattern and cache tables folded into the query
// table; the query's own patterns, with their keys and indexes, are in its panel.
const ownerPattern = {
  id: 'ddb-ap-0001-abcdef',
  engine: 'dynamodb',
  operation: 'Query',
  destTable: 'api_keys',
  gsiName: 'by_user',
  destDef: {
    gsis: [{ gsi_name: 'by_user', partition_key: { attribute_name: 'user_id', attribute_type: 'S' }, sort_key: { attribute_name: 'last_access', attribute_type: 'N' }, projection: 'KEYS_ONLY' }],
  },
  keyCondition: 'user_id = :uid',
  sourceTables: ['wp_woocommerce_api_keys'],
  designRps: 12.3456,
  itemSize: 2048,
  avgItems: null,
  patternGroup: 'api-keys',
  description: 'Keys of one user, newest first',
  patternDescription: 'Keys of one user, newest first',
  queryIds: [ID],
  inScope: true,
};
const cachePattern = {
  id: 'ec-ap-0001',
  engine: 'elasticache',
  isCacheLayer: true,
  operation: 'GET',
  destTable: 'apikey:{id}',
  ttlSeconds: 300,
  keyDataType: 'hash',
  sourceTables: ['wp_woocommerce_api_keys'],
  description: '',
  queryIds: [ID],
};
const openDesign = () => act(() => createWrapper(container).findTabs().findTabLinkById('design').click());

test('the target design tab shows each pattern that serves the query, with its keys and indexes', () => {
  render({ patterns: [ownerPattern, cachePattern] });
  openDesign();
  const shown = text();
  expect(shown).toContain('DynamoDB access pattern ddb-ap-0');
  expect(shown).toContain('api_keys');
  expect(shown).toContain('by_user');
  expect(shown).toContain('user_id = :uid');
  expect(shown).toContain('user_id (S)');
  expect(shown).toContain('last_access (N)');
  expect(shown).toContain('KEYS_ONLY');
  expect(shown).toContain('12.35');
  expect(shown).not.toContain('12.3456');
  expect(shown).toContain('2,048 bytes');
  expect(shown).toContain('api-keys');
  expect(shown).toContain('Keys of one user, newest first');
});

test('the cache layer key design shows its key, TTL and data type, named as the cache layer', () => {
  render({ patterns: [ownerPattern, cachePattern] });
  openDesign();
  const shown = text();
  expect(shown).toContain('ElastiCache (cache layer) access pattern ec-ap-00');
  expect(shown).toContain('apikey:{id}');
  expect(shown).toContain('TTL 300s');
  expect(shown).toContain('hash');
  expect(shown.indexOf('DynamoDB access pattern')).toBeLessThan(shown.indexOf('ElastiCache (cache layer)'));
});

test('a query no schema design serves says so instead of an empty tab', () => {
  render({ patterns: [] });
  openDesign();
  expect(text()).toContain('No schema design lists an access pattern for this query.');
  render({ patterns: undefined });
  openDesign();
  expect(text()).toContain('No schema design lists an access pattern for this query.');
  render({ patterns: [], schemaDesigns: { dynamodb: { status: 'completed' } }, availability: 'available' });
  openDesign();
  expect(text()).toContain('No schema design lists an access pattern for this query.');
});

// #2480 review: the empty tab says which case applies.
test('a skipped schema design says so, with the reason the report carries, as text', () => {
  render({ patterns: [], schemaDesigns: { dynamodb: { status: 'skipped', reason: 'llm_mode=none: <b>every</b> schema designer needs a model' } }, availability: 'available' });
  openDesign();
  const shown = text();
  expect(shown).toContain('Schema design was skipped for DynamoDB, so no access pattern is listed for this query.');
  expect(shown).toContain('Reason: llm_mode=none: <b>every</b> schema designer needs a model');
  expect(shown).not.toContain('No schema design lists');
  expect(createWrapper(container).findTabs().findTabContent().getElement().querySelector('b')).toBeNull();
});

test('a skipped schema design with no reason leaves the reason out', () => {
  render({ patterns: [], schemaDesigns: { dynamodb: { status: 'skipped' } }, availability: 'unavailable' });
  openDesign();
  expect(text()).toContain('Schema design was skipped for DynamoDB');
  expect(text()).not.toContain('Reason');
  expect(text()).not.toContain('could not be read');
});

test('design files the page could not read say so, not that no design lists the query', () => {
  render({ patterns: [], schemaDesigns: {}, availability: 'unavailable' });
  openDesign();
  expect(text()).toContain('The schema design files could not be read, so this query\'s access patterns cannot be shown.');
  expect(text()).not.toContain('No schema design lists');
});

test('design files read only in part say the query may have a pattern not shown', () => {
  render({ patterns: [], schemaDesigns: {}, availability: 'partial' });
  openDesign();
  expect(text()).toContain('No access pattern for this query could be read. Some schema design files could not be read, so one may be missing here.');
});

// #2480 review: a query opened from the source table dialog moves focus into the panel.
test('a focus request moves focus into the loaded panel, once', () => {
  render({ focusRequest: 0 });
  expect(document.activeElement).toBe(document.body);
  render({ loading: true, focusRequest: 1 });
  expect(document.activeElement).toBe(document.body);
  render({ loading: false, focusRequest: 1 });
  const target = container.querySelector('[data-query-detail-focus]');
  expect(target).not.toBeNull();
  expect(document.activeElement).toBe(target);
  expect(target.getAttribute('aria-label')).toBe('Query 0452bed2 · UPDATE wp_woocommerce_api_keys');
  act(() => document.body.focus());
  act(() => target.blur());
  render({ loading: false, focusRequest: 1 });
  expect(document.activeElement).not.toBe(target);
});

test('an out of scope pattern says why, and markup in design text stays text', () => {
  render({ patterns: [{ ...ownerPattern, inScope: false, outOfScopeReason: 'Scans <b>all</b> rows', keyCondition: '<i>k</i>' }] });
  openDesign();
  expect(text()).toContain('Out of scope');
  expect(text()).toContain('Scans <b>all</b> rows');
  expect(text()).toContain('<i>k</i>');
  expect(container.querySelector('b')).toBeNull();
  expect(container.querySelector('i')).toBeNull();
});

test('the tabs are named, and so are their scroll buttons on a narrow screen', () => {
  render();
  const list = container.querySelector('[role="tablist"]');
  expect(list.getAttribute('aria-label')).toBe('Query details');
  const source = require('fs').readFileSync(require('path').join(__dirname, '../QueryDetailPanel.js'), 'utf8');
  expect(source).toMatch(/scrollLeftAriaLabel: t\('analysis-results\.tabs\.scroll-left'\)/);
  expect(source).toMatch(/scrollRightAriaLabel: t\('analysis-results\.tabs\.scroll-right'\)/);
});
