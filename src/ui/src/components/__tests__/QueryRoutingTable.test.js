// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2319: the Queries tab's one query table, on the real Cloudscape components
 * (Container, PropertyFilter, Table, Pagination) and the real English strings.
 * It is open, filters with a property filter, selects a query for the split panel,
 * and pages 25 rows at a time. The split panel itself needs the page's app layout,
 * so QueryDetailPanel.test.js covers its content and tests/e2e/test_ui.py the
 * panel in the browser.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import QueryRoutingTable from '../QueryRoutingTable';
import { buildQueryRoutingRows } from '../../utils/queryRouting';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const t = i18n.t.bind(i18n);
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

const labels = { dynamodb: 'DynamoDB', aurora_mysql: 'Aurora MySQL', elasticache: 'ElastiCache' };
const journey = (n, extra = {}) => ({
  query_id: `${String(n).padStart(4, '0')}${'f'.repeat(60)}`,
  source: {
    query_text: `SELECT ${n} FROM shop.wp_posts`,
    query_type: 'SELECT',
    tables_accessed: ['shop.wp_posts'],
  },
  assignment: { assigned_engine: 'dynamodb' },
  ...extra,
});
const moved = journey(2, {
  source: { query_text: 'UPDATE wp_wc_product_meta_lookup SET a = ?', query_type: 'UPDATE', tables_accessed: ['wp_wc_product_meta_lookup'] },
  assignment: { assigned_engine: 'aurora_mysql' },
});
const kinds = new Map([[moved.query_id, { moved: true, tied: true, openTie: false, overridden: false, pinned: false }]]);
const rowsOf = (journeys, links) => buildQueryRoutingRows(journeys, kinds, labels, links);

const render = (props) => act(() => {
  root.render(
    <QueryRoutingTable
      t={t}
      rows={rowsOf([journey(1), moved])}
      loading={false}
      error={null}
      hasLedger
      truncated={false}
      selectedId={null}
      onSelect={() => {}}
      {...props}
    />,
  );
});
const wrapper = () => createWrapper(container);
const bodyIds = () => wrapper().findTable().findRows().map(r => r.getElement().querySelector('[title]').getAttribute('title'));
const cellText = (row, col) => wrapper().findTable().findBodyCell(row, col).getElement().textContent;
const typeFilter = (text) => {
  const filter = wrapper().findPropertyFilter();
  act(() => filter.setInputValue(text));
  act(() => {
    filter.findNativeInput().keydown({ keyCode: 13, key: 'Enter' });
  });
};

test('the table is open when the tab opens, with no section to expand', () => {
  render();
  expect(container.querySelector('#query-routing-section')).not.toBeNull();
  expect(container.querySelector('button[aria-expanded]')).toBeNull();
  expect(wrapper().findHeader().findHeadingText().getElement().textContent).toBe('Query routing');
  expect(wrapper().findTable().findRows()).toHaveLength(2);
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(2)');
});

test('a property filter sits over the table, named for screen readers', () => {
  render();
  const input = wrapper().findPropertyFilter().findNativeInput().getElement();
  expect(input.getAttribute('aria-label')).toBe('Filter queries');
});

test('columns: id, operation, engine, destination, cached, routing, tables, statement', () => {
  render();
  const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim()).slice(1);
  expect(headers).toEqual(['Query', 'Operation', 'Engine', 'Destination', 'Cached', 'Routing', 'Tables', 'Statement']);
});

test('a row shows an eight character id, the engine badge in its catalog name, and a table without its schema', () => {
  render();
  const first = wrapper().findTable().findBodyCell(1, 2).getElement();
  expect(first.querySelector('[title]').textContent).toBe('0001ffff');
  expect(first.querySelector('[title]').getAttribute('title')).toHaveLength(64);
  expect(cellText(1, 4).trim()).toBe('DynamoDB');
  expect(wrapper().findTable().findBodyCell(1, 4).getElement().querySelector('[data-engine-badge="dynamodb"]')).not.toBeNull();
  expect(cellText(1, 8)).toBe('wp_posts');
  expect(cellText(1, 9)).toBe('SELECT 1 FROM shop.wp_posts');
});

test('a table name can break at its underscores and nowhere else', () => {
  render({ rows: rowsOf([moved]) });
  const cell = wrapper().findTable().findBodyCell(1, 8).getElement();
  expect(cell.textContent).toBe('wp_wc_product_meta_lookup');
  expect(cell.querySelectorAll('wbr')).toHaveLength(4);
});

test('the engine column keeps a badge on one line', () => {
  render();
  const cell = wrapper().findTable().findBodyCell(1, 4).getElement();
  expect(cell.querySelector('span[style*="nowrap"]')).not.toBeNull();
});

