/**
 * Cache overlay helpers (#296 "cache overlay" design).
 *
 * ElastiCache is a cache layer, never a system of record: it no longer owns any
 * query. Each query keeps its owner engine (`assigned_engine`); a hot read may
 * additionally carry `cache_engine: "elasticache"` plus `cache_pattern` and
 * `cache_reason`. The assignment artifact and the synthesis report both carry an
 * optional top-level `cache_overlay` describing the overlay as a whole
 * (`{ engine, query_count, calls_per_second, call_share_percent, owners,
 * patterns, ... }`), and the synthesis report's `ranking[]` carries a
 * `role: "cache_layer"` entry for it, sorted after every owner engine.
 *
 * Every page that shows an owner distribution, a Sankey, or a ranking must stop
 * counting the cache engine as if it owned workload share -- no "0%" or "N% of
 * workload" for it -- and instead show it as a cache layer with its overlay
 * query count and share of calls.
 *
 * Backward compatible: artifacts produced before the overlay existed carry
 * elasticache as a normal owner and no `cache_overlay`/`cache_engine`/`role`
 * fields at all. Every helper below is a no-op for that shape -- distributions
 * and rankings pass straight through unless a cache_overlay signals the new
 * shape is in play.
 */

export const CACHE_ENGINES = new Set(['elasticache']);

export const isCacheEngine = (engine) => CACHE_ENGINES.has(engine);

/** Pull the optional `cache_overlay` off an assignment or synthesis artifact. */
export function getCacheOverlay(source) {
  return source?.cache_overlay || null;
}

/**
 * Remove cache engines from an owner distribution (e.g. before/after_distribution,
 * or any `{ engine: count }` map). Only filters when `hasOverlay` is true, so a
 * legacy artifact with no cache_overlay (elasticache still a real owner) renders
 * exactly as before.
 */
export function ownerDistribution(distribution, hasOverlay) {
  const dist = distribution || {};
  if (!hasOverlay) return dist;
  const result = {};
  Object.entries(dist).forEach(([engine, count]) => {
    if (!isCacheEngine(engine)) result[engine] = count;
  });
  return result;
}

/**
 * Schema designs for owner engines only, dropping the cache layer's own
 * design (#361: the access pattern explorer's unified pattern list -- and the
 * total/engine pie it drives -- must not count ElastiCache's own cached-read
 * patterns as if they were owned workload, the same invariant
 * `ownerDistribution` enforces for a `{ engine: count }` map).
 *
 * Only filters when `hasOverlay` is true (#405), mirroring `ownerDistribution`:
 * a legacy report has no `cache_overlay` at all, so ElastiCache is still a real
 * owner there and its schema design's access patterns are owned workload, not a
 * cache layer's. Without this guard, a legacy report would disagree with
 * src/report/analysis_report.py, which only strips a design when
 * `cache_overlay.engine` names it.
 */
export function ownerSchemaDesigns(designs, hasOverlay) {
  const list = designs || [];
  if (!hasOverlay) return list;
  return list.filter((d) => !isCacheEngine(d?.target_type));
}

/**
 * Count of the cache layer's own schema-design access patterns across a list
 * of `{ target_type, content: { access_patterns } }` schema designs (#361):
 * real design artifacts, but never owned query workload, so callers can
 * surface the count on its own (e.g. "+13 cache-layer patterns") instead of
 * silently dropping it or folding it into an owner-only total.
 *
 * Only counts when `hasOverlay` is true (#405): a legacy report with no
 * `cache_overlay` never carries a separate cache-layer count -- ElastiCache's
 * design there is owned workload (see `ownerSchemaDesigns` above), already
 * included in the owner total, so this must return 0 rather than double-count
 * it as both owned and "+N cache-layer patterns".
 */
export function cacheAccessPatternCount(designs, hasOverlay) {
  if (!hasOverlay) return 0;
  return (designs || [])
    .filter((d) => isCacheEngine(d?.target_type))
    .reduce((sum, d) => sum + (d?.content?.access_patterns?.length || 0), 0);
}

