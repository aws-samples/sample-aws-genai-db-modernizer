// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #212, #2321: the results page's "Table owners" callout, on the real Cloudscape
 * components and the real English strings. One table states every mapped table from
 * report.json's facts: its owner (the engine that takes its writes, its source of
 * truth), the engines that read a CDC copy of it, the reads a replica serves (the
 * former "Sources of truth and replicas" section, #992) and its cache. Each row is its
 * own facts.table_owner_lines line, its replica reads included (#2321): the component
 * never counts and never joins the capped facts.table_sources list for a figure (ADR-032).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import en from '../../locales/en.json';
import TableOwnersCallout from '../TableOwnersCallout';
import { ENGINE_LABELS } from '../../utils/engineNames';
import oldWording from './fixtures/oldWordingTableSources.json';

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

const RISK = 'Eventually consistent: 2 reads on Aurora MySQL read a copy kept current from DynamoDB via CDC, so each sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from DynamoDB.';
const SHARED_RISK = "Eventually consistent: a read on a replica reads a copy kept current from the table's owner (its source of truth) via CDC, so it sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from the owner.";
// What src/shared/report_facts.py shared_risk_sentence writes for these rows.
const SHARED_SENTENCE = `Every replicated table above has this risk. ${SHARED_RISK}`;

const ownership = {
  table_count: 4,
  owned_table_counts: { dynamodb: 2, aurora_mysql: 2 },
  no_owner_table_count: 0,
  also_read_table_count: 3,
  cached_table_count: 1,
  cache_engines: ['elasticache'],
  table_lines: [
    { table: 'wordpress.wp_postmeta', owner: 'dynamodb', owner_basis: 'writes', also_read_by: ['aurora_mysql'], cached_by: 'elasticache', replica_read_count: 2 },
    { table: 'wordpress.wp_usermeta', owner: 'aurora_mysql', owner_basis: 'most_queries', also_read_by: ['dynamodb'], cached_by: null, replica_read_count: 3 },
    { table: 'wordpress.wp_commentmeta', owner: 'dynamodb', owner_basis: 'writes', also_read_by: [], cached_by: null, replica_read_count: 0 },
    { table: 'wordpress.wp_split', owner: 'aurora_mysql', owner_basis: 'split_writes', also_read_by: ['dynamodb'], cached_by: null, replica_read_count: 1 },
  ],
};

const postmeta = {
  table: 'wordpress.wp_postmeta',
  source_of_truth: 'dynamodb',
  replica_engines: ['aurora_mysql'],
  write_query_count: 2,
  replica_read_query_ids: ['r1', 'r2'],
  replica_read_count: 2,
  risk: RISK,
  shares_replica_risk: true,
};
const options = {
  ...postmeta,
  table: 'wordpress.wp_options',
  replica_read_query_ids: ['r3'],
  replica_read_count: 1,
};
const sources = {
  written_table_count: 3,
  replicated_table_count: 2,
  split_write_table_count: 0,
  replicated_tables: [postmeta, options],
  shared_risk_sentence: SHARED_SENTENCE,
  more_replicated_count: 0,
  single_engine_tables: ['wordpress.wp_commentmeta'],
  single_engine_table_count: 1,
};

function render(facts) {
  act(() => root.render(<TableOwnersCallout synthesis={{ facts }} t={t} />));
}
function open() {
  const toggle = container.querySelector('[aria-expanded="false"]');
  act(() => toggle.click());
}
function renderOpen(facts) {
  render(facts);
  open();
}
const wrapper = () => createWrapper(container);
const rowOf = table => container.querySelector(`[data-table="${table}"]`).closest('tr');
const cell = (table, name) => rowOf(table).querySelector(`[data-cell="${name}"]`).textContent;
const tableOrder = () => [...container.querySelectorAll('tbody [data-table]')].map(n => n.getAttribute('data-table'));
const both = { table_owner_lines: ownership, table_sources: sources };
const withCounts = (counts) => ({
  ...ownership,
  table_lines: ownership.table_lines.map(line => ({ ...line, replica_read_count: counts[line.table] })),
});
const infoLink = () => container.querySelector('[data-figure="consistency-info"] a, [data-figure="consistency-info"] button, [data-figure="consistency-info"] [role="button"]');

test('one table states each table: owner, CDC to, replica reads, cached by', () => {
  renderOpen(both);
  const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim());
  expect(headers).toEqual(['Source table', 'Owner', 'CDC to', 'Replica reads', 'Cached by']);
  expect(cell('wordpress.wp_postmeta', 'owner')).toBe('DynamoDB');
  expect(cell('wordpress.wp_postmeta', 'cdc-to')).toBe('Aurora MySQL');
  expect(cell('wordpress.wp_postmeta', 'replica-reads')).toBe('2');
  expect(cell('wordpress.wp_postmeta', 'cached-by')).toBe('ElastiCache');
  expect(cell('wordpress.wp_usermeta', 'owner')).toBe('Aurora MySQL');
  expect(cell('wordpress.wp_usermeta', 'cdc-to')).toBe('DynamoDB');
  expect(cell('wordpress.wp_usermeta', 'cached-by')).toBe('—');
  expect(cell('wordpress.wp_commentmeta', 'cdc-to')).toBe('—');
  // #2321: 0 means no replica read, so it shows as 0, not a dash.
  expect(cell('wordpress.wp_commentmeta', 'replica-reads')).toBe('0');
  expect(rowOf('wordpress.wp_postmeta').querySelector('[data-engine-badge="dynamodb"]')).not.toBeNull();
});

test('a table written on several engines shows the owner and no single writer', () => {
  renderOpen(both);
  expect(cell('wordpress.wp_split', 'owner')).toBe('Aurora MySQL (no single writer)');
});

test("the replica read count is the owner line's own, never joined from table_sources", () => {
  renderOpen({
    table_owner_lines: withCounts({ 'wordpress.wp_postmeta': 28 }),
    table_sources: { ...sources, replicated_tables: [{ ...postmeta, replica_read_query_ids: ['a'], replica_read_count: 2 }] },
  });
  expect(cell('wordpress.wp_postmeta', 'replica-reads')).toBe('28');
});

test('a table nothing writes, or one past the 25 table_sources lists, still shows its count', () => {
  // #2321 review: wp_usermeta has no table_sources row (no write), and a replicated
  // table past the cap has none either; each owner line still carries its count.
  renderOpen({ table_owner_lines: ownership, table_sources: { ...sources, replicated_tables: [postmeta] } });
  expect(cell('wordpress.wp_usermeta', 'cdc-to')).toBe('DynamoDB');
  expect(cell('wordpress.wp_usermeta', 'replica-reads')).toBe('3');
  expect(cell('wordpress.wp_split', 'replica-reads')).toBe('1');
});

test('a dash only when the owner line has no count, as in a report saved before it', () => {
  renderOpen({ table_owner_lines: withCounts({ 'wordpress.wp_postmeta': 2 }), table_sources: sources });
  expect(cell('wordpress.wp_postmeta', 'replica-reads')).toBe('2');
  expect(cell('wordpress.wp_usermeta', 'replica-reads')).toBe('—');
});

test('owner lines without table_sources still show each count, with no Info link', () => {
  renderOpen({ table_owner_lines: ownership });
  const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim());
  expect(headers).toEqual(['Source table', 'Owner', 'CDC to', 'Replica reads', 'Cached by']);
  expect(cell('wordpress.wp_postmeta', 'replica-reads')).toBe('2');
  expect(cell('wordpress.wp_usermeta', 'replica-reads')).toBe('3');
  expect(cell('wordpress.wp_commentmeta', 'replica-reads')).toBe('0');
  expect(container.querySelector('[data-figure="consistency-info"]')).toBeNull();
  expect(container.textContent).not.toContain('more replicated');
});

