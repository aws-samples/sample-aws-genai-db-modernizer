// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #822: the results page's query routing table lists every query of the
 * assessment (the query-journeys list), so a query's decision trace opens
 * without schema design. Rows only format what the API and report.json's
 * facts already hold: the engine from the journey, and why a query's routing
 * is listed from `facts.query_trace.listed_queries` (never recomputed here).
 */
import {
  buildQueryRoutingRows, filterQueryRows, flattenSql, queryExcerpt, queryFilterOptions, queryPatternLinks,
} from '../queryRouting';
import { routingHistoryRecorded, routingKindsById } from '../reportFacts';

const journeys = [
  {
    query_id: 'a'.repeat(64),
    source: { query_text: 'SELECT  *\n FROM wp_posts WHERE ID = ?', tables_accessed: ['wp_posts'] },
    assignment: { assigned_engine: 'dynamodb' },
  },
  {
    query_id: 'b'.repeat(64),
    source: { query_text: 'REPLACE INTO wp_wc_product_meta_lookup VALUES (?)', tables_accessed: ['wp_wc_product_meta_lookup'] },
    assignment: { assigned_engine: 'aurora_mysql' },
  },
  { query_id: 'c'.repeat(64), source: { query_text: null, tables_accessed: [] }, assignment: null },
];

const synthesis = {
  facts: {
    query_trace: {
      listed_queries: [
        { query_id: 'b'.repeat(64), moved: true, tied: false, open_tie: false, overridden: true, pinned: false },
      ],
    },
  },
};

const labels = { dynamodb: 'DynamoDB', aurora_mysql: 'Aurora MySQL' };

test('routingKindsById reads the facts layer, and is empty without it', () => {
  const kinds = routingKindsById(synthesis);
  expect(kinds.get('b'.repeat(64))).toEqual({ moved: true, tied: false, openTie: false, overridden: true, pinned: false });
  expect(kinds.has('a'.repeat(64))).toBe(false);
  expect(routingKindsById({}).size).toBe(0);
  expect(routingKindsById(null).size).toBe(0);
});

test('one row per query, with its engine display name, tables and listing kinds', () => {
  const rows = buildQueryRoutingRows(journeys, routingKindsById(synthesis), labels);
  expect(rows.map((r) => r.queryId)).toEqual(['a'.repeat(64), 'b'.repeat(64), 'c'.repeat(64)]);
  expect(rows[0]).toMatchObject({ engine: 'dynamodb', engineName: 'DynamoDB', tables: ['wp_posts'], kinds: null });
  expect(rows[0].statement).toBe('SELECT * FROM wp_posts WHERE ID = ?');
  expect(rows[0].excerpt).toBe('SELECT * FROM wp_posts WHERE ID = ?');
  expect(rows[1]).toMatchObject({ engineName: 'Aurora MySQL', kinds: { moved: true, overridden: true } });
  expect(rows[2]).toMatchObject({ engine: null, engineName: null, excerpt: '', tables: [] });
});

test('an engine the label map does not know keeps its id', () => {
  const [row] = buildQueryRoutingRows([{ query_id: 'x', assignment: { assigned_engine: 'neptune' } }], new Map(), labels);
  expect(row.engineName).toBe('neptune');
});

test('the excerpt collapses whitespace and stops near 120 characters', () => {
  expect(queryExcerpt('  SELECT\n\t1  ')).toBe('SELECT 1');
  const long = queryExcerpt(`SELECT ${'x'.repeat(300)}`);
  expect(long.length).toBeLessThanOrEqual(121);
  expect(long.endsWith('…')).toBe(true);
});

test('routingHistoryRecorded follows facts.query_trace.has_ledger', () => {
  expect(routingHistoryRecorded({ facts: { query_trace: { has_ledger: true } } })).toBe(true);
  expect(routingHistoryRecorded({ facts: { query_trace: { has_ledger: false } } })).toBe(false);
  expect(routingHistoryRecorded({ facts: {} })).toBe(false);
  expect(routingHistoryRecorded(null)).toBe(false);
});

// #1028: the query routing section is the query explorer every run has, with
// or without schema design: search by text, and narrow by query type and engine.
const typed = [
  { query_id: 'q1', source: { query_text: 'SELECT 1 FROM wp_posts', query_type: 'SELECT', tables_accessed: ['wp_posts'] }, assignment: { assigned_engine: 'dynamodb' } },
  { query_id: 'q2', source: { query_text: 'UPDATE wp_posts SET a = ?', query_type: 'UPDATE', tables_accessed: ['wp_posts'] }, assignment: { assigned_engine: 'aurora_mysql' } },
  { query_id: 'q3', source: { query_text: 'SELECT 2 FROM wp_options', query_type: 'SELECT', tables_accessed: ['wp_options'] }, assignment: { assigned_engine: 'aurora_mysql' } },
  { query_id: 'q4', source: { query_text: 'SELECT 3' }, assignment: null },
];

