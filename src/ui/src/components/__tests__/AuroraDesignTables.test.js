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
import AuroraDesignTables, { AuroraAppLayerNotes, AuroraChangeSummary, AuroraLeftOutAlert } from '../AuroraDesignTables';
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

describe('AuroraAppLayerNotes (#2545)', () => {
  const NOTES = [
    { label: 'Index renamed', object: 'usermeta.meta_key', text: 'Created as usermeta_b_idx: postmeta already has an index named meta_key in this schema.' },
    { label: 'Trigger', object: '<img src=x onerror=alert(1)>', text: 'Run `UPDATE` from the application.' },
  ];

  test('a collapsed section lists each note: its kind, its object and its text', () => {
    act(() => root.render(<AuroraAppLayerNotes notes={NOTES} t={t} />));
    const section = wrapper().findExpandableSection();
    expect(section).not.toBeNull();
    expect(section.findHeader().getElement().textContent).toBe('Application-layer notes');
    expect(section.findExpandButton().getElement().getAttribute('aria-expanded')).toBe('false');
    act(() => section.findExpandButton().click());
    const items = [...container.querySelectorAll('#aurora-design-app-layer-notes li')].map(li => li.textContent);
    expect(items).toEqual([
      'Index renamed on usermeta.meta_key: Created as usermeta_b_idx: postmeta already has an index named meta_key in this schema.',
      'Trigger on <img src=x onerror=alert(1)>: Run UPDATE from the application.',
    ]);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('#aurora-design-app-layer-notes code')).not.toBeNull();
  });

  test('no section when the design has no notes', () => {
    act(() => root.render(<AuroraAppLayerNotes notes={[]} t={t} />));
    expect(wrapper().findExpandableSection()).toBeNull();
  });
});