/**
 * Split a synthesis report's `ranking[]` into owner rows and the cache-layer row
 * (role === "cache_layer"), if present. Owners keep their relative order; the
 * cache layer entry (always sorted after owners by the backend) is pulled out so
 * callers can render it as a cache layer card instead of a ranked owner.
 */
export function splitRankingByRole(ranking) {
  const owners = [];
  let cacheLayer = null;
  (ranking || []).forEach((item) => {
    if (item && item.role === 'cache_layer') {
      cacheLayer = item;
    } else {
      owners.push(item);
    }
  });
  return { owners, cacheLayer };
}

/**
 * Target-engine entries for the Results page header (#358): the owners from a
 * (post-`ownerDistribution`) distribution, in order, followed by the cache
 * layer -- if a `cache_overlay` is present and its engine isn't already an
 * owner -- flagged with `isCacheLayer: true` so the caller can render it
 * distinctly (e.g. "ElastiCache (cache layer)") instead of silently leaving
 * it out of "Target engines" the way a plain owner-only list would.
 *
 * Legacy artifacts with no cache_overlay (elasticache still a real owner)
 * pass straight through: `cacheOverlay` is null, so nothing is appended, and
 * the distribution it came from was never stripped in the first place.
 */
export function targetEngineEntries(afterDist, cacheOverlay) {
  const entries = Object.keys(afterDist || {}).map((engine) => ({ engine, isCacheLayer: false }));
  const cacheEngine = cacheOverlay?.engine;
  if (cacheEngine && !entries.some((e) => e.engine === cacheEngine)) {
    entries.push({ engine: cacheEngine, isCacheLayer: true });
  }
  return entries;
}

/**
 * Whether a `tco_analysis.cost_breakdown` entry's database should keep a cost
 * card: it's either a workload owner (present in `afterDist`) or the
 * cache_overlay's engine. Pulled out of `resolveCostBreakdown` (#358) so the
 * standalone "Export to HTML" script -- which has no bundler at run time and
 * so can't import this module -- can mirror the exact same expression inline
 * (see `src/ui/src/utils/ExportReport.js`'s `KEEP_COST_CARD_EXPR`) and a test
 * can assert the two agree.
 */
export function isKeptCostEngine(database, afterDist, cacheEngine) {
  return afterDist?.[database] != null || database === cacheEngine;
}

/**
 * Cost breakdown + projected total for the Results page (#358). Filtering
 * `tco_analysis.cost_breakdown` down to "engines that own workload share"
 * (the same filter the Sankey/target-engine list uses) silently drops the
 * cache layer's own cost card and, worse, understates the headline total --
 * the cache engine's cost is real infrastructure spend even though it owns
 * no queries.
 *
 * This keeps a cost card for every owner plus the cache_overlay engine (if
 * any), and always reports the artifact's own `projected_monthly_cost` as the
 * total -- never a sum recomputed from a filtered subset -- so the figure
 * matches the decision report and chat (one source of truth in report.json).
 * Falls back to summing the kept items only when `projected_monthly_cost` is
 * missing (older artifacts predating that field) -- summing only finite
 * numbers, so a non-numeric `monthly_cost_usd` (a string, NaN, hostile text)
 * can't turn the fallback sum into string concatenation (PR #244's shell
 * guard, applied here too since this helper now owns the sum).
 */
export function resolveCostBreakdown(tcoAnalysis, afterDist, cacheOverlay) {
  const all = tcoAnalysis?.cost_breakdown || [];
  const cacheEngine = cacheOverlay?.engine;
  const items = all.filter((cb) => isKeptCostEngine(cb.database, afterDist, cacheEngine));
  const reported = tcoAnalysis?.projected_monthly_cost;
  const finiteCost = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const total = typeof reported === 'number' && Number.isFinite(reported)
    ? reported
    : items.reduce((sum, cb) => sum + finiteCost(cb.monthly_cost_usd), 0);
  return { items, total };
}

