// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2446: the access pattern explorer's "By source table" view, now the Data model
 * tab's Source tables section, on the real Cloudscape components and English strings.
 * One row per source table with a filter on the name; a row opens its destination
 * designs in a dialog, whose queries open in the page's query detail panel.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import SourceTablesSection from '../SourceTablesSection';
import { cacheLayerPatterns, ownerAccessPatterns, sourceTableRows } from '../../utils/accessPatterns';

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

const Q1 = `1111${'a'.repeat(60)}`;
const designs = [
  {
    target_type: 'dynamodb',
    content: {
      table_definitions: [{
        table_name: 'orders',
        partition_key: { attribute_name: 'pk', attribute_type: 'S' },
        sort_key: { attribute_name: 'sk', attribute_type: 'S' },
        gsis: [{ gsi_name: 'by_user' }],
        source_tables: ['shop.orders', 'shop.order_items'],
      }],
      access_patterns: [
        { pattern_id: 'D1', operation: 'GetItem', table_name: 'orders', source_tables: ['shop.orders'], source_query_ids: [Q1], description: 'Order by id <b>x</b>' },
        { pattern_id: 'D2', operation: 'Query', table_name: 'orders', gsi_name: 'by_user', source_tables: ['shop.order_items'], source_query_ids: [] },
      ],
    },
  },
  {
    target_type: 'elasticache',
    content: { access_patterns: [{ pattern_id: 'C1', operation: 'GET', key_pattern: 'order:{id}', source_tables: ['shop.orders'] }] },
  },
];
const overlay = { engine: 'elasticache' };
const owner = ownerAccessPatterns(designs, overlay);
const cache = cacheLayerPatterns(designs, overlay);
const destTableLookup = {
  'dynamodb::orders': { engine: 'dynamodb', name: 'orders', def: designs[0].content.table_definitions[0], allSourceTables: ['orders', 'order_items'] },
};

const render = (props) => act(() => {
  root.render(
    <SourceTablesSection
      rows={sourceTableRows(owner, cache)}
      availability="available"
      expectedCount={3}
      shownCount={3}
      cacheEngine="elasticache"
      destTableLookup={destTableLookup}
      onSelectQuery={() => {}}
      t={t}
      {...props}
    />,
  );
});
const wrapper = () => createWrapper(container);
const sectionTable = () => wrapper().find('#source-tables-section').findTable();
const cellText = (row, col) => sectionTable().findBodyCell(row, col).getElement().textContent;
const dialog = () => createWrapper(document.body).findModal();

test('one row per source table, by name, under its own heading and count', () => {
  render();
  expect(container.querySelector('#source-tables-section')).not.toBeNull();
  expect(wrapper().findHeader().findHeadingText().getElement().textContent).toBe('Source tables');
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(2)');
  const headers = sectionTable().findColumnHeaders().map(h => h.getElement().textContent.trim());
  expect(headers).toEqual(['Source table', 'Engines', 'Destination tables', 'Merged with', 'Operations']);
  expect(cellText(1, 1)).toBe('order_items');
  expect(cellText(2, 1)).toBe('orders');
});

test('a row names its engines, the cache layer as such, the tables it merges with and its operations', () => {
  render();
  expect(cellText(2, 2)).toContain('DynamoDB');
  expect(cellText(2, 2)).toContain('ElastiCache (cache layer)');
  expect(cellText(2, 3)).toContain('order:{id}');
  expect(cellText(2, 4)).toBe('order_items');
  expect(cellText(2, 5)).toBe('GET, GetItem');
  expect(cellText(1, 4)).toBe('orders');
});

test('the filter narrows the rows by name and the counter says how many of all', () => {
  render();
  act(() => wrapper().findTextFilter().findInput().setInputValue('items'));
  expect(sectionTable().findRows()).toHaveLength(1);
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1 of 2)');
  expect(wrapper().findTextFilter().findInput().findNativeInput().getElement().getAttribute('aria-label')).toBe('Filter source tables');
});

test('a row opens its destination design with keys and indexes, and a query opens in the panel', () => {
  const onSelectQuery = jest.fn();
  render({ onSelectQuery });
  act(() => sectionTable().findBodyCell(2, 1).findLink().click());
  const modal = dialog();
  expect(modal.isVisible()).toBe(true);
  expect(modal.findHeader().getElement().textContent).toContain('Source table orders');
  const body = modal.findContent().getElement().textContent;
  expect(body).toContain('pk (S)');
  expect(body).toContain('sk (S)');
  expect(body).toContain('by_user');
  expect(body).toContain('Consolidates with');
  expect(body).toContain('order_items');
  expect(body).toContain('Order by id <b>x</b>');
  expect(modal.findContent().getElement().querySelector('b')).toBeNull();
  const link = modal.findContent().findLink();
  expect(link.getElement().textContent).toBe('1111aaaa');
  act(() => link.click());
  // #2480 review: the query opens with focus moved to its panel.
  expect(onSelectQuery).toHaveBeenCalledWith(Q1, { focusPanel: true });
  expect(dialog().isVisible()).toBe(false);
});

test('the dialog has a tab per engine, the cache layer named as such', () => {
  render();
  act(() => sectionTable().findBodyCell(2, 1).findLink().click());
  const tabs = dialog().findContent().findTabs().findTabLinks().map(l => l.getElement().textContent);
  expect(tabs).toEqual(['DynamoDB', 'ElastiCache (cache layer)']);
});

test('design files the page cannot read are an alert, not an empty table; a partial list says so', () => {
  render({ rows: [], availability: 'unavailable', expectedCount: 4, shownCount: 0 });
  expect(container.textContent).toContain("Access pattern details aren't available for this assessment");
  expect(wrapper().findTable()).toBeNull();
  render({ availability: 'partial', expectedCount: 9, shownCount: 3 });
  expect(container.textContent).toContain('Only part of the access patterns are listed');
});

test('a legacy report with ElastiCache as an owner lists it without the cache layer label', () => {
  const legacy = ownerAccessPatterns(designs, null);
  render({ rows: sourceTableRows(legacy, []), cacheEngine: undefined });
  expect(cellText(2, 2)).toContain('ElastiCache');
  expect(cellText(2, 2)).not.toContain('cache layer');
});
