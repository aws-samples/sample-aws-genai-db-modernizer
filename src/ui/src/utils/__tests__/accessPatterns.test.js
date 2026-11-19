// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2446: the schema designs' access patterns, read once for the results page: each
 * query's patterns for its detail panel, and the source tables for the Data model tab.
 * Nothing here counts a figure; it reads and regroups what the designs hold.
 */
import {
  OPERATION_TYPES, cacheLayerPatterns, operationType, ownerAccessPatterns, patternsByQueryId, sourceTableRows,
  statementType, targetDesignGap,
} from '../accessPatterns';

const design = (engine, content) => ({ target_type: engine, content });
const ddb = design('dynamodb', {
  table_definitions: [{ table_name: 'orders', partition_key: { attribute_name: 'pk', attribute_type: 'S' } }],
  access_patterns: [
    { pattern_id: 'D1', operation: 'GetItem', table_name: 'orders', source_tables: ['shop.orders'], source_query_ids: ['q1'], design_rps: 4 },
    { pattern_id: 'D2', operation: 'Query', table_name: 'orders', gsi_name: 'by_user', source_tables: ['shop.order_items'], source_query_ids: ['q2', 'q3'] },
  ],
});
const aurora = design('aurora_mysql', {
  access_patterns: [{ pattern_id: 'A1', operation: 'SELECT', source_tables: ['shop.users', 'shop.orders'], query_ids: ['q4'], index_used: '__seq_scan__' }],
});
const cache = design('elasticache', {
  key_designs: [{ key_pattern: 'order:{id}', ttl_seconds: 60, data_type: 'hash' }],
  access_patterns: [{ pattern_id: 'C1', operation: 'GET', key_pattern: 'order:{id}', source_tables: ['shop.orders'], source_query_ids: ['q1'] }],
});
const overlay = { engine: 'elasticache' };

const owner = ownerAccessPatterns([ddb, aurora, cache], overlay, { A1: 'Joins stay relational' });
const cached = cacheLayerPatterns([ddb, aurora, cache], overlay);

test('the owner patterns leave the cache layer out and keep each design field', () => {
  expect(owner.map(p => p.id)).toEqual(['D1', 'D2', 'A1']);
  const d1 = owner[0];
  expect(d1).toMatchObject({ engine: 'dynamodb', operation: 'GetItem', opCategory: 'read', destTable: 'orders', sourceTables: ['orders'], queryIds: ['q1'], designRps: 4 });
  expect(d1.destDef.partition_key.attribute_name).toBe('pk');
  expect(owner[1].gsiName).toBe('by_user');
});

test('a designed Aurora pattern reads its routing reason and shows a confirmed scan in words', () => {
  const a1 = owner[2];
  expect(a1.description).toBe('Joins stay relational');
  expect(a1.indexUsed).toBe('None (sequential scan)');
});

test('a legacy report with no cache overlay keeps ElastiCache as an owner and lists no cache layer', () => {
  expect(ownerAccessPatterns([ddb, cache], null).map(p => p.engine)).toEqual(['dynamodb', 'dynamodb', 'elasticache']);
  expect(cacheLayerPatterns([ddb, cache], null)).toEqual([]);
});

test('the cache layer patterns carry their key design and type', () => {
  expect(cached).toHaveLength(1);
  expect(cached[0]).toMatchObject({ isCacheLayer: true, destTable: 'order:{id}', ttlSeconds: 60, keyDataType: 'hash', opCategory: 'read' });
});

test('each query lists its owner patterns, then its cache key designs', () => {
  const byId = patternsByQueryId(owner, cached);
  expect(byId.get('q1').map(p => p.id)).toEqual(['D1', 'C1']);
  expect(byId.get('q3').map(p => p.id)).toEqual(['D2']);
  expect(byId.get('q9')).toBeUndefined();
  expect(patternsByQueryId([], []).size).toBe(0);
});

test('operation types cover target operations and source statements', () => {
  expect(['GetItem', 'SEARCH', 'insertOne', 'UpdateItem', 'DEL'].map(operationType)).toEqual(['read', 'search', 'write', 'update', 'delete']);
  expect(['SELECT', 'INSERT', 'UPDATE', 'DELETE'].map(operationType)).toEqual(['read', 'write', 'update', 'delete']);
});

