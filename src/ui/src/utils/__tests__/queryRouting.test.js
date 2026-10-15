/**
 * #822: the results page's query routing table lists every query of the
 * assessment (the query-journeys list), so a query's decision trace opens
 * without schema design. Rows only format what the API and report.json's
 * facts already hold: the engine from the journey, and why a query's routing
 * is listed from `facts.query_trace.listed_queries` (never recomputed here).
 */
import { buildQueryRoutingRows, filterQueryRoutingRows, queryExcerpt } from '../queryRouting';
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
