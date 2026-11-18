// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2325: the data behind the Table design's table of tables. Each table carries the
 * statements the design stores for it (never a re-split of the script), the query count the
 * facts state and the query rows those ids name; the list filters on the table name.
 */
import { withTableDetails, filterAuroraTables, buildAuroraDesign, buildAuroraTables, tableKey, designTableKey } from '../auroraDesign';
import { tableQueryRowsOf } from '../reportFacts';

const table = (tableName, extra = {}) => ({ tableName, columns: [], primaryKey: [], indexes: [], foreignKeys: [], ddlStatements: null, ...extra });
const q = (queryId, callsPerSecond = 1) => ({ queryId, excerpt: 'SELECT 1', queryType: 'SELECT', callsPerSecond, reason: '' });
const groups = [{ sourceTable: 'wp_posts', queries: [q('a', 1), q('b', 5)] }, { sourceTable: 'wp_posts, wp_users', queries: [q('c', 2)] }];

test('the count is the fact, and the queries are the fact\'s ids joined to the page\'s rows, busiest first', () => {
  const rows = [
    { engine: 'aurora_mysql', table: 'wp_posts', query_count: 3, query_ids: ['a', 'b', 'c'] },
    { engine: 'aurora_mysql', table: 'wp_users', query_count: 1, query_ids: ['c'] },
  ];
  const [posts, users, other] = withTableDetails([table('wp_posts'), table('WP_Users'), table('wp_other')], rows, groups, '');
  expect(posts.queryCount).toBe(3);
  expect(posts.queries.map(x => x.queryId)).toEqual(['b', 'c', 'a']);
  expect(users.queryCount).toBe(1);
  expect(users.queries.map(x => x.queryId)).toEqual(['c']);
  expect(other.queryCount).toBe(0);
  expect(other.queries).toEqual([]);
});

test('the figure never follows a group name: a group named for two tables is not split', () => {
  const [posts] = withTableDetails([table('wp_posts')], [], groups, '');
  expect(posts.queryCount).toBe(0);
  expect(posts.queries).toEqual([]);
});

test('a row\'s key is its position, so two tables with one name stay apart', () => {
  const rows = withTableDetails([table('orders', { ddlStatements: ['A'] }), table('orders', { ddlStatements: ['B'] })], [], [], '');
  expect(rows.map(r => r.key)).toEqual(['0', '1']);
  expect(rows.map(r => r.ddlStatements)).toEqual([['A'], ['B']]);
});

test('table keys read like the facts: no quotes, no schema, lower case', () => {
  expect(tableKey('Shop.`WP_Posts`')).toBe('wp_posts');
  expect(tableKey('"Order Details"')).toBe('order details');
});

test('a design carries the statements it stores and none it does not', () => {
  const built = buildAuroraTables({ table_definitions: [{ table_name: 'a', ddl_statements: ['CREATE TABLE a (id int);', '-- note'] }, { table_name: 'b' }] });
  expect(built[0].ddlStatements).toEqual(['CREATE TABLE a (id int);', '-- note']);
  expect(built[1].ddlStatements).toBeNull();
});

test('the whole script rides along as the fallback for a design with no stored statements', () => {
  const design = { target_type: 'aurora_mysql', content: { generated_ddl: 'CREATE TABLE a (id int);', table_definitions: [{ table_name: 'a' }] } };
  const built = buildAuroraDesign('aurora_mysql', design, [], [{ engine: 'aurora_mysql', table: 'a', query_count: 0, query_ids: [] }]);
  expect(built.tables[0].fallbackDdl).toBe('CREATE TABLE a (id int);');
  expect(built.tables[0].ddlStatements).toBeNull();
});

test('only the engine\'s own rows apply', () => {
  const rows = [{ engine: 'aurora_postgresql', table: 'a', query_count: 9, query_ids: ['x'] }];
  const built = buildAuroraDesign('aurora_mysql', { content: { table_definitions: [{ table_name: 'a' }] } }, [], rows);
  expect(built.tables[0].queryCount).toBe(0);
});