// #2480 review: a source statement's type, when no pattern serves its query. The
// collector's QueryType values are SELECT, INSERT, UPDATE, DELETE, MERGE and OTHER
// (src/contracts/collector_output.py; tests/unit/report/test_statement_types_js_sync.py
// keeps this list in step). A MERGE or an upsert writes; anything else is "other".
describe('statement types', () => {
  test('every type the collector emits has its own operation type', () => {
    expect(['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'OTHER'].map(statementType))
      .toEqual(['read', 'write', 'update', 'delete', 'write', 'other']);
  });

  test('MERGE, UPSERT and REPLACE are writes, in any case', () => {
    expect(['MERGE', 'merge', 'UPSERT', 'REPLACE'].map(statementType)).toEqual(['write', 'write', 'write', 'write']);
  });

  test('a statement type nobody recognises is other, never read', () => {
    expect(['OTHER', 'SHOW', 'SET SESSION', 'SET', 'CALL', 'VACUUM', 'GET DIAGNOSTICS'].map(statementType))
      .toEqual(['other', 'other', 'other', 'other', 'other', 'other', 'other']);
  });

  test('no statement type has no operation type', () => {
    expect([undefined, null, ''].map(statementType)).toEqual([null, null, null]);
  });

  test('other is a filter value, listed last', () => {
    expect(OPERATION_TYPES[OPERATION_TYPES.length - 1]).toBe('other');
  });
});

// #2480 review: why a query's Target design tab is empty, said as it is.
describe('an empty target design', () => {
  const skipped = { dynamodb: { status: 'skipped', reason: 'llm_mode=none: every schema designer needs a model' } };

  test('a skipped design says so, with its reason when the report carries one', () => {
    expect(targetDesignGap('dynamodb', skipped, 'available'))
      .toEqual({ state: 'skipped', engine: 'dynamodb', reason: 'llm_mode=none: every schema designer needs a model' });
    expect(targetDesignGap('dynamodb', { dynamodb: { status: 'skipped' } }, 'available'))
      .toEqual({ state: 'skipped', engine: 'dynamodb', reason: null });
  });

  test('a skip outranks design files that could not be read', () => {
    expect(targetDesignGap('dynamodb', skipped, 'unavailable').state).toBe('skipped');
  });

  test('design files the page cannot read, or only in part, say so', () => {
    expect(targetDesignGap('dynamodb', {}, 'unavailable')).toEqual({ state: 'unavailable' });
    expect(targetDesignGap('dynamodb', { dynamodb: { status: 'completed' } }, 'partial')).toEqual({ state: 'partial' });
  });

  test('a read design that lists no pattern for the query says that', () => {
    expect(targetDesignGap('dynamodb', { dynamodb: { status: 'completed' } }, 'available')).toEqual({ state: 'none' });
    expect(targetDesignGap('aurora_mysql', skipped, 'available')).toEqual({ state: 'none' });
    expect(targetDesignGap(null, skipped, 'available')).toEqual({ state: 'none' });
    expect(targetDesignGap('dynamodb', undefined, undefined)).toEqual({ state: 'none' });
  });
});

describe('source tables', () => {
  const rows = sourceTableRows(owner, cached);
  const row = name => rows.find(r => r.table === name);

  test('one row per source table, by name, with its engines, destinations and operations', () => {
    expect(rows.map(r => r.table)).toEqual(['order_items', 'orders', 'users']);
    expect(row('orders').engines).toEqual(['dynamodb', 'aurora_mysql', 'elasticache']);
    expect(row('orders').operations).toEqual(['GET', 'GetItem', 'SELECT']);
    expect(row('orders').destinations).toEqual([
      { engine: 'dynamodb', name: 'orders' },
      { engine: 'aurora_mysql', name: 'users' },
      { engine: 'aurora_mysql', name: 'orders' },
      { engine: 'elasticache', name: 'order:{id}' },
    ]);
  });

  test('two source tables on one named destination merge; an Aurora join and a cache key never do', () => {
    expect(row('orders').mergedWith).toEqual(['order_items']);
    expect(row('order_items').mergedWith).toEqual(['orders']);
    expect(row('users').mergedWith).toEqual([]);
  });

  test('a table only the cache layer reads still has its row', () => {
    const only = cacheLayerPatterns([design('elasticache', { access_patterns: [{ pattern_id: 'C9', operation: 'GET', key_pattern: 'k', source_tables: ['shop.sessions'] }] })], overlay);
    expect(sourceTableRows([], only)).toEqual([
      expect.objectContaining({ table: 'sessions', engines: ['elasticache'], mergedWith: [], operations: ['GET'] }),
    ]);
  });

  test('no designs, no rows', () => {
    expect(sourceTableRows([], [])).toEqual([]);
  });
});

// #2455: a design of several schemas keys its tables `schema.table`; two schemas' `orders`
// are two source tables, on every engine, never one row of the bare name.
describe('source tables of a design that spans two schemas', () => {
  const twoSchemas = design('aurora_postgresql', {
    table_definitions: [
      { table_name: 'orders', schema_name: 'sales' },
      { table_name: 'orders', schema_name: 'archive' },
    ],
    access_patterns: [
      { pattern_id: 'P1', operation: 'SELECT', source_tables: ['sales.orders'], query_ids: ['q1'] },
      { pattern_id: 'P2', operation: 'SELECT', source_tables: ['db.archive.orders'], query_ids: ['q2'] },
    ],
  });
  const ddbOrders = design('dynamodb', {
    table_definitions: [{ table_name: 'orders_by_user' }],
    access_patterns: [{ pattern_id: 'D1', operation: 'GetItem', table_name: 'orders_by_user', source_tables: ['sales.orders'], source_query_ids: ['q1'] }],
  });
  const patterns = ownerAccessPatterns([twoSchemas, ddbOrders], null);

  test('each schema keeps its own row, keyed as the facts key it', () => {
    const rows = sourceTableRows(patterns, []);
    expect(rows.map(r => r.table)).toEqual(['archive.orders', 'sales.orders']);
    expect(rows.find(r => r.table === 'archive.orders').patterns.map(p => p.id)).toEqual(['P2']);
    expect(rows.find(r => r.table === 'sales.orders').patterns.map(p => p.id)).toEqual(['P1', 'D1']);
  });

  test('a pattern finds its own schema table definition', () => {
    expect(patterns[0].destDef).toBe(twoSchemas.content.table_definitions[0]);
    expect(patterns[1].destDef).toBe(twoSchemas.content.table_definitions[1]);
  });

  test('a design of one schema keeps the bare names', () => {
    expect(sourceTableRows(ownerAccessPatterns([ddb], null), []).map(r => r.table)).toEqual(['order_items', 'orders']);
  });
});

// #2483: the query table's source table property also matches the tables merged with a
// query's own, into the destination a pattern serves it from: the destination's own
// source_tables and the other patterns on it. Two schemas' `orders` stay apart.
describe('the tables merged into a pattern\'s destination', () => {
  test('a named destination lists its own source tables and every pattern\'s on it', () => {
    const withSources = design('dynamodb', {
      table_definitions: [{ table_name: 'orders', source_tables: ['shop.orders', 'shop.customers'] }],
      access_patterns: [
        { pattern_id: 'D1', operation: 'GetItem', table_name: 'orders', source_tables: ['shop.orders'], source_query_ids: ['q1'] },
        { pattern_id: 'D2', operation: 'Query', table_name: 'orders', source_tables: ['shop.order_items'], source_query_ids: ['q2'] },
      ],
    });
    const [d1, d2] = ownerAccessPatterns([withSources], null);
    expect(d1.mergedSourceTables).toEqual(['customers', 'order_items', 'orders']);
    expect(d2.mergedSourceTables).toEqual(['customers', 'order_items', 'orders']);
  });

  test('an Aurora join and a cache key design merge nothing', () => {
    expect(owner[2].mergedSourceTables).toEqual([]);
    expect(cached[0].mergedSourceTables).toBeUndefined();
  });

  test('a design of several schemas keeps each schema\'s table apart', () => {
    const sales = design('aurora_postgresql', {
      table_definitions: [{ table_name: 'orders', schema_name: 'sales' }, { table_name: 'orders', schema_name: 'archive' }],
      access_patterns: [],
    });
    const merged = design('dynamodb', {
      table_definitions: [{ table_name: 'orders_by_user', source_tables: ['sales.orders', 'sales.users'] }],
      access_patterns: [{ pattern_id: 'D1', operation: 'GetItem', table_name: 'orders_by_user', source_tables: ['sales.orders'], source_query_ids: ['q1'] }],
    });
    const [d1] = ownerAccessPatterns([sales, merged], null);
    expect(d1.mergedSourceTables).toEqual(['sales.orders', 'sales.users']);
  });
});

// #2478: a source table name finds its design table as the facts key it
// (table_keys.DesignTableKeys): a bare name one schema has, or a name the source database
// prefixes, reads as that schema's table; a bare name two schemas share, or a schema the
// design lacks, reads as no design table.
describe('source table names in a design of two schemas', () => {
  const sales = { table_name: 'orders', schema_name: 'sales' };
  const archive = { table_name: 'orders', schema_name: 'archive' };
  const customers = { table_name: 'customers', schema_name: 'sales' };
  const twoSchemas = design('aurora_postgresql', {
    source_database: 'shop',
    table_definitions: [sales, archive, customers],
    access_patterns: [
      { pattern_id: 'P1', operation: 'SELECT', source_tables: ['customers'], query_ids: ['q1'] },
      { pattern_id: 'P2', operation: 'SELECT', source_tables: ['shop.customers'], query_ids: ['q2'] },
      { pattern_id: 'P3', operation: 'SELECT', source_tables: ['orders'], query_ids: ['q3'] },
      { pattern_id: 'P4', operation: 'SELECT', source_tables: ['crm.customers'], query_ids: ['q4'] },
      { pattern_id: 'P5', operation: 'SELECT', source_tables: ['Sales.Orders'], query_ids: ['q5'] },
    ],
  });
  const byId = Object.fromEntries(ownerAccessPatterns([twoSchemas], null).map(p => [p.id, p]));

  test('a bare or source-database name one schema has reads as that schema table', () => {
    expect(byId.P1.sourceTables).toEqual(['sales.customers']);
    expect(byId.P1.destDef).toBe(customers);
    expect(byId.P2.sourceTables).toEqual(['sales.customers']);
    expect(byId.P2.destDef).toBe(customers);
    expect(byId.P5.destDef).toBe(sales);
  });

  test('a bare name two schemas share, or another schema, finds no design table', () => {
    expect(byId.P3.sourceTables).toEqual(['orders']);
    expect(byId.P3.destDef).toBeNull();
    expect(byId.P4.sourceTables).toEqual(['crm.customers']);
    expect(byId.P4.destDef).toBeNull();
  });

  test('the source tables keep one row per design table', () => {
    const rows = sourceTableRows(ownerAccessPatterns([twoSchemas], null), []);
    expect(rows.map(r => r.table)).toEqual(['crm.customers', 'orders', 'sales.customers', 'sales.orders']);
    expect(rows.find(r => r.table === 'sales.customers').patterns.map(p => p.id)).toEqual(['P1', 'P2']);
  });

  test('a design saved before #2455 (no schema names) keeps the bare names', () => {
    const legacy = design('aurora_postgresql', {
      table_definitions: [{ table_name: 'orders' }, { table_name: 'orders' }],
      access_patterns: [{ pattern_id: 'L1', operation: 'SELECT', source_tables: ['sales.orders'], query_ids: ['q1'] }],
    });
    const [p] = ownerAccessPatterns([legacy], null);
    expect(p.sourceTables).toEqual(['orders']);
    expect(p.destDef).toBe(legacy.content.table_definitions[1]);
  });
});

// #2478 review: a design of `public` plus `pg_catalog` is one of one schema, so `audit` is
// keyed bare. A name of the system schema it left out (its `system_schema` note) keeps its
// schema, as DesignTableKeys.row_key does, so it never finds the user table `audit`.
describe('source table names of a left-out system schema', () => {
  const audit = { table_name: 'audit' };
  const oneSchema = design('aurora_postgresql', {
    source_database: 'shop',
    table_definitions: [audit, { table_name: 'users' }],
    app_layer_notes: [{ feature: 'system_schema', source_object: 'pg_catalog.audit', recommendation: 'x' }],
    access_patterns: [
      { pattern_id: 'P1', operation: 'SELECT', source_tables: ['public.audit'], query_ids: ['q1'] },
      { pattern_id: 'P2', operation: 'SELECT', source_tables: ['pg_catalog.audit'], query_ids: ['q2'] },
      { pattern_id: 'P3', operation: 'SELECT', source_tables: ['"PG_CATALOG"."audit"'], query_ids: ['q3'] },
    ],
  });
  const byId = Object.fromEntries(ownerAccessPatterns([oneSchema], null).map(p => [p.id, p]));

  test('a system schema table keeps its own key and finds no design table', () => {
    expect(byId.P2.sourceTables).toEqual(['pg_catalog.audit']);
    expect(byId.P2.destDef).toBeNull();
    expect(byId.P3.sourceTables).toEqual(['pg_catalog.audit']);
    expect(byId.P3.destDef).toBeNull();
  });

  test('the user table still reads as its bare name', () => {
    expect(byId.P1.sourceTables).toEqual(['audit']);
    expect(byId.P1.destDef).toBe(audit);
  });

  test('a design with no system_schema note keeps shortTable', () => {
    const plain = design('aurora_postgresql', {
      table_definitions: [audit],
      access_patterns: [{ pattern_id: 'L1', operation: 'SELECT', source_tables: ['pg_catalog.audit'], query_ids: ['q1'] }],
    });
    expect(ownerAccessPatterns([plain], null)[0].sourceTables).toEqual(['audit']);
  });
});

// #2478 review: a three-part name whose middle part is the source database names that
// schema, as DesignTableKeys.row_key reads it: `shop.shop.orders` is `shop.orders`.
test('a three-part name keeps its schema even when it is the source database name', () => {
  const twoSchemas = design('aurora_postgresql', {
    source_database: 'shop',
    table_definitions: [{ table_name: 'orders', schema_name: 'sales' }, { table_name: 'orders', schema_name: 'archive' }],
    access_patterns: [{ pattern_id: 'P1', operation: 'SELECT', source_tables: ['shop.shop.orders'], query_ids: ['q1'] }],
  });
  const [p] = ownerAccessPatterns([twoSchemas], null);
  expect(p.sourceTables).toEqual(['shop.orders']);
  expect(p.destDef).toBeNull();
});