test('a row carries the collector\'s query type, or null without one', () => {
  const rows = buildQueryRoutingRows(typed, new Map(), labels);
  expect(rows.map(r => r.queryType)).toEqual(['SELECT', 'UPDATE', 'SELECT', null]);
});

// #1536: a statement that names no table carries the stored "unknown" placeholder; the
// row says "No source table", and the text filter matches that wording, not "unknown".
test('a placeholder table reads as no source table in the row and the filter', () => {
  const none = [
    { query_id: 'n1', source: { query_text: 'SET NAMES utf8', tables_accessed: ['unknown'] }, assignment: null },
    { query_id: 'n2', source: { query_text: 'SELECT 1 FROM wp_posts', tables_accessed: ['wp_posts'] }, assignment: null },
  ];
  const rows = buildQueryRoutingRows(none, new Map(), labels);
  expect(rows[0].tables).toEqual(['No source table']);
  expect(rows[1].tables).toEqual(['wp_posts']);
  const ids = (value) => filterQueryRows(rows, { operation: 'and', tokens: [{ value, operator: ':' }] }).map(r => r.queryId);
  expect(ids('no source')).toEqual(['n1']);
  expect(ids('unknown')).toEqual([]);
});

// #2319: the one query table filters over its columns with a property filter.
const wide = [
  { query_id: 'q1', source: { query_text: 'SELECT 1 FROM shop.wp_posts', query_type: 'SELECT', tables_accessed: ['shop.wp_posts'] }, assignment: { assigned_engine: 'dynamodb' } },
  { query_id: 'q2', source: { query_text: 'UPDATE wp_posts SET a = ?', query_type: 'UPDATE', tables_accessed: ['wp_posts', 'wp_options'] }, assignment: { assigned_engine: 'aurora_mysql' } },
  { query_id: 'q3', source: { query_text: 'SELECT 2 FROM wp_options', query_type: 'SELECT', tables_accessed: ['wp_options'] }, assignment: { assigned_engine: 'aurora_mysql' } },
  { query_id: 'q4', source: { query_text: 'SELECT 3' }, assignment: null },
];
const links = queryPatternLinks(
  [
    { engine: 'dynamodb', queryIds: ['q1'], destNames: ['posts_by_id'] },
    { engine: 'dynamodb', queryIds: ['q3'], destNames: ['options'] },
  ],
  [{ engine: 'elasticache', queryIds: ['q3'] }],
);
const kinds = new Map([['q2', { moved: true }]]);
const wideRows = buildQueryRoutingRows(wide, kinds, labels, links);
const ids = (query) => filterQueryRows(wideRows, { operation: 'and', ...query }).map(r => r.queryId);
const tok = (propertyKey, operator, value) => ({ propertyKey, operator, value });

test('a row has table names without the schema prefix, its destinations once and its cache engine', () => {
  expect(wideRows[0].tables).toEqual(['wp_posts']);
  expect(wideRows[0].destinations).toEqual([{ engine: 'dynamodb', name: 'posts_by_id' }]);
  expect(wideRows[2].destinations.map(d => d.name)).toEqual(['options']);
  expect(wideRows[2].cacheEngine).toBe('elasticache');
  expect(wideRows[0].cacheEngine).toBeNull();
  expect(wideRows[3].destinations).toEqual([]);
});

test('no schema design means no destination and no cache engine, and no row is dropped', () => {
  const bare = buildQueryRoutingRows(wide, kinds, labels);
  expect(bare).toHaveLength(4);
  expect(bare.every(r => r.destinations.length === 0 && r.cacheEngine === null)).toBe(true);
});

test('property tokens filter by engine, operation, table, destination, cached and routing', () => {
  expect(ids({ tokens: [tok('engine', '=', 'aurora_mysql')] })).toEqual(['q2', 'q3']);
  expect(ids({ tokens: [tok('engine', '!=', 'aurora_mysql')] })).toEqual(['q1', 'q4']);
  expect(ids({ tokens: [tok('operation', '=', 'SELECT')] })).toEqual(['q1', 'q3']);
  expect(ids({ tokens: [tok('table', '=', 'wp_options')] })).toEqual(['q2', 'q3']);
  expect(ids({ tokens: [tok('table', ':', 'post')] })).toEqual(['q1', 'q2']);
  expect(ids({ tokens: [tok('table', '!:', 'post')] })).toEqual(['q3', 'q4']);
  expect(ids({ tokens: [tok('destination', '=', 'options')] })).toEqual(['q3']);
  expect(ids({ tokens: [tok('cached', '=', 'yes')] })).toEqual(['q3']);
  expect(ids({ tokens: [tok('cached', '=', 'no')] })).toEqual(['q1', 'q2', 'q4']);
  expect(ids({ tokens: [tok('routing', '=', 'yes')] })).toEqual(['q2']);
});

