// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2325: the Table design's one table of tables, and the body of its detail panel,
 * on the real Cloudscape components and the real English strings. The split panel
 * needs the page's app layout, so this renders its body; the panel in the browser
 * is covered by tests/e2e/test_ui.py.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import AuroraDesignTables, { AuroraLeftOutAlert } from '../AuroraDesignTables';
import { AuroraTableDetailBody } from '../AuroraTableDetailPanel';
import { withTableDetails } from '../../utils/auroraDesign';

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

const raw = (name, extra = {}) => ({
  tableName: name,
  columns: [{ name: 'id', auroraType: 'BIGINT', sourceType: null }, { name: 'title', auroraType: 'TEXT', sourceType: null }],
  primaryKey: ['id'],
  indexes: ['CREATE INDEX i ON wp_posts (title)'],
  foreignKeys: [],
  ...extra,
});
const mkDesign = (names, statements = null) => ({
  engine: 'aurora_mysql',
  displayName: 'Aurora MySQL',
  tables: withTableDetails(
    names.map(n => raw(n, { ddlStatements: statements && statements[n] })),
    [{ engine: 'aurora_mysql', table: names[0], query_count: 1, query_ids: ['q1'] }],
    [{ sourceTable: names[0], queries: [{ queryId: 'q1', excerpt: 'SELECT * FROM x', queryType: 'SELECT', callsPerSecond: 2, reason: 'Relational joins' }] }],
    'CREATE TABLE whole (id BIGINT);',
  ),
});

const render = (props) => act(() => {
  root.render(<AuroraDesignTables design={mkDesign(['wp_posts', 'wp_users'])} count={2} selectedKey={null} onSelect={() => {}} t={t} {...props} />);
});
const wrapper = () => createWrapper(container);

test('one table lists the design tables with their figures', () => {
  render();
  const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim()).slice(1);
  expect(headers).toEqual(['Table', 'Columns', 'Primary key', 'Indexes', 'Foreign keys', 'Queries']);
  expect(wrapper().findTable().findRows()).toHaveLength(2);
  const row = (c) => wrapper().findTable().findBodyCell(1, c).getElement().textContent;
  expect([row(2), row(3), row(4), row(5), row(6), row(7)]).toEqual(['wp_posts', '2', 'id', '1', '0', '1']);
  expect(wrapper().findHeader().findHeadingText().getElement().textContent).toBe("2 tables");
});

test('every column has a fixed width, so a scan down the rows does not zigzag', () => {
  render();
  const widths = wrapper().findTable().findColumnHeaders().slice(1).map(h => h.getElement().style.width);
  expect(widths).toEqual(['220px', '90px', '180px', '90px', '110px', '90px']);
});

test('the text filter narrows the rows by name and says how many match', () => {
  render();
  act(() => wrapper().findTextFilter().findInput().setInputValue('USERS'));
  expect(wrapper().findTable().findRows()).toHaveLength(1);
  expect(wrapper().findTable().findBodyCell(1, 2).getElement().textContent).toBe('wp_users');
  expect(wrapper().findTextFilter().findResultsCount().getElement().textContent).toBe('1 match');
  expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1 of 2)');
  act(() => wrapper().findTextFilter().findInput().setInputValue('nothing'));
  expect(container.textContent).toContain('No table matches the filter.');
});

test('a name link and a row radio both pick the table', () => {
  const onSelect = jest.fn();
  render({ onSelect });
  act(() => wrapper().findTable().findBodyCell(2, 2).find('a').click());
  expect(onSelect).toHaveBeenLastCalledWith('1');
  act(() => wrapper().findTable().findRowSelectionArea(1).find('input').click());
  expect(onSelect).toHaveBeenLastCalledWith('0');
});

test('the picked table is the selected row', () => {
  render({ selectedKey: '1' });
  expect(wrapper().findTable().findSelectedRows().map(r => r.getElement().textContent)[0]).toContain('wp_users');
});

test('a design of 147 tables renders 25 rows a page', () => {
  const many = Array.from({ length: 147 }, (_, i) => `t_${i}`);
  render({ design: mkDesign(many), count: 147 });
  expect(wrapper().findTable().findRows()).toHaveLength(25);
  expect(wrapper().findPagination().findPageNumbers()).toHaveLength(6);
  act(() => wrapper().findPagination().findPageNumberByIndex(6).click());
  expect(wrapper().findTable().findRows()).toHaveLength(22);
});

