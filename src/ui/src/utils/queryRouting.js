// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { displayTables } from './tableNames';
import { OPERATION_TYPES, statementType } from './accessPatterns';
// #822: the rows of the results page's query routing table, one per query of
// the assessment (GET /assessments/{job_id}/query-journeys), so every query's
// journey and "Why this engine" trace opens without schema design. Rows only
// format what the API and the facts layer hold (routingKindsById in
// reportFacts.js); nothing here decides or recomputes a query's routing.

const EXCERPT_CHARS = 120;

// The statement with its whitespace collapsed to single spaces.
export function flattenSql(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

// The start of a query's SQL, whitespace collapsed, about 120 characters (the
// same length the engineering report's trace appendix prints). It stops at a
// word boundary when there is one, so the cut never lands inside a token (#2319).
export function queryExcerpt(text) {
  const flat = flattenSql(text);
  if (flat.length <= EXCERPT_CHARS) return flat;
  const space = flat.slice(0, EXCERPT_CHARS + 1).lastIndexOf(' ');
  const cut = space > EXCERPT_CHARS / 2 ? space : EXCERPT_CHARS;
  return `${flat.slice(0, cut).trimEnd()}…`;
}

// #2319: which destination tables serve a query and which cache engine fronts
// it, read from the schema design's access patterns (each lists the query ids it
// serves). Pure membership: nothing is counted or scored. A run without schema
// design has no patterns, so both maps are empty and the columns read as a dash.
// #2446: also each query's operation types, from the operations of the patterns
// that serve it (an OpenSearch search, a DynamoDB GetItem, a cache GET).
export function queryPatternLinks(ownerPatterns, cachePatterns) {
  const opTypesById = new Map();
  [...(ownerPatterns || []), ...(cachePatterns || [])].forEach((p) => {
    if (!p.opCategory) return;
    (p.queryIds || []).forEach((qid) => {
      const set = opTypesById.get(qid) || new Set();
      set.add(p.opCategory);
      opTypesById.set(qid, set);
    });
  });
  const destinationsById = new Map();
  (ownerPatterns || []).forEach((p) => {
    (p.queryIds || []).forEach((qid) => {
      const list = destinationsById.get(qid) || [];
      (p.destNames || []).forEach((name) => list.push({ engine: p.engine, name }));
      destinationsById.set(qid, list);
    });
  });
  const cacheEngineById = new Map();
  (cachePatterns || []).forEach((p) => {
    (p.queryIds || []).forEach((qid) => cacheEngineById.set(qid, p.engine));
  });
  // #2483: the values the explorer's pattern group, Uses GSI, target operation and
  // source table filters read, gathered over every pattern that serves the query (the
  // fields its Target design tab shows). Set membership only: nothing is counted.
  const designById = new Map();
  [...(ownerPatterns || []), ...(cachePatterns || [])].forEach((p) => {
    (p.queryIds || []).forEach((qid) => {
      const d = designById.get(qid) || {
        patternGroups: new Set(), usesGsi: new Set(), targetOperations: new Set(), sourceTables: new Set(),
      };
      if (p.patternGroup) d.patternGroups.add(p.patternGroup);
      d.usesGsi.add(p.gsiName ? 'yes' : 'no');
      if (p.operation && p.operation !== '—') d.targetOperations.add(p.operation);
      [...(p.sourceTables || []), ...(p.mergedSourceTables || [])].forEach(s => d.sourceTables.add(s));
      designById.set(qid, d);
    });
  });
  return { destinationsById, cacheEngineById, opTypesById, designById };
}

const sorted = set => (set ? [...set].sort() : []);

// A query's operation types (#2446): those of the access patterns that serve it,
// else its own statement's type (SELECT reads, MERGE writes, OTHER is other), in the
// filter's order. A query with no statement type and no pattern has none.
function queryOperationTypes(designed, queryType) {
  const own = statementType(queryType);
  const types = designed && designed.size > 0 ? designed : new Set(own ? [own] : []);
  return OPERATION_TYPES.filter(type => types.has(type));
}

export function buildQueryRoutingRows(journeys, kindsById, engineLabels, links = {}) {
  const destinationsById = links.destinationsById || new Map();
  const cacheEngineById = links.cacheEngineById || new Map();
  const opTypesById = links.opTypesById || new Map();
  const designById = links.designById || new Map();
  return (journeys || []).filter(j => j && j.query_id).map((j) => {
    const engine = (j.assignment && j.assignment.assigned_engine) || null;
    const statement = flattenSql(j.source && j.source.query_text);
    const tables = displayTables(j.source && j.source.tables_accessed);
    const destinations = (destinationsById.get(j.query_id) || [])
      .filter((d, i, all) => all.findIndex(o => o.engine === d.engine && o.name === d.name) === i);
    const engineName = engine ? (engineLabels[engine] || engine) : null;
    const queryType = (j.source && j.source.query_type) || null;
    const design = designById.get(j.query_id) || {};
    return {
      queryId: j.query_id,
      excerpt: queryExcerpt(statement),
      statement,
      tables,
      destinations,
      queryType,
      opTypes: queryOperationTypes(opTypesById.get(j.query_id), queryType),
      engine,
      engineName,
      cacheEngine: cacheEngineById.get(j.query_id) || null,
      patternGroups: sorted(design.patternGroups),
      usesGsi: sorted(design.usesGsi),
      targetOperations: sorted(design.targetOperations),
      sourceTables: sorted(design.sourceTables),
      kinds: kindsById.get(j.query_id) || null,
      // Lower-cased once, so a filter over thousands of rows only compares strings.
      search: [j.query_id, statement, queryType || '', engineName || '', ...tables, ...destinations.map(d => d.name)]
        .join('\n').toLowerCase(),
    };
  });
}

// What a row holds for a filter property, as a list of values (a query reads
// several tables). `cached` and `routing` are yes/no.
function propertyValues(row, key) {
  switch (key) {
    case 'engine': return row.engine ? [row.engine] : [];
    case 'operation': return row.queryType ? [row.queryType] : [];
    case 'opType': return row.opTypes || [];
    case 'table': return row.tables;
    case 'destination': return row.destinations.map(d => d.name);
    case 'cached': return [row.cacheEngine ? 'yes' : 'no'];
    case 'routing': return [row.kinds ? 'yes' : 'no'];
    // #2483: a query's target design values, one per pattern that serves it.
    case 'patternGroup': return row.patternGroups || [];
    case 'usesGsi': return row.usesGsi || [];
    case 'targetOperation': return row.targetOperations || [];
    case 'sourceTable': return row.sourceTables || [];
    // #2318: the close calls table's own properties.
    case 'runnerUp': return row.runnerUp ? [row.runnerUp] : [];
    case 'settledBy': return row.settledBy ? [row.settledBy] : [];
    default: return [];
  }
}

const POSITIVE_OPERATOR = { '=': '=', '!=': '=', ':': ':', '!:': ':' };

function tokenMatches(row, token) {
  const { propertyKey, operator, value } = token;
  const wanted = String(value === undefined || value === null ? '' : value).toLowerCase();
  const negated = operator === '!=' || operator === '!:';
  const values = propertyKey ? propertyValues(row, propertyKey).map(v => String(v).toLowerCase()) : null;
  const exact = POSITIVE_OPERATOR[operator] === '=';
  const hit = values === null
    ? row.search.includes(wanted)
    : values.some(v => (exact ? v === wanted : v.includes(wanted)));
  return negated ? !hit : hit;
}

// A property filter query ({ tokens, operation }) over the rows: every token
// ('and') or any token ('or'). No tokens keeps every row.
export function filterQueryRows(rows, query) {
  const tokens = (query && query.tokens) || [];
  if (tokens.length === 0) return rows;
  const some = query.operation === 'or';
  return rows.filter(r => (some ? tokens.some(t => tokenMatches(r, t)) : tokens.every(t => tokenMatches(r, t))));
}

// The values each property offers in the filter: the distinct values the rows
// hold, sorted, never a count. An engine's value is its id, its label the name.
export function queryFilterOptions(rows) {
  const engines = new Map();
  const sets = {
    operation: new Set(), table: new Set(), destination: new Set(),
    patternGroup: new Set(), targetOperation: new Set(), sourceTable: new Set(),
  };
  (rows || []).forEach((r) => {
    if (r.engine) engines.set(r.engine, r.engineName || r.engine);
    if (r.queryType) sets.operation.add(r.queryType);
    r.tables.forEach(t => sets.table.add(t));
    r.destinations.forEach(d => sets.destination.add(d.name));
    (r.patternGroups || []).forEach(v => sets.patternGroup.add(v));
    (r.targetOperations || []).forEach(v => sets.targetOperation.add(v));
    (r.sourceTables || []).forEach(v => sets.sourceTable.add(v));
  });
  const plain = key => [...sets[key]].sort().map(value => ({ propertyKey: key, value }));
  return [
    ...[...engines.entries()].sort((a, b) => a[1].localeCompare(b[1]))
      .map(([value, label]) => ({ propertyKey: 'engine', value, label })),
    ...plain('operation'),
    ...plain('table'),
    ...plain('destination'),
    ...plain('patternGroup'),
    ...plain('targetOperation'),
    ...plain('sourceTable'),
  ];
}

// The label a query pinned to its engine shows instead of its fit (#1373): the
// assignment's `pinned` ('utility' for a utility or DDL statement), translated;
// null for a query placed by its score. Formats only.
const PINNED_FIT_KEYS = { utility: 'common.fit.pinned-utility' };

export function pinnedFitLabel(pinned, t) {
  const key = pinned ? PINNED_FIT_KEYS[pinned] : null;
  return key ? t(key) : null;
}

// The query journey's "Workload Fit" (#1373): the pin's label for a pinned query,
// else the assignment's `confidence`, the query's score on the engine it is on
// (the server sets it with every engine change, so a consolidated query shows its
// new engine's score). Formatted only, never recomputed; no figure reads as null.
export function journeyWorkloadFit(journey, t = (key) => key) {
  const assignment = journey && journey.assignment;
  const pinned = assignment && pinnedFitLabel(assignment.pinned, t);
  if (pinned) return pinned;
  const confidence = assignment && assignment.confidence;
  return confidence ? `${confidence}%` : null;
}
