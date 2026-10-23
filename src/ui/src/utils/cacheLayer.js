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

import { CACHE_ENGINE_IDS } from './engineCatalog.generated';

// The same set as src/shared/cache_engines.py (#398): both come from the engine
// catalog's role = "cache" (src/catalog/engines, #492).
export const CACHE_ENGINES = new Set(CACHE_ENGINE_IDS);

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
 * cache layer's. A design is only stripped when `cache_overlay.engine` names it.
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
 * cache_overlay's engine. Pulled out of `resolveCostBreakdown` (#358) so a
 * test can pin it.
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
 * matches the executive report and chat (one source of truth in report.json).
 * Falls back to `facts.projected_monthly_cost_usd` when
 * `projected_monthly_cost` is missing (older artifacts predating that field):
 * the facts layer sums the same kept engines once (ADR-032, #748), so this
 * helper keeps no sum of its own. A hand-built artifact with neither field has
 * no total (`null`): callers show "n/a" or "—", never a made-up 0.
 */
export function resolveCostBreakdown(tcoAnalysis, afterDist, cacheOverlay, facts) {
  const all = tcoAnalysis?.cost_breakdown || [];
  const cacheEngine = cacheOverlay?.engine;
  const items = all.filter((cb) => isKeptCostEngine(cb.database, afterDist, cacheEngine));
  const reported = tcoAnalysis?.projected_monthly_cost;
  const finite = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const total = finite(reported) ?? finite(facts?.projected_monthly_cost_usd);
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
 * layer needs a label: "Cache layer · 14 designed cached reads · 76.1% of calls", or at the
 * gate (`kind: 'candidates'`) "Cache layer · 20 cache candidates · 83.4% of calls".
 * Accepts a `cache_overlay` object or a `role: "cache_layer"` ranking entry
 * (`cache_overlay_queries` / `cache_call_share_percent`).
 * Returns null when there is no overlay to describe.
 */
export function formatCacheLayerLine(overlay, { t = defaultT, withLabel = true, kind } = {}) {
  if (kind !== 'candidates' && kind !== 'designed') {
    // #1252: every caller says which figure it shows; there is no default to drift to.
    throw new Error(`formatCacheLayerLine needs kind 'candidates' or 'designed', got ${kind}`);
  }
  if (!overlay) return null;

  // #1252: each figure says what it counts. The approval gate shows `candidates`
  // (the hot reads routing put under the cache overlay; once synthesis has rewritten the
  // overlay, the candidate fields it stored); after schema design the figure is `designed`
  // (what the cache engine's design covers).
  const candidates = kind === 'candidates';
  const count = (candidates ? overlay.candidate_query_count : undefined)
    ?? overlay.query_count ?? overlay.cache_overlay_queries ?? 0;
  const reads = candidates
    ? t('cache-layer.candidates', {
      count,
      defaultValue: count === 1 ? '{{count}} cache candidate' : '{{count}} cache candidates',
    })
    : t('cache-layer.cached-reads', {
      count,
      defaultValue: count === 1 ? '{{count}} designed cached read' : '{{count}} designed cached reads',
    });

  const parts = withLabel ? [t('cache-layer.badge-label', { defaultValue: 'Cache layer' })] : [];
  parts.push(reads);

  const share = (candidates ? overlay.candidate_call_share_percent : undefined)
    ?? overlay.call_share_percent ?? overlay.cache_call_share_percent;
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

/**
 * The one sentence that says the cache design covers fewer reads than the gate's candidates
 * (#1252, ADR-032), from `facts.cache_layer` (`cacheLayerFactsOf`): "20 cache candidates,
 * 14 designed; 6 stay on their owner engine." Null when the design covers every candidate.
 * Same wording as `src/shared/cache_change.py`.
 */
export function cacheChangeSentence(cacheFacts, { t = defaultT } = {}) {
  if (!cacheFacts) return null;
  const { candidates, designed } = cacheFacts;
  const stay = candidates - designed;
  if (!(stay > 0)) return null;
  const done = designed === 0
    ? t('cache-layer.none-designed', { defaultValue: 'none designed' })
    : t('cache-layer.n-designed', { count: designed, defaultValue: '{{count}} designed' });
  const stays = designed === 0 && stay > 1
    ? t('cache-layer.all-stay-on-owner', {
      count: stay, defaultValue: 'all {{count}} stay on their owner engine',
    })
    : t('cache-layer.stay-on-owner', {
      count: stay,
      defaultValue: stay === 1 ? 'it stays on its owner engine' : '{{count}} stay on their owner engine',
    });
  const head = t('cache-layer.candidates', {
    count: candidates,
    defaultValue: candidates === 1 ? '{{count}} cache candidate' : '{{count}} cache candidates',
  });
  return `${head}, ${done}; ${stays}.`;
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
