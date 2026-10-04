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

  return parts.join(' · ');
}

/** "cached by ElastiCache" indicator text for a single cached query row. */
export function formatCachedByLine(engineLabelText, { t = defaultT } = {}) {
  return t('assignment-gate.table.cached-by', {
    engine: engineLabelText,
    defaultValue: 'cached by {{engine}}',
  });
}