test('there is no second section, and an old Sources of truth link lands on Table owners', () => {
  renderOpen(both);
  expect(container.textContent).not.toContain('Sources of truth and replicas');
  const anchor = container.querySelector('#sources-of-truth-section');
  expect(anchor).not.toBeNull();
  expect(anchor.closest('#table-owners-section')).not.toBeNull();
  expect(anchor.textContent).toContain('Table owners (4 mapped tables)');
  // The section and the Info link are the only disclosures.
  expect(container.querySelectorAll('[aria-expanded]').length).toBe(2);
});

test('is a container section collapsed when the page opens, headed by the mapped table count', () => {
  render(both);
  const header = container.querySelector('#table-owners-section');
  expect(header.textContent).toContain('Table owners (4 mapped tables)');
  expect(container.querySelector('[aria-expanded="false"]')).not.toBeNull();
  // Collapsed: the section's toggle and the Info link, both closed.
  expect(container.querySelectorAll('[aria-expanded="false"]').length).toBe(2);
  expect(container.querySelectorAll('[aria-expanded="true"]').length).toBe(0);
});

test('the table filters by table name or engine name, and says when nothing matches', () => {
  renderOpen(both);
  const filter = wrapper().findTextFilter();
  expect(filter.findInput().findNativeInput().getElement().getAttribute('aria-label')).toBe('Filter tables');
  act(() => filter.findInput().setInputValue('usermeta'));
  expect(tableOrder()).toEqual(['wordpress.wp_usermeta']);
  act(() => filter.findInput().setInputValue('elasticache'));
  expect(tableOrder()).toEqual(['wordpress.wp_postmeta']);
  act(() => filter.findInput().setInputValue('no such table'));
  expect(tableOrder()).toEqual([]);
  expect(container.textContent).toContain('No table matches the filter.');
  act(() => filter.findInput().setInputValue(''));
  expect(tableOrder()).toHaveLength(4);
});