// #2289: the change from the source, as facts.design_changes states it, with the rows
// synthesis stores once on the design summary.
describe('the change from the source (#2289)', () => {
  const COLUMNS = [
    { name: 'id', aurora_type: 'BIGINT', source_type: 'bigint(20)', nullable: false, default: null, identity: true, change: 'same' },
    { name: 'title', aurora_type: 'TEXT', source_type: 'varchar(255)', nullable: true, default: "'<b>x</b>'", identity: false, change: 'type_changed' },
    { name: 'legacy', aurora_type: null, source_type: 'int', nullable: null, default: null, identity: null, change: 'skipped' },
  ];
  const index = (name, columns, change, extra = {}) => ({
    name, columns, unique: false, change, source_name: null, differences: [], source_columns: null, source_unique: null, reason: null, ...extra,
  });
  const INDEXES = [
    index('i', ['title'], 'same'),
    index('u_new', ['id', 'title'], 'renamed', { unique: true, source_name: 'u_old' }),
    index('email_uq', ['title'], 'changed', { differences: ['unique'], source_columns: ['title'], source_unique: true }),
    index('lower_title', [], 'step1'),
    index('gone', ['legacy'], 'skipped', { reason: 'Source index has expression keys: the draft leaves it out.' }),
    index('bare', ['legacy'], 'skipped'),
  ];
  const factsRow = (name, kinds, changed) => ({
    table_name: name, schema_name: null, source_table: `wordpress.${name}`, changed,
    change_kinds: kinds, change_counts: Object.fromEntries(kinds.map(k => [k, 1])),
  });
  const NAMES = ['wp_posts', 'wp_users', 'wp_terms', 'wp_links'];
  const CHANGES = {
    engine: 'aurora_mysql',
    table_count: 3,
    changed_table_count: 1,
    change_counts: { type_changed: 1, index_renamed: 1, index_changed: 1, index_skipped: 2, translated: 4, index_step1: 1 },
    table_changes: [
      factsRow('wp_posts', ['type_changed', 'index_renamed', 'index_changed', 'index_skipped', 'index_step1'], true),
      factsRow('wp_users', [], false),
      // A heterogeneous table whose types are all translated: no change.
      factsRow('wp_terms', ['translated'], false),
    ],
    uncompared_table_count: 1,
    uncompared_tables: [{ table_name: 'wp_links', schema_name: null, reason_code: 'ambiguous_source', reason: 'two source schemas hold a table of its name and the design names none' }],
  };
  const SUMMARY_TABLES = NAMES.map(n => ({
    table_name: n,
    source_changes: n === 'wp_links' ? { uncompared: 'ambiguous_source' } : { source_table: `wordpress.${n}`, columns: COLUMNS, indexes: INDEXES },
  }));
  const changedDesign = (changes = CHANGES) => {
    const design = mkDesign(NAMES);
    design.changes = changes;
    design.tables = withTableDetails(
      NAMES.map(n => raw(n)), [], [], '',
      changes ? { facts: changes, summaryTables: SUMMARY_TABLES } : null,
    );
    return design;
  };
  const renderChanged = (props) => act(() => {
    root.render(<AuroraDesignTables design={changedDesign()} count={4} selectedKey={null} onSelect={() => {}} t={t} {...props} />);
  });
  const names = () => wrapper().findTable().findRows().map((_, i) => wrapper().findTable().findBodyCell(i + 1, 2).getElement().textContent);
  const pick = (value) => {
    const select = wrapper().findSelect();
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue(value));
  };

  test('the list has a Change column with each table kinds of change as badges', () => {
    renderChanged();
    const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim()).slice(1);
    expect(headers[headers.length - 1]).toBe('Change from source');
    const cell = (r) => wrapper().findTable().findBodyCell(r, 8).getElement();
    expect(cell(1).textContent).toContain('Type changed');
    expect(cell(1).textContent).toContain('Recreated by step 1');
    expect(cell(2).textContent).toBe('Same');
    expect(cell(3).textContent).toBe('Translated');
    expect(cell(4).textContent).toBe('Not compared');
  });

  test('the change filter narrows the rows to a kind, to any change or to none', () => {
    renderChanged();
    const select = wrapper().findSelect();
    const ids = select.findTrigger().getElement().getAttribute('aria-labelledby').split(' ');
    const labels = ids.map(id => document.getElementById(id)).filter(Boolean).map(e => e.textContent);
    expect(labels).toContain('Filter tables by their change from the source');
    act(() => select.openDropdown());
    const options = select.findDropdown().findOptions().map(o => o.getElement().textContent);
    expect(options).toEqual(['All tables', 'Any change', 'No change', 'Type changed', 'Index renamed', 'Index changed', 'Index not created', 'Translated', 'Recreated by step 1']);
    act(() => select.selectOptionByValue('any'));
    expect(names()).toEqual(['wp_posts']);
    expect(wrapper().findHeader().findCounter().getElement().textContent.trim()).toBe('(1 of 4)');
    pick('same');
    expect(names()).toEqual(['wp_users', 'wp_terms']);
    pick('translated');
    expect(names()).toEqual(['wp_terms']);
  });

  test('on a heterogeneous design the summary count and the Any change filter agree', () => {
    act(() => root.render(<><AuroraChangeSummary changes={CHANGES} t={t} /><AuroraDesignTables design={changedDesign()} count={4} selectedKey={null} onSelect={() => {}} t={t} /></>));
    expect(container.querySelector('[data-figure="design-changes"]').textContent).toContain('Tables that change from the source: 1 of 3.');
    pick('any');
    expect(wrapper().findTable().findRows()).toHaveLength(CHANGES.changed_table_count);
  });

  test('every header sorts, and sorting by change puts the changed table last', () => {
    renderChanged();
    const table = wrapper().findTable();
    act(() => table.findColumnSortingArea(8).click());
    expect(names()).toEqual(['wp_links', 'wp_users', 'wp_terms', 'wp_posts']);
    // The header holds its text, the sorting control and the resize handle only: nothing else interactive.
    const header = table.findColumnHeaders()[7].getElement();
    expect(header.querySelectorAll('a, input, button:not([aria-roledescription="Column resize handle"])')).toHaveLength(0);
  });

  test('a report written before the comparison shows no Change column and no change filter', () => {
    render();
    expect(wrapper().findSelect()).toBeNull();
    const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim());
    expect(headers).not.toContain('Change from source');
  });

  test('the panel lists each column with its source type, nullable, default and change, as text', () => {
    const design = changedDesign();
    act(() => root.render(<AuroraTableDetailBody table={design.tables[0]} engineName="Aurora MySQL" t={t} />));
    const columns = wrapper().findAllTables()[0];
    const headers = columns.findColumnHeaders().map(h => h.getElement().textContent.trim());
    expect(headers).toEqual(['Column', 'Aurora type', 'Source type', 'Nullable', 'Default', 'Change from source']);
    const row = (r) => [1, 2, 3, 4, 5, 6].map(c => columns.findBodyCell(r, c).getElement().textContent);
    expect(row(1)).toEqual(['id', 'BIGINT', 'bigint(20)', 'No', 'Identity', 'Same']);
    expect(row(2)).toEqual(['title', 'TEXT', 'varchar(255)', 'Yes', "'<b>x</b>'", 'Type changed']);
    expect(row(3)).toEqual(['legacy', '—', 'int', '—', '—', 'Not created']);
    expect(container.querySelector('b')).toBeNull();
  });

  test('the panel lists the indexes as a table, each change with its detail or reason', () => {
    const design = changedDesign();
    act(() => root.render(<AuroraTableDetailBody table={design.tables[0]} engineName="Aurora MySQL" t={t} />));
    const indexes = wrapper().findAllTables()[1];
    const headers = indexes.findColumnHeaders().map(h => h.getElement().textContent.trim());
    expect(headers).toEqual(['Index', 'Columns', 'Unique', 'Change from source']);
    const row = (r) => [1, 2, 3, 4].map(c => indexes.findBodyCell(r, c).getElement().textContent);
    expect(row(1)).toEqual(['i', 'title', 'No', 'Same']);
    expect(row(2)).toEqual(['u_new', 'id, title', 'Yes', 'RenamedSource name u_old']);
    expect(row(3)).toEqual(['email_uq', 'title', 'No', 'ChangedDiffers from the source in: uniquenessSource key title, unique']);
    expect(row(4)).toEqual(['lower_title', '—', 'No', 'Recreated by step 1The design script does not create this expression index; step 1 recreates it when it moves this table.']);
    expect(row(5)).toEqual(['gone', 'legacy', 'No', 'Not createdSource index has expression keys: the draft leaves it out.']);
    expect(row(6)).toEqual(['bare', 'legacy', 'No', 'Not createdThe design does not create this index and records no note about it.']);
    expect(container.textContent).not.toContain('CREATE INDEX i ON wp_posts (title)');
  });

  test('a table not compared says why in the panel', () => {
    const design = changedDesign();
    act(() => root.render(<AuroraTableDetailBody table={design.tables[3]} engineName="Aurora MySQL" t={t} />));
    expect(container.querySelector('[data-figure="table-uncompared"]').textContent).toBe(
      'This table is not compared with the source: two source schemas hold a table of its name and the design names none.',
    );
    expect(container.textContent).toContain('CREATE INDEX i ON wp_posts (title)');
  });

  test('the panel keeps the DDL in a collapsed section', () => {
    const design = changedDesign();
    design.tables[0].ddlStatements = ['CREATE TABLE wp_posts (id BIGINT);'];
    act(() => root.render(<AuroraTableDetailBody table={design.tables[0]} engineName="Aurora MySQL" t={t} />));
    const section = wrapper().findExpandableSection();
    expect(section.findHeader().getElement().textContent).toBe('Generated DDL');
    expect(section.findExpandButton().getElement().getAttribute('aria-expanded')).toBe('false');
  });

  test('the summary line reads the facts counts, and says nothing with no compared table', () => {
    act(() => root.render(<AuroraChangeSummary changes={{ ...CHANGES, change_counts: { ...CHANGES.change_counts, translated: 1200 } }} t={t} />));
    expect(container.textContent).toBe('Tables that change from the source: 1 of 3. 1,200 column types are translated as the deterministic translation writes them. Step 1 recreates 1 expression index the design script does not create, when it moves its table. 1 table is not compared with the source.');
    act(() => root.render(<AuroraChangeSummary changes={{ ...CHANGES, changed_table_count: 0, change_counts: {}, uncompared_table_count: 0 }} t={t} />));
    expect(container.textContent).toBe('All 3 tables match the source tables they were compared with.');
    act(() => root.render(<AuroraChangeSummary changes={{ ...CHANGES, table_count: 0, changed_table_count: 0, change_counts: {} }} t={t} />));
    expect(container.textContent).toBe('1 table is not compared with the source.');
    act(() => root.render(<AuroraChangeSummary changes={{ ...CHANGES, table_count: 0, changed_table_count: 0, change_counts: {}, uncompared_table_count: 0 }} t={t} />));
    expect(container.textContent).toBe('');
    act(() => root.render(<AuroraChangeSummary changes={null} t={t} />));
    expect(container.textContent).toBe('');
  });
});
