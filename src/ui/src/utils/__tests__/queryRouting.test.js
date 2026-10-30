// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #822: the results page's query routing table lists every query of the
 * assessment (the query-journeys list), so a query's decision trace opens
 * without schema design. Rows only format what the API and report.json's
 * facts already hold: the engine from the journey, and why a query's routing
 * is listed from `facts.query_trace.listed_queries` (never recomputed here).
 */
import { buildQueryRoutingRows, filterQueryRoutingRows, queryExcerpt, queryRoutingFilterOptions } from '../queryRouting';
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

test('filtering by text matches the id, the statement, a table or the engine, case-insensitively', () => {
  const rows = buildQueryRoutingRows(journeys, routingKindsById(synthesis), labels);
  expect(filterQueryRoutingRows(rows, 'aurora', 'all').map((r) => r.queryId)).toEqual(['b'.repeat(64)]);
  expect(filterQueryRoutingRows(rows, 'WP_POSTS', 'all').map((r) => r.queryId)).toEqual(['a'.repeat(64)]);
  expect(filterQueryRoutingRows(rows, 'cccc', 'all').map((r) => r.queryId)).toEqual(['c'.repeat(64)]);
  expect(filterQueryRoutingRows(rows, '  ', 'all')).toHaveLength(3);
});

test('the routing-story mode keeps only the queries the facts list', () => {
  const rows = buildQueryRoutingRows(journeys, routingKindsById(synthesis), labels);
  expect(filterQueryRoutingRows(rows, '', 'listed').map((r) => r.queryId)).toEqual(['b'.repeat(64)]);
  expect(filterQueryRoutingRows(rows, 'posts', 'listed')).toEqual([]);
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

test('the query type and engine filters narrow the rows, together and with the text', () => {
  const rows = buildQueryRoutingRows(typed, new Map(), labels);
  const ids = (...args) => filterQueryRoutingRows(rows, ...args).map(r => r.queryId);
  expect(ids('', 'all', { queryType: 'SELECT' })).toEqual(['q1', 'q3']);
  expect(ids('', 'all', { engine: 'aurora_mysql' })).toEqual(['q2', 'q3']);
  expect(ids('', 'all', { queryType: 'SELECT', engine: 'aurora_mysql' })).toEqual(['q3']);
  expect(ids('wp_posts', 'all', { engine: 'aurora_mysql' })).toEqual(['q2']);
  expect(ids('update', 'all')).toEqual(['q2']);
  expect(ids('', 'all', {})).toEqual(['q1', 'q2', 'q3', 'q4']);
  expect(ids('', 'all', { queryType: null, engine: null })).toEqual(['q1', 'q2', 'q3', 'q4']);
});

test('the filter options are the distinct query types and engines the rows hold, sorted', () => {
  const rows = buildQueryRoutingRows(typed, new Map(), labels);
  expect(queryRoutingFilterOptions(rows)).toEqual({
    queryTypes: ['SELECT', 'UPDATE'],
    engines: [{ engine: 'aurora_mysql', engineName: 'Aurora MySQL' }, { engine: 'dynamodb', engineName: 'DynamoDB' }],
  });
  expect(queryRoutingFilterOptions([])).toEqual({ queryTypes: [], engines: [] });
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
  const ids = (text) => filterQueryRoutingRows(rows, text, 'all').map(r => r.queryId);
  expect(ids('no source')).toEqual(['n1']);
  expect(ids('unknown')).toEqual([]);
});