test('the detail shows columns, keys, the table queries with their reason, and its DDL as text', () => {
  const design = mkDesign(['wp_posts']);
  design.tables[0].ddlStatements = ["CREATE TABLE wp_posts (a TEXT DEFAULT '<img src=x onerror=alert(1)>');", '-- Index not created: note with no semicolon'];
  act(() => root.render(<AuroraTableDetailBody table={design.tables[0]} engineName="Aurora MySQL" t={t} />));
  const text = container.textContent;
  expect(text).toContain('BIGINT');
  expect(text).toContain('Relational joins');
  expect(text).toContain('SELECT * FROM x');
  expect(text).toContain('CREATE INDEX i ON wp_posts (title)');
  expect(text).toContain('<img src=x onerror=alert(1)>');
  expect(container.querySelector('img')).toBeNull();
  expect(text).toContain('-- Index not created: note with no semicolon');
  expect(text).not.toContain('CREATE TABLE whole');
  expect(wrapper().findAllTables()).toHaveLength(2);
});

test('a table with no queries says so, and a design with no stored statements shows the whole script with a note', () => {
  const design = mkDesign(['wp_posts', 'wp_users']);
  act(() => root.render(<AuroraTableDetailBody table={design.tables[1]} engineName="Aurora MySQL" t={t} />));
  expect(container.textContent).toContain('No query was routed to Aurora for this table.');
  expect(container.textContent).toContain('saved before each table kept its own statements');
  expect(container.textContent).toContain('CREATE TABLE whole (id BIGINT);');
});

test('the panel counts with the shared formatter', () => {
  const design = mkDesign(['wp_posts']);
  act(() => root.render(<AuroraTableDetailBody table={{ ...design.tables[0], queryCount: 1234 }} engineName="Aurora MySQL" t={t} />));
  expect(container.textContent).toContain('(1,234)');
});

test('two tables with one name open apart', () => {
  const onSelect = jest.fn();
  const names = ['orders', 'orders'];
  render({ design: mkDesign(names, { orders: ['X'] }), count: 2, onSelect });
  act(() => wrapper().findTable().findBodyCell(2, 2).find('a').click());
  expect(onSelect).toHaveBeenLastCalledWith('1');
});

test('the filter box has a named clear button once text is typed', () => {
  render();
  act(() => wrapper().findTextFilter().findInput().setInputValue('wp'));
  const clear = container.querySelector('button[aria-label="Clear filter"]');
  expect(clear).not.toBeNull();
});

// #2484: a table the design leaves out is named above the list, never just missing.
const LEFT_OUT = {
  engine: 'aurora_postgresql',
  object_count: 2,
  objects: [
    { name: 'Sales.orders', feature: 'colliding_table_name', label: 'Colliding table name', object: 'table', reason: 'its schema and name match another table of the design.' },
    { name: '<img src=x onerror=alert(1)>', feature: 'unsafe_name', label: 'Unsafe name', object: 'table', reason: 'its name holds a control character.' },
  ],
};

test('the left-out alert names each object, its kind of note and its reason, as text', () => {
  act(() => root.render(<AuroraLeftOutAlert leftOut={LEFT_OUT} t={t} />));
  const alert = wrapper().findAlert();
  expect(alert).not.toBeNull();
  expect(alert.findHeader().getElement().textContent).toBe('2 objects are not created');
  const items = [...container.querySelectorAll('#aurora-design-left-out li')].map(li => li.textContent);
  expect(items).toEqual([
    'Colliding table name on table Sales.orders: its schema and name match another table of the design.',
    'Unsafe name on table <img src=x onerror=alert(1)>: its name holds a control character.',
  ]);
  expect(container.querySelector('img')).toBeNull();
});

test('the header counts as the fact does, one object included', () => {
  act(() => root.render(<AuroraLeftOutAlert leftOut={{ ...LEFT_OUT, object_count: 1, objects: [LEFT_OUT.objects[0]] }} t={t} />));
  expect(wrapper().findAlert().findHeader().getElement().textContent).toBe('1 object is not created');
});

test('no alert when the design leaves nothing out', () => {
  act(() => root.render(<AuroraLeftOutAlert leftOut={null} t={t} />));
  expect(wrapper().findAlert()).toBeNull();
  act(() => root.render(<AuroraLeftOutAlert leftOut={{ ...LEFT_OUT, object_count: 0, objects: [] }} t={t} />));
  expect(wrapper().findAlert()).toBeNull();
});