test('the replica reads and owner columns sort, a table with no count after the others when most reads come first', () => {
  renderOpen({
    table_owner_lines: withCounts({ 'wordpress.wp_postmeta': 5, 'wordpress.wp_usermeta': 1 }),
    table_sources: sources,
  });
  const sortBy = (n) => act(() => wrapper().findTable().findColumnSortingArea(n).click());
  // Replica reads: first click sorts ascending, the second descending.
  sortBy(4);
  expect(tableOrder().slice(-2)).toEqual(['wordpress.wp_usermeta', 'wordpress.wp_postmeta']);
  sortBy(4);
  expect(tableOrder().slice(0, 2)).toEqual(['wordpress.wp_postmeta', 'wordpress.wp_usermeta']);
  expect(tableOrder().slice(2).sort()).toEqual(['wordpress.wp_commentmeta', 'wordpress.wp_split']);
  // Owner: by the engine's name.
  sortBy(2);
  const owners = tableOrder().map(table => cell(table, 'owner').replace(' (no single writer)', ''));
  expect(owners).toEqual([...owners].sort());
});

test('the consistency note sits behind an Info link, stating the risk once', () => {
  renderOpen({ table_owner_lines: ownership, table_sources: { ...sources, replicated_tables: [postmeta, options] } });
  expect(container.querySelector('[data-figure="consistency"]')).toBeNull();
  act(() => infoLink().click());
  const note = container.querySelector('[data-figure="consistency"]');
  expect(note.textContent).toContain("Each table's writes land on one engine, its source of truth.");
  const shared = container.querySelector('[data-figure="shared-risk"]').textContent;
  expect(shared).toBe(SHARED_SENTENCE);
  expect(shared).not.toContain('Every table above');
  expect(container.textContent.split('Eventually consistent: ').length - 1).toBe(1);
  expect(container.textContent).not.toContain(RISK);
});

// #2321 review: the note sits under every row with a CDC to engine, also wp_usermeta,
// which nothing writes and Aurora MySQL owns with CDC to DynamoDB, so the shared
// sentence names no engine that disagrees with such a row's owner.
test('the shared risk names no source that disagrees with a replicated row\'s owner', () => {
  renderOpen(both);
  act(() => infoLink().click());
  const shared = container.querySelector('[data-figure="shared-risk"]').textContent;
  const named = Object.values(ENGINE_LABELS).filter(name => shared.includes(name));
  const replicated = ownership.table_lines.filter(line => line.also_read_by.length > 0);
  expect(replicated.map(line => line.table)).toContain('wordpress.wp_usermeta');
  replicated.forEach(line => {
    const owner = cell(line.table, 'owner');
    expect([line.table, named.filter(name => !owner.includes(name))]).toEqual([line.table, []]);
  });
});

test('the Info link says whether the note is open and which element it controls', () => {
  renderOpen(both);
  const link = infoLink();
  expect(link.getAttribute('aria-expanded')).toBe('false');
  const controlled = link.getAttribute('aria-controls');
  expect(controlled).toBeTruthy();
  expect(document.getElementById(controlled)).not.toBeNull();
  act(() => link.click());
  expect(infoLink().getAttribute('aria-expanded')).toBe('true');
  expect(document.getElementById(controlled).querySelector('[data-figure="consistency"]')).not.toBeNull();
  act(() => infoLink().click());
  expect(infoLink().getAttribute('aria-expanded')).toBe('false');
});

test('prints the shared risk sentence the facts store, never its own wording', () => {
  renderOpen({ table_owner_lines: ownership, table_sources: { ...sources, shared_risk_sentence: 'Stored shared sentence.' } });
  act(() => infoLink().click());
  expect(container.querySelector('[data-figure="shared-risk"]').textContent).toBe('Stored shared sentence.');
});

test("an older report's block states its own risk and the shared one, as the engineering report does", () => {
  renderOpen({ table_owner_lines: { ...ownership, table_lines: [
    { table: 'wordpress.wp_posts', owner: 'dynamodb', owner_basis: 'writes', also_read_by: ['aurora_mysql'], cached_by: null },
    { table: 'wordpress.wp_options', owner: 'dynamodb', owner_basis: 'writes', also_read_by: ['aurora_mysql'], cached_by: null },
  ], table_count: 2 }, table_sources: oldWording.served });
  act(() => infoLink().click());
  const { shared, own_risk: ownRisk } = oldWording.expected;
  expect(container.querySelector('[data-figure="shared-risk"]').textContent).toContain(shared);
  const own = container.querySelector(`[data-own-risk="${ownRisk.table}"]`);
  expect(own.textContent).toContain(ownRisk.risk);
  expect(container.textContent.split('Eventually consistent: ').length - 1).toBe(2);
});

