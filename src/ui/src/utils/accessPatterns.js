// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { cacheLayerAccessPatterns, ownerSchemaDesigns } from './cacheLayer';
import { destinationKey, destinationOf } from './destinations';
import { designTableKey, isAuroraEngine } from './auroraDesign';

// The schema designs' access patterns, as the results page reads them (#2446). The
// Queries tab's one query table shows each query's patterns in its detail panel, and
// the Data model tab lists them by source table. Everything here only reads and
// regroups what the designs hold: no figure is counted or scored.

const OP_CATEGORY = {
  GetItem: 'read', Query: 'read', Scan: 'read', BatchGetItem: 'read',
  get_by_id: 'read', search: 'search', aggregate: 'search',
  find: 'read', findOne: 'read', lookup: 'read',
  GET: 'read', SEARCH: 'search', MGET: 'read', HGETALL: 'read',
  PutItem: 'write', BatchWriteItem: 'write',
  insertOne: 'write', insertMany: 'write', bulkWrite: 'write',
  SET: 'write', HSET: 'write', SADD: 'write', ZADD: 'write',
  UpdateItem: 'update', updateOne: 'update', updateMany: 'update',
  DeleteItem: 'delete', deleteOne: 'delete', deleteMany: 'delete',
  DEL: 'delete', HDEL: 'delete',
};

// The operation types, in the order a filter lists them. "other" is a source
// statement no type covers (the collector's OTHER: SHOW, SET SESSION, a utility).
export const OPERATION_TYPES = ['read', 'write', 'update', 'delete', 'search', 'other'];

// A source statement's type (the collector's QueryType, src/contracts/collector_output.py),
// for a query no access pattern serves (#2480 review). A MERGE or an upsert writes.
// tests/unit/report/test_statement_types_js_sync.py checks every QueryType is here.
const STATEMENT_TYPES = {
  SELECT: 'read',
  INSERT: 'write', MERGE: 'write', UPSERT: 'write', REPLACE: 'write',
  UPDATE: 'update',
  DELETE: 'delete',
  OTHER: 'other',
};

// A source statement's operation type: read, write, update, delete, or other for a
// type nobody recognises (never read). No statement type has none.
export function statementType(queryType) {
  const key = String(queryType || '').trim().toUpperCase();
  if (!key) return null;
  return STATEMENT_TYPES[key] || 'other';
}

// An operation's type (read, write, update, delete or search): a target operation
// (GetItem, insertOne, SEARCH) or a source statement's type (SELECT, INSERT).
export function operationType(op) {
  if (OP_CATEGORY[op]) return OP_CATEGORY[op];
  const text = op || '';
  if (/get|read|find|scan|query|select|fetch|lookup/i.test(text)) return 'read';
  if (/put|insert|create|write|set|add/i.test(text)) return 'write';
  if (/search|agg|match|wildcard/i.test(text)) return 'search';
  if (/update|modify|patch/i.test(text)) return 'update';
  if (/delete|remove/i.test(text)) return 'delete';
  return 'read';
}

// Why a query's Target design tab lists nothing (#2480 review), as report.json's
// schema_designs and the page's read of the design files state it: its engine's design
// was skipped (with the stored reason, when there is one), the design files could not
// be read (all of them, or only some), or a read design lists no pattern for it.
export function targetDesignGap(engine, schemaDesigns, availability) {
  const design = engine && schemaDesigns ? schemaDesigns[engine] : null;
  if (design && typeof design === 'object' && design.status === 'skipped') {
    return { state: 'skipped', engine, reason: design.reason ? String(design.reason) : null };
  }
  if (availability === 'unavailable') return { state: 'unavailable' };
  if (availability === 'partial') return { state: 'partial' };
  return { state: 'none' };
}

export const shortTable = (name) => (name ? String(name).split('.').pop() : name);