test('the destination and the cache engine come from the schema design, or read as a dash', () => {
  const links = {
    destinationsById: new Map([[moved.query_id, [{ engine: 'aurora_mysql', name: 'wp_postmeta' }]]]),
    cacheEngineById: new Map([[moved.query_id, 'elasticache']]),
  };
  render({ rows: rowsOf([journey(1), moved], links) });
  expect(cellText(2, 5)).toBe('wp_postmeta');
  expect(wrapper().findTable().findBodyCell(2, 6).getElement().querySelector('[data-engine-badge="elasticache"]')).not.toBeNull();
  expect(cellText(1, 5)).toBe('—');
  expect(cellText(1, 6)).toBe('—');
});

test('the routing column names why a listed query has a story, and nothing for the rest', () => {
  render();
  expect(cellText(2, 7)).toContain('Moved');
  expect(cellText(2, 7)).toContain('Tie settled');
  expect(cellText(1, 7)).toBe('Kept its first assignment');
});

test('the id button and the radio both select the query', () => {
  const picked = [];
  render({ onSelect: id => picked.push(id) });
  const link = wrapper().findTable().findRows()[1].getElement().querySelector('[role="button"]');
  expect(link.getAttribute('aria-label')).toBe(`Open the history and decision trace for query ${moved.query_id}`);
  act(() => link.click());
  act(() => wrapper().findTable().findRowSelectionArea(1).find('input').click());
  expect(picked).toEqual([moved.query_id, journey(1).query_id]);
});

test('the selected query is the selected row', () => {
  render({ selectedId: moved.query_id });
  const selected = wrapper().findTable().findSelectedRows();
  expect(selected).toHaveLength(1);
  expect(selected[0].getElement().textContent).toContain('UPDATE');
});

test('a property token narrows the rows and the counter says how many of all', () => {
  render();
  typeFilter('UPDATE');
  expect(wrapper().findTable().findRows()).toHaveLength(1);
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1 of 2)');
  expect(container.textContent).toContain('1 match');
});

test('a filter that matches nothing says so', () => {
  render();
  typeFilter('no-such-query');
  expect(wrapper().findTable().findRows()).toHaveLength(0);
  expect(container.textContent).toContain('No queries match.');
});

test('1,654 queries page 25 rows at a time, and filtering them renders only a page', () => {
  const journeys = Array.from({ length: 1654 }, (_, i) => journey(i));
  render({ rows: rowsOf(journeys) });
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1,654)');
  expect(wrapper().findTable().findRows()).toHaveLength(25);
  expect(wrapper().findPagination().findPageNumbers()).not.toHaveLength(0);
  expect(wrapper().findPagination().findPageNumbers().pop().getElement().textContent).toBe('67');
  typeFilter('SELECT');
  expect(container.textContent).toContain('1,654 matches');
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1,654 of 1,654)');
  act(() => wrapper().findPropertyFilter().findTokens()[0].findRemoveButton().click());
  typeFilter('SELECT 1650 ');
  expect(wrapper().findTable().findRows()).toHaveLength(1);
  expect(wrapper().findPagination()).toBeNull();
});

test('the pager buttons are named', () => {
  render({ rows: rowsOf(Array.from({ length: 30 }, (_, i) => journey(i))) });
  const pager = wrapper().findPagination();
  expect(pager.findNextPageButton().getElement().getAttribute('aria-label')).toBe('Next page');
  expect(pager.findPreviousPageButton().getElement().getAttribute('aria-label')).toBe('Previous page');
  act(() => pager.findNextPageButton().click());
  expect(wrapper().findTable().findRows()).toHaveLength(5);
});

test('an error or an empty list says so instead of an empty table', () => {
  render({ rows: [], error: 'server' });
  expect(container.textContent).toContain("Query details aren't available for this assessment");
  expect(container.textContent).not.toContain('server');
  expect(wrapper().findHeader().findCounter()).toBeNull();
  render({ rows: [] });
  expect(container.textContent).toContain('No queries match.');
});

test('without a move ledger, rows claim no routing history and the filter has no routing property', () => {
  render({ hasLedger: false });
  expect(cellText(1, 7)).toBe('—');
  expect(container.textContent).not.toContain('Kept its first assignment');
  expect(container.textContent).toContain(
    'This assessment predates the per-query decision record, so only the final engine is known.',
  );
});

test('a list cut at the page limit says so', () => {
  render({ truncated: true });
  expect(container.textContent).toContain('Only the first 2 queries are listed.');
  render({ truncated: false });
  expect(container.textContent).not.toContain('are listed.');
});

test('statement text is text: markup in a statement is not parsed', () => {
  const evil = journey(9, { source: { query_text: "SELECT '<img src=x onerror=alert(1)>' FROM t", query_type: 'SELECT', tables_accessed: ['t'] } });
  render({ rows: rowsOf([evil]) });
  expect(container.querySelector('img')).toBeNull();
  expect(cellText(1, 9)).toContain('<img src=x onerror=alert(1)>');
});