test('the filter keeps the tables whose name contains the text, ignoring case', () => {
  const tables = [table('wp_posts'), table('wp_users'), table('Topics')];
  expect(filterAuroraTables(tables, 'USER').map(x => x.tableName)).toEqual(['wp_users']);
  expect(filterAuroraTables(tables, 'op').map(x => x.tableName)).toEqual(['Topics']);
  expect(filterAuroraTables(tables, '  ')).toBe(tables);
  expect(filterAuroraTables(tables, 'zzz')).toEqual([]);
});

test('the facts reader returns the rows, and [] without them', () => {
  const rows = [{ engine: 'aurora_mysql', table: 'a', query_count: 1, query_ids: ['x'] }];
  expect(tableQueryRowsOf({ facts: { query_group_counts: { table_queries: rows } } })).toBe(rows);
  expect(tableQueryRowsOf({ facts: { query_group_counts: {} } })).toEqual([]);
  expect(tableQueryRowsOf(null)).toEqual([]);
});

// #2455: two schemas' tables of one name are two tables, each with its own queries.
const designOf = (defs) => ({ content: { table_definitions: defs } });

test('a table of a design of several schemas shows and keys as schema.table', () => {
  const built = buildAuroraTables({ table_definitions: [{ table_name: 'orders', schema_name: 'Sales' }, { table_name: 'orders' }] });
  expect(built.map(t => [t.tableName, t.schemaName])).toEqual([['Sales.orders', 'Sales'], ['orders', null]]);
  expect(designTableKey('Orders', 'Sales')).toBe('sales.orders');
  expect(designTableKey('Shop.`Orders`', null)).toBe('orders');
});

test('two schemas\' tables of one name each read their own row', () => {
  const rows = [
    { engine: 'aurora_postgresql', table: 'sales.orders', query_count: 2, query_ids: ['a', 'b'] },
    { engine: 'aurora_postgresql', table: 'archive.orders', query_count: 1, query_ids: ['c'] },
    { engine: 'aurora_postgresql', table: 'orders', query_count: 9, query_ids: ['a', 'b', 'c'] },
  ];
  const built = buildAuroraDesign('aurora_postgresql', designOf([
    { table_name: 'orders', schema_name: 'sales' }, { table_name: 'orders', schema_name: 'archive' },
  ]), [], rows);
  expect(built.tables.map(t => [t.tableName, t.queryCount])).toEqual([['sales.orders', 2], ['archive.orders', 1]]);
});

test('a report written before #2455 lends its bare row only to a table no other schema shares', () => {
  const rows = [
    { engine: 'aurora_postgresql', table: 'orders', query_count: 3, query_ids: ['a', 'b', 'c'] },
    { engine: 'aurora_postgresql', table: 'customers', query_count: 1, query_ids: ['d'] },
  ];
  const built = buildAuroraDesign('aurora_postgresql', designOf([
    { table_name: 'orders', schema_name: 'sales' }, { table_name: 'orders', schema_name: 'archive' },
    { table_name: 'customers', schema_name: 'sales' },
  ]), [], rows);
  expect(built.tables.map(t => [t.tableName, t.queryCount])).toEqual([['sales.orders', 0], ['archive.orders', 0], ['sales.customers', 1]]);
});

test('a design saved before #2455 reads its bare rows as before', () => {
  const rows = [{ engine: 'aurora_postgresql', table: 'orders', query_count: 3, query_ids: ['a', 'b', 'c'] }];
  const built = buildAuroraDesign('aurora_postgresql', designOf([{ table_name: 'orders' }, { table_name: 'orders' }]), [], rows);
  expect(built.tables.map(t => [t.tableName, t.queryCount])).toEqual([['orders', 3], ['orders', 3]]);
});

test('the filter finds a table by its schema', () => {
  const tables = buildAuroraTables({ table_definitions: [{ table_name: 'orders', schema_name: 'sales' }, { table_name: 'orders', schema_name: 'archive' }] });
  expect(filterAuroraTables(tables, 'archive.').map(x => x.tableName)).toEqual(['archive.orders']);
});