// A design of several schemas keys each table `schema.table` (#2455, table_keys.py). The
// mapper a design's source table names go through reads a name as the facts key it
// (`DesignTableKeys.row_key`, #2478): a name that spells a schema the design has
// (`sales.orders`, `db.sales.orders`) reads as `schema.table`, so two schemas' `orders` stay
// two source tables; a name of another schema keeps its schema (`crm.orders`), unless the
// prefix of a two-part name is the source database; a bare name reads as the one design
// table of that name, and stays shortTable's when two schemas have it. A name of a system
// schema the design left out (its `system_schema` notes, `left_out_system_schemas`) keeps
// its schema in a design of one schema too, so `pg_catalog.audit` is never the user table
// `audit`. Any other name in a design of one schema is shortTable's.
const folded = (name) => String(name).replace(/[`"[\]]/g, '').trim().toLowerCase();

export function sourceNameMapper(designs) {
  const schemas = new Set();
  const databases = new Set();
  const system = new Set();
  const byBare = new Map();
  (designs || []).forEach((d) => {
    if (d?.content?.source_database) databases.add(folded(d.content.source_database));
    (d?.content?.app_layer_notes || []).forEach((n) => {
      const obj = n?.feature === 'system_schema' ? n.source_object : null;
      if (typeof obj === 'string' && obj.includes('.')) system.add(folded(obj.split('.')[0]));
    });
    (d?.content?.table_definitions || []).forEach((t) => {
      if (!t.schema_name) return;
      schemas.add(folded(t.schema_name));
      const keys = byBare.get(folded(t.table_name)) || new Set();
      keys.add(designTableKey(t.table_name, t.schema_name));
      byBare.set(folded(t.table_name), keys);
    });
  });
  if (schemas.size === 0 && system.size === 0) return shortTable;
  return (name) => {
    if (!name) return name;
    const parts = String(name).replace(/[`"[\]]/g, '').split('.').map(p => p.trim());
    const bare = parts[parts.length - 1];
    if (parts.length >= 2) {
      const schema = parts[parts.length - 2].toLowerCase();
      const database = parts.length === 2 && databases.has(schema);
      if (!schemas.has(schema) && !database && system.has(schema)) return designTableKey(bare, schema);
      if (schemas.size > 0 && (schemas.has(schema) || !database)) return designTableKey(bare, schema);
    }
    if (schemas.size === 0) return shortTable(name);
    const hits = byBare.get(bare.toLowerCase());
    return hits && hits.size === 1 ? [...hits][0] : shortTable(name);
  };
}

// Aurora's '__seq_scan__' sentinel (#157 review, S1) is a script-internal value,
// never a reader-facing one. Three facts collapse onto it, and only one is a claim
// worth showing:
// - the predicate itself is unknown (needs_judgment): hidden;
// - the query's table could not be identified at all (load_testable=false with that
//   reason, #157 review round 2): there was nothing to resolve an index against, so
//   hidden too;
// - the predicate is confirmed empty: a sequential scan is the resolved answer.
export function auroraIndexUsedDisplay(ap) {
  if (ap?.index_used !== '__seq_scan__') return ap?.index_used || null;
  if (ap?.needs_judgment) return null;
  if (ap?.load_testable === false && (ap?.load_testable_reason || '').includes('could be identified')) {
    return null;
  }
  return 'None (sequential scan)';
}

// The owner engines' access patterns, one record per pattern (#361: the cache layer's
// design is left out, cacheLayerPatterns lists it). `auroraReasonByPatternId` holds
// the assignment's reason for a designed Aurora pattern (#157 review round 2): its
// description says why the query is on Aurora at all.
export function ownerAccessPatterns(designs, cacheOverlay, auroraReasonByPatternId = {}) {
  const patterns = [];
  const shorten = sourceNameMapper(designs);
  ownerSchemaDesigns(designs, !!cacheOverlay).forEach((design) => {
    const engine = design.target_type;
    const content = design.content || {};
    const tableLookup = {};
    (content.table_definitions || []).forEach((t) => {
      tableLookup[t.schema_name ? designTableKey(t.table_name, t.schema_name) : t.table_name] = t;
    });
    (content.index_designs || []).forEach((idx) => { tableLookup[idx.index_name] = idx; });
    (content.collection_designs || []).forEach((col) => { tableLookup[col.collection_name || col.name] = col; });

    (content.access_patterns || []).forEach((ap) => {
      // Aurora (#157, #429 part 1) carries each source table over 1:1, so its
      // destination is its source tables.
      const { destName, destNames, named } = destinationOf(ap, shorten);
      const operation = ap.operation || ap.http_method || '—';
      patterns.push({
        id: ap.pattern_id || ap.name || `${engine}-${patterns.length}`,
        engine,
        operation,
        opCategory: operationType(ap.operation || ap.http_method),
        sourceTables: (ap.source_tables || []).map(shorten),
        destTable: destName,
        destNames,
        destNamed: named,
        destDef: tableLookup[destName] || null,
        gsiName: ap.gsi_name || null,
        keyCondition: ap.key_condition || null,
        indexUsed: auroraIndexUsedDisplay(ap),
        description: (isAuroraEngine(engine) && auroraReasonByPatternId[ap.pattern_id])
          || ap.description || ap.name || '',
        patternDescription: ap.description || ap.name || '',
        queryIds: ap.source_query_ids || ap.query_ids || [],
        patternGroup: ap.pattern_group || null,
        designRps: ap.design_rps ?? null,
        itemSize: ap.item_size_bytes ?? null,
        avgItems: ap.avg_items_returned ?? null,
        inScope: ap.in_scope !== false,
        outOfScopeReason: ap.out_of_scope_reason || null,
        opensearchDsl: ap.opensearch_dsl || null,
      });
    });
  });
  return withMergedSourceTables(patterns, shorten);
}

