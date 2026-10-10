/**
 * Aurora design section (#478): the Results page, the HTML export and the
 * engineering report all show DynamoDB/DocumentDB/ElastiCache/OpenSearch in
 * full detail -- tables, indexes, access patterns -- but Aurora only shows up
 * in the trade-offs tab, because the access pattern explorer reads each
 * design's `access_patterns`, and Aurora's schema-design contract has no such
 * field (it carries over tables 1:1 instead; #157 adds real access patterns
 * later).
 *
 * This module shapes what Aurora *does* already have, with no contract
 * change: `table_definitions` (columns, primary key, indexes, foreign keys),
 * `generated_ddl`, `optimizations` and `migration_strategy` from the schema
 * design, plus "queries on Aurora" grouped by source table -- built from
 * `synthesis.query_groups`, not a separate fetch. The relational branch added
 * to `build_query_groups` (`src/agents/referee/synthesis_report.py`) already
 * puts every query the assignment routed to an Aurora engine into that same
 * `query_groups` list the UI already fetches as part of `/results`, so this
 * is pure data-shaping, no new API call.
 */

import { displayEngine } from './engineNames';

// Mirrors AURORA_ENGINES in src/agents/referee/synthesis_report.py, plus the
// legacy bare "aurora" key a few fixtures still use.
export const AURORA_ENGINE_KEYS = ['aurora_mysql', 'aurora_postgresql', 'aurora'];

export const isAuroraEngine = (engine) => AURORA_ENGINE_KEYS.includes(engine);

// Mirrors UTILITY_GROUP_LABEL/UNRESOLVED_TABLE_GROUP_LABEL in
// src/agents/referee/synthesis_report.py exactly (#478):
// neither is a real table a reader should see ranked by throughput like
// the others, so both always sort last here -- the same rule the
// engineering report's "Queries on Aurora by table" subsection applies.
export const AURORA_BLAME_LABELS = [
  'Utility and session statements',
  'Table not identified by the collector',
];

/**
 * One schema design's `table_definitions` entry, shaped for compact display:
 * column name + resolved Aurora type, primary key, indexes and foreign keys
 * (both the latter are raw DDL strings in the contract, kept as-is -- there is
 * nothing more structured to pull out of them).
 */
export function buildAuroraTables(content) {
  const tables = content?.table_definitions || [];
  return tables.map((t) => ({
    tableName: t.table_name,
    columns: (t.columns || []).map((c) => ({
      name: c.name,
      auroraType: c.aurora_type,
      sourceType: c.source_type ?? null,
    })),
    primaryKey: t.primary_key || [],
    indexes: t.indexes || [],
    foreignKeys: t.foreign_keys || [],
  }));
}

/**
 * An access pattern's reason text, wherever it actually lives (#478). The
 * relational branch (`build_query_groups` in
 * `src/agents/referee/synthesis_report.py`) stores `reason_index` into the
 * group's own `reasons` list instead of a copy of the string on every entry
 * -- 146 distinct reasons backed 1281 access patterns on the discourse
 * sample, so copying one onto every one of them was pure duplication. Every
 * other engine's entry still carries the string directly as `description`.
 * Mirrors `reason_text_for` in `synthesis_report.py` and `_reason_text_for`
 * in `src/report/renderers.py` (Python has no JS, so this is a third copy of
 * the same small lookup, not a shared import).
 */
export function reasonTextFor(group, accessPattern) {
  const reasonIndex = accessPattern?.reason_index;
  if (typeof reasonIndex === 'number') {
    const reasons = group?.reasons || [];
    return reasons[reasonIndex] || '';
  }
  return accessPattern?.description || '';
}

/**
 * The access pattern entries `query_groups[].access_patterns` carries for a
 * given engine, keyed by query id -- so a group's `source_queries` (which
 * carry the query text/type but not calls-per-second or the assignment's
 * reason) can be joined back to the per-query figures the relational branch
 * attaches to each pattern (`design_rps`, `reason_index`/`description`).
 */
function patternsByQueryId(group, engine) {
  const byId = {};
  (group.access_patterns || []).forEach((ap) => {
    if (ap?.engine !== engine) return;
    (ap.query_ids || []).forEach((qid) => {
      byId[qid] = ap;
    });
  });
  return byId;
}