/**
 * Per-query cache badge info for the Assignment Gate's query list: null when the
 * query isn't cached, otherwise the cache engine, pattern and reason to show as
 * a small "cached by <engine>" indicator next to the query's owner engine.
 */
export function getQueryCacheInfo(queryAssignment) {
  if (!queryAssignment?.cache_engine) return null;
  return {
    engine: queryAssignment.cache_engine,
    pattern: queryAssignment.cache_pattern || null,
    reason: queryAssignment.cache_reason || null,
  };
}

/**
 * Add the cache layer to a Sankey `{ nodes, links }` graph as a node fed by
 * its owner engines' flows (#330), so ElastiCache becomes visible in the
 * diagram itself instead of only in a caption below it. This is a *second*
 * layer of links -- `ownerEngine -> cacheEngine` -- on top of the existing
 * `source -> ownerEngine` flows; it never touches those, so owner node totals
 * (and the existing links) still sum to the total query count. The cache
 * engine is still never a flow source, preserving the #296 invariant that it
 * never owns a query.
 *
 * No-op (returns `sankeyData` unchanged) when there's no overlay, the overlay
 * has no `owners` breakdown, or none of its owner engines are present as
 * nodes in `sankeyData` (so a partial/legacy sankey is left alone).
 */
export function addCacheOverlayNode(sankeyData, overlay) {
  if (!sankeyData || !overlay?.engine || !overlay?.owners) return sankeyData;

  const nodeIds = new Set((sankeyData.nodes || []).map((node) => node.id));
  const cacheLinks = Object.entries(overlay.owners)
    .filter(([engine, count]) => nodeIds.has(engine) && count > 0)
    .map(([engine, count]) => ({ source: engine, target: overlay.engine, value: count }));

  if (cacheLinks.length === 0) return sankeyData;

  return {
    nodes: [...sankeyData.nodes, { id: overlay.engine }],
    links: [...sankeyData.links, ...cacheLinks],
  };
}

/**
 * Cache-layer access-pattern rows for the explorer (#429 part 2, follow-up to
 * #361/#405): `ownerSchemaDesigns`/`allAccessPatterns` correctly drop the
 * cache design's own `access_patterns` from the owner total and pie, but the
 * Results page used to drop them from the explorer entirely -- the cache
 * layer's key designs were real deliverables with nowhere to browse them.
 * This builds that design's own rows, each tagged `isCacheLayer: true` so a
 * caller renders them as a distinct "<Name> (cache layer)" group instead of
 * mixing them into the owner rows, with the matching key design (by
 * `key_pattern`) joined in so its TTL and data type travel with the row.
 *
 * No-op (returns []) without `cacheOverlay.engine` (same hasOverlay guard as
 * `ownerSchemaDesigns`/`cacheAccessPatternCount`): a legacy report has no
 * overlay at all, so ElastiCache's design is already included in the owner
 * rows there -- this must not duplicate it.
 */
export function cacheLayerAccessPatterns(designs, cacheOverlay) {
  const cacheEngine = cacheOverlay?.engine;
  if (!cacheEngine) return [];
  const design = (designs || []).find((d) => d?.target_type === cacheEngine);
  if (!design) return [];

  const content = design.content || {};
  const keyDesignByPattern = {};
  (content.key_designs || []).forEach((kd) => {
    if (kd?.key_pattern) keyDesignByPattern[kd.key_pattern] = kd;
  });

  return (content.access_patterns || []).map((ap, idx) => {
    const keyPattern = ap.key_pattern || null;
    const keyDesign = keyPattern ? keyDesignByPattern[keyPattern] : null;
    return {
      id: ap.pattern_id || ap.name || `${cacheEngine}-${idx}`,
      engine: cacheEngine,
      isCacheLayer: true,
      operation: ap.operation || ap.http_method || '—',
      sourceTables: (ap.source_tables || []).map((t) => t.split('.').pop()),
      sourceTablesRaw: ap.source_tables || [],
      destTable: keyPattern || '—',
      keyPattern,
      ttlSeconds: typeof keyDesign?.ttl_seconds === 'number' ? keyDesign.ttl_seconds : null,
      keyDataType: keyDesign?.data_type || null,
      gsiName: null,
      patternGroup: null,
      description: ap.description || ap.name || '',
      queryIds: ap.source_query_ids || ap.query_ids || [],
    };
  });
}