// #2483: the source tables merged into each pattern's destination, for the query table's
// source table property: the destination's own source_tables (the Data model's
// "consolidated tables") and those of every pattern on it, through the same name mapper,
// so two schemas' `orders` stay apart. Like groupBySourceTable, a pattern that names no
// destination (an Aurora join) merges nothing. Listed and sorted, never counted.
function withMergedSourceTables(patterns, shorten) {
  const byDestination = new Map();
  patterns.forEach((p) => {
    if (!p.destNamed) return;
    const key = destinationKey(p.engine, p.destTable);
    const set = byDestination.get(key) || new Set();
    const def = p.destDef || {};
    (def.source_tables || def.source_collections || []).forEach(s => set.add(shorten(s)));
    p.sourceTables.forEach(s => set.add(s));
    byDestination.set(key, set);
  });
  return patterns.map(p => ({
    ...p,
    mergedSourceTables: p.destNamed ? [...byDestination.get(destinationKey(p.engine, p.destTable))].sort() : [],
  }));
}

// The cache layer's access patterns and key designs (#429 part 2), with their type.
export function cacheLayerPatterns(designs, cacheOverlay) {
  const shorten = sourceNameMapper(designs);
  return cacheLayerAccessPatterns(designs, cacheOverlay).map(ap => ({
    ...ap,
    sourceTables: (ap.sourceTablesRaw || []).map(shorten),
    opCategory: operationType(ap.operation),
  }));
}

// Each query's access patterns, by query id: its owner engine's patterns first, then
// the cache layer's key designs. A pattern serving several queries is listed under each.
export function patternsByQueryId(ownerPatterns, cachePatterns) {
  const byId = new Map();
  [...(ownerPatterns || []), ...(cachePatterns || [])].forEach((p) => {
    (p.queryIds || []).forEach((qid) => {
      const list = byId.get(qid) || [];
      if (!list.includes(p)) list.push(p);
      byId.set(qid, list);
    });
  });
  return byId;
}

function groupBySourceTable(patterns) {
  const groups = new Map();
  patterns.forEach((ap) => {
    ap.sourceTables.forEach((table) => {
      if (!groups.has(table)) {
        groups.set(table, { table, engines: [], destinations: [], patterns: [], mergedWith: [] });
      }
      const g = groups.get(table);
      if (!g.engines.includes(ap.engine)) g.engines.push(ap.engine);
      // #2320: one entry per destination of one engine, in first-seen order.
      (ap.destNames || []).forEach((name) => {
        if (!g.destinations.some(d => d.engine === ap.engine && d.name === name)) {
          g.destinations.push({ engine: ap.engine, name });
        }
      });
      g.patterns.push(ap);
    });
  });

  // A destination is an engine and a name: the same name on two engines does not
  // converge. A pattern that names no destination table (Aurora carries each source
  // table over 1:1) is left out: a join that reads two tables does not merge them.
  const sourcesOf = new Map();
  patterns.forEach((ap) => {
    if (!ap.destNamed) return;
    const key = destinationKey(ap.engine, ap.destTable);
    const set = sourcesOf.get(key) || new Set();
    ap.sourceTables.forEach(s => set.add(s));
    sourcesOf.set(key, set);
  });
  groups.forEach((g) => {
    const merged = new Set();
    g.patterns.forEach((ap) => {
      if (!ap.destNamed) return;
      (sourcesOf.get(destinationKey(ap.engine, ap.destTable)) || new Set()).forEach((s) => {
        if (s !== g.table) merged.add(s);
      });
    });
    g.mergedWith = [...merged].sort();
  });
  return groups;
}

// The Data model tab's source tables (#2446): one row per source table an access
// pattern reads, sorted by name, with its engines, destination tables, the tables it
// merges with into one destination, its operations and its patterns. The cache
// layer's key designs join their source table's row; they never merge tables, so the
// owner patterns alone decide what merges.
export function sourceTableRows(ownerPatterns, cachePatterns) {
  const owner = groupBySourceTable(ownerPatterns || []);
  const cache = groupBySourceTable(cachePatterns || []);
  cache.forEach((c, table) => {
    const g = owner.get(table);
    if (!g) {
      owner.set(table, { ...c, mergedWith: [] });
      return;
    }
    c.engines.forEach(e => { if (!g.engines.includes(e)) g.engines.push(e); });
    c.destinations.forEach((d) => {
      if (!g.destinations.some(o => o.engine === d.engine && o.name === d.name)) g.destinations.push(d);
    });
    g.patterns.push(...c.patterns);
  });
  return [...owner.values()]
    .map(g => ({
      ...g,
      operations: [...new Set(g.patterns.map(p => p.operation))].sort(),
    }))
    .sort((a, b) => a.table.localeCompare(b.table));
}