test('tokens combine with and, or, and free text matches the id, the statement and the names', () => {
  const two = [tok('engine', '=', 'aurora_mysql'), tok('operation', '=', 'SELECT')];
  expect(ids({ tokens: two })).toEqual(['q3']);
  expect(ids({ tokens: two, operation: 'or' })).toEqual(['q1', 'q2', 'q3']);
  expect(ids({ tokens: [{ operator: ':', value: 'UPDATE' }] })).toEqual(['q2']);
  expect(ids({ tokens: [{ operator: ':', value: 'aurora' }] })).toEqual(['q2', 'q3']);
  expect(ids({ tokens: [{ operator: '!:', value: 'wp_posts' }] })).toEqual(['q3', 'q4']);
  expect(ids({ tokens: [] })).toEqual(['q1', 'q2', 'q3', 'q4']);
});

test('the filter options are the distinct values the rows hold, sorted, engines by name', () => {
  const opts = queryFilterOptions(wideRows);
  expect(opts.filter(o => o.propertyKey === 'engine')).toEqual([
    { propertyKey: 'engine', value: 'aurora_mysql', label: 'Aurora MySQL' },
    { propertyKey: 'engine', value: 'dynamodb', label: 'DynamoDB' },
  ]);
  expect(opts.filter(o => o.propertyKey === 'operation').map(o => o.value)).toEqual(['SELECT', 'UPDATE']);
  expect(opts.filter(o => o.propertyKey === 'table').map(o => o.value)).toEqual(['wp_options', 'wp_posts']);
  expect(queryFilterOptions([])).toEqual([]);
});

test('the excerpt stops at a word, never inside a token, and the statement keeps the whole text', () => {
  const sql = `SELECT ${'column_name, '.repeat(20)}FROM some_table`;
  const cut = queryExcerpt(sql);
  expect(cut.endsWith('…')).toBe(true);
  expect(cut.slice(0, -1)).toMatch(/column_name,$|column_name$/);
  expect(flattenSql(sql).endsWith('FROM some_table')).toBe(true);
});

// #2446: the access pattern explorer's operation type filter is a property of the query
// table: a query's types are its patterns' operation types, else its statement's.
describe('operation types', () => {
  const typed = [
    { query_id: 't1', source: { query_text: 'SELECT 1', query_type: 'SELECT', tables_accessed: ['wp_posts'] }, assignment: { assigned_engine: 'opensearch' } },
    { query_id: 't2', source: { query_text: 'SELECT 2', query_type: 'SELECT', tables_accessed: ['wp_posts'] }, assignment: { assigned_engine: 'dynamodb' } },
    { query_id: 't3', source: { query_text: 'DELETE FROM t', query_type: 'DELETE', tables_accessed: ['t'] }, assignment: { assigned_engine: 'aurora_mysql' } },
    { query_id: 't4', source: { query_text: 'x', tables_accessed: [] }, assignment: null },
  ];
  const typedLinks = queryPatternLinks(
    [
      { engine: 'opensearch', opCategory: 'search', queryIds: ['t1'], destNames: [] },
      { engine: 'dynamodb', opCategory: 'read', queryIds: ['t2'], destNames: [] },
    ],
    [{ engine: 'elasticache', opCategory: 'write', queryIds: ['t2'] }],
  );
  const rows = buildQueryRoutingRows(typed, new Map(), labels, typedLinks);
  const typeIds = (value, operator = '=') => filterQueryRows(rows, { operation: 'and', tokens: [{ propertyKey: 'opType', operator, value }] }).map(r => r.queryId);

  test('a query takes the types of the patterns that serve it, in the filter\'s order', () => {
    expect(rows.map(r => r.opTypes)).toEqual([['search'], ['read', 'write'], ['delete'], []]);
  });

  test('without a pattern a query has its statement\'s type, and without one none', () => {
    const bare = buildQueryRoutingRows(typed, new Map(), labels);
    expect(bare.map(r => r.opTypes)).toEqual([['read'], ['read'], ['delete'], []]);
  });

  // #2480 review: a MERGE writes, and OTHER (SHOW, SET SESSION) is no read.
  test('without a pattern a MERGE is a write and OTHER is other, never read', () => {
    const stmt = (id, type) => ({ query_id: id, source: { query_text: 'x', query_type: type, tables_accessed: [] }, assignment: null });
    const bare = buildQueryRoutingRows(
      [stmt('m', 'MERGE'), stmt('o', 'OTHER'), stmt('s', 'SHOW'), stmt('i', 'INSERT'), stmt('u', 'UPDATE')],
      new Map(), labels,
    );
    expect(bare.map(r => r.opTypes)).toEqual([['write'], ['other'], ['other'], ['write'], ['update']]);
    const ofType = value => filterQueryRows(bare, { operation: 'and', tokens: [{ propertyKey: 'opType', operator: '=', value }] }).map(r => r.queryId);
    expect(ofType('other')).toEqual(['o', 's']);
    expect(ofType('read')).toEqual([]);
  });

  test('the operation type token filters, and its negation keeps the rest', () => {
    expect(typeIds('search')).toEqual(['t1']);
    expect(typeIds('read')).toEqual(['t2']);
    expect(typeIds('write')).toEqual(['t2']);
    expect(typeIds('read', '!=')).toEqual(['t1', 't3', 't4']);
  });
});