test('no Info link when no table has a replica', () => {
  renderOpen({ table_owner_lines: ownership, table_sources: { ...sources, replicated_tables: [], replicated_table_count: 0, shared_risk_sentence: null } });
  expect(container.querySelector('[data-figure="consistency-info"]')).toBeNull();
  expect(cell('wordpress.wp_postmeta', 'replica-reads')).toBe('2');
});

test('states the facts layer counts as stored, never counting the rows', () => {
  renderOpen({ table_owner_lines: { ...ownership, also_read_table_count: 7, cached_table_count: 5 }, table_sources: sources });
  const owned = [...container.querySelectorAll('[data-figure="owned-tables"] [data-engine]')].map(d => d.textContent);
  expect(owned).toEqual(['DynamoDB owns 2', 'Aurora MySQL owns 2']);
  expect(container.querySelector('[data-figure="also-read-tables"]').textContent)
    .toBe('7 tables are also read by another engine through CDC replicas');
  expect(container.querySelector('[data-figure="cached-tables"]').textContent)
    .toBe('5 tables are cached by ElastiCache');
  expect(container.querySelector('[data-figure="no-owner-tables"]')).toBeNull();
});

test('says how many tables only a cache design maps', () => {
  renderOpen({ table_owner_lines: { ...ownership, no_owner_table_count: 1 }, table_sources: sources });
  expect(container.querySelector('[data-figure="no-owner-tables"]').textContent).toBe('1 table has no owner engine');
});

test('with owner lines every table is a row, so no "more replicated tables" line; the source database and split writes stay', () => {
  renderOpen({
    table_owner_lines: ownership,
    table_sources: { ...sources, more_replicated_count: 3, split_write_table_count: 2, source_database_table_count: 24 },
  });
  const text = container.textContent;
  expect(text).not.toContain('more replicated');
  expect(text).toContain("2 tables have writes on more than one engine and no single source of truth; route each table's writes to one engine in the assignment review.");
  expect(container.querySelector('[data-figure="source-database-tables"]').textContent).toBe(
    "The source database stays the writer of 24 tables: no engine in the customer's engine scope can serve their writes. Each engine that reads one holds a replica kept current from the source database.",
  );
});

test('a job with no schema design lists each replicated table from the sources facts alone', () => {
  renderOpen({ table_sources: sources });
  expect(container.querySelector('#table-owners-section').textContent).toContain('Table owners (3 written tables)');
  const headers = wrapper().findTable().findColumnHeaders().map(h => h.getElement().textContent.trim());
  expect(headers).toEqual(['Source table', 'Owner', 'CDC to', 'Replica reads']);
  expect(cell('wordpress.wp_postmeta', 'owner')).toBe('DynamoDB');
  expect(cell('wordpress.wp_postmeta', 'cdc-to')).toBe('Aurora MySQL');
  expect(cell('wordpress.wp_options', 'replica-reads')).toBe('1');
  expect(container.textContent).toContain('1 written table runs every query on the source of truth, with no replica: wordpress.wp_commentmeta.');
});

test('a job with no schema design still says how many more replicated tables the JSON lists', () => {
  renderOpen({ table_sources: { ...sources, more_replicated_count: 3 } });
  expect(container.textContent).toContain('3 more replicated tables are listed in the Assessment Data (JSON), under table_sources.');
});

test('renders nothing for a report that maps no table and writes none', () => {
  render({ table_owner_lines: null });
  expect(container.innerHTML).toBe('');
  render({ table_owner_lines: { ...ownership, table_count: 0, table_lines: [] } });
  expect(container.innerHTML).toBe('');
  render({ table_sources: { ...sources, written_table_count: 0 } });
  expect(container.innerHTML).toBe('');
  render({});
  expect(container.innerHTML).toBe('');
});

test('states the relational-step rule for a report written since #2166', () => {
  renderOpen({ table_owner_lines: { ...ownership, unwritten_owner_rule: 'relational_step' }, table_sources: sources });
  const rule = container.querySelector('[data-figure="owner-rule"]').textContent;
  expect(rule).toBe(en['analysis-results.table-owners.rule']);
  expect(rule).toContain("a table no query writes stays on the plan's relational engine");
  expect(rule).toContain('CDC to names those engines');
});

test("states an older report's own rule above its most-queries rows", () => {
  renderOpen({ table_owner_lines: { ...ownership, unwritten_owner_rule: 'most_queries' }, table_sources: sources });
  const rule = container.querySelector('[data-figure="owner-rule"]').textContent;
  expect(rule).toBe(en['analysis-results.table-owners.rule-most-queries']);
  expect(rule).toContain('is owned by the engine that serves most of its queries');
  expect(rule).not.toContain("plan's relational engine");
});