// Fallback used when no i18next `t` is supplied (mirrors defaultT in
// assignmentSummary.js so this module stays safe to call standalone).
function defaultT(key, options = {}) {
  const { defaultValue, ...vars } = options;
  let text = defaultValue !== undefined ? defaultValue : key;
  Object.entries(vars).forEach(([name, value]) => {
    text = text.split(`{{${name}}}`).join(value);
  });
  return text;
}

/**
 * Format a cache_overlay as the compact stat line used everywhere the cache
 * layer needs a label: "Cache layer · 20 cached reads · 83.4% of calls".
 * Accepts a `cache_overlay` object or a `role: "cache_layer"` ranking entry
 * (`cache_overlay_queries` / `cache_call_share_percent`).
 * Returns null when there is no overlay to describe.
 */
export function formatCacheLayerLine(overlay, { t = defaultT, withLabel = true } = {}) {
  if (!overlay) return null;

  const count = overlay.query_count ?? overlay.cache_overlay_queries ?? 0;
  const reads = t('cache-layer.cached-reads', {
    count,
    defaultValue: count === 1 ? '{{count}} cached read' : '{{count}} cached reads',
  });

  const parts = withLabel ? [t('cache-layer.badge-label', { defaultValue: 'Cache layer' })] : [];
  parts.push(reads);

  const share = overlay.call_share_percent ?? overlay.cache_call_share_percent;
  if (typeof share === 'number' && Number.isFinite(share)) {
    parts.push(t('cache-layer.call-share-percent', {
      percent: share.toFixed(1),
      defaultValue: '{{percent}}% of calls',
    }));
  }

  // A cache-layer ranking entry carries its cache fit: the mean fit of the reads
  // it fronts (#152)
  const fit = overlay.routed_confidence;
  if (typeof fit === 'number' && Number.isFinite(fit)) {
    parts.push(t('cache-layer.cache-fit', {
      percent: fit.toFixed(0),
      defaultValue: '{{percent}}% cache fit',
    }));
  }

  return parts.join(' · ');
}

/** "cached by ElastiCache" indicator text for a single cached query row. */
export function formatCachedByLine(engineLabelText, { t = defaultT } = {}) {
  return t('assignment-gate.table.cached-by', {
    engine: engineLabelText,
    defaultValue: 'cached by {{engine}}',
  });
}

/**
 * The PUT /assignments override list from the gate's two pending-edit maps
 * (#296): `engineOverrides` { query_id: owner engine } and `cacheOverrides`
 * { query_id: true|false } ("Cache with ElastiCache" toggle). A cache engine is
 * never sent as an owner: it would only be converted to a cache pin server-side.
 */
export function buildOverrideList(engineOverrides = {}, cacheOverrides = {}) {
  const byId = {};
  Object.entries(engineOverrides || {}).forEach(([queryId, engine]) => {
    if (isCacheEngine(engine)) {
      byId[queryId] = { ...(byId[queryId] || { query_id: queryId }), cached: true };
    } else {
      byId[queryId] = { ...(byId[queryId] || { query_id: queryId }), assigned_engine: engine };
    }
  });
  Object.entries(cacheOverrides || {}).forEach(([queryId, cached]) => {
    byId[queryId] = { ...(byId[queryId] || { query_id: queryId }), cached: !!cached };
  });
  return Object.values(byId);
}