/**
 * "Queries on Aurora" grouped by source table (really: by the group_name the
 * relational branch used, which is the sorted, comma-joined source table set
 * -- a join across tables gets one group, not one per table). Each query
 * carries its excerpt, type, calls/s and the assignment's reason, sorted by
 * calls/s so the hottest queries lead.
 */
export function buildAuroraQueryGroups(engine, queryGroups) {
  const groups = [];
  (queryGroups || []).forEach((g) => {
    if (!Array.isArray(g?.engines) || !g.engines.includes(engine)) return;
    const byQueryId = patternsByQueryId(g, engine);
    if (Object.keys(byQueryId).length === 0) return;

    const queries = (g.source_queries || [])
      .filter((sq) => byQueryId[sq.query_id])
      .map((sq) => {
        const ap = byQueryId[sq.query_id];
        return {
          queryId: sq.query_id,
          excerpt: sq.query_text || '',
          queryType: sq.query_type || ap.operation || null,
          callsPerSecond: typeof ap.design_rps === 'number' ? ap.design_rps : 0,
          reason: reasonTextFor(g, ap),
          inScope: ap.in_scope !== false,
        };
      })
      .sort((a, b) => b.callsPerSecond - a.callsPerSecond);

    if (queries.length === 0) return;

    groups.push({
      sourceTable: g.group_name,
      totalCallsPerSecond: queries.reduce((sum, q) => sum + q.callsPerSecond, 0),
      queries,
    });
  });
  // Real tables first, by calls/s descending; the two blame labels always
  // last regardless of their own calls/s (neither is a busiest table, just
  // a bucket for what the collector/utility traffic is -- matching the
  // engineering report's own ordering).
  return groups.sort((a, b) => {
    const aBlame = AURORA_BLAME_LABELS.includes(a.sourceTable);
    const bBlame = AURORA_BLAME_LABELS.includes(b.sourceTable);
    if (aBlame !== bBlame) return aBlame ? 1 : -1;
    return b.totalCallsPerSecond - a.totalCallsPerSecond;
  });
}

/**
 * One Aurora design, ready for the "Aurora design" section: display name,
 * table list, DDL, optimizations, migration strategy and queries-on-Aurora.
 * `design` is a `schemaDesigns[]` entry (`{ target_type, content }`, the
 * shape `/assessments/:jobId/schema-designs` and the HTML export both use)
 * -- `null` when this engine has no schema design yet (the
 * queries still run on Aurora even then, same as the engineering report's
 * "Queries on Aurora by table" subsection, which never required one; only
 * the design-dependent fields below -- tables, DDL, optimizations,
 * migration strategy -- are empty in that case, not the whole section).
 */
export function buildAuroraDesign(engine, design, queryGroups) {
  const content = design?.content || {};
  return {
    engine,
    displayName: displayEngine(engine),
    migrationStrategy: content.migration_strategy || null,
    ddl: content.generated_ddl || '',
    optimizations: content.optimizations || [],
    appLayerNotes: content.app_layer_notes || [],
    tables: buildAuroraTables(content),
    queryGroups: buildAuroraQueryGroups(engine, queryGroups),
  };
}

/**
 * Every Aurora engine present in this assessment, in the shape the "Aurora
 * design" section renders -- empty array when there is no Aurora engine at
 * all, so the section can hide itself with no special-casing. An engine
 * counts as present either because it has a schema design, or because the
 * assignment routed queries to it (`query_groups`: a
 * schema design must not gate visibility -- the UI and export disagreed
 * with the engineering report here before this fix).
 */
export function buildAuroraDesigns(schemaDesigns, queryGroups) {
  const designsByEngine = {};
  (schemaDesigns || []).forEach((d) => {
    if (isAuroraEngine(d?.target_type) && d?.content) {
      designsByEngine[d.target_type] = d;
    }
  });

  const enginesWithQueries = new Set();
  (queryGroups || []).forEach((g) => {
    (g.access_patterns || []).forEach((ap) => {
      if (isAuroraEngine(ap?.engine)) enginesWithQueries.add(ap.engine);
    });
  });

  const engines = new Set([...Object.keys(designsByEngine), ...enginesWithQueries]);
  return [...engines]
    .sort()
    .map((engine) => buildAuroraDesign(engine, designsByEngine[engine] || null, queryGroups));
}
