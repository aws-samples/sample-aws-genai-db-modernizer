/**
 * #296 cache overlay: ElastiCache is a cache layer, never an owner. These tests
 * cover the helpers that keep owner distributions/rankings from ever counting
 * it as a workload share, and the "cached by <engine>" per-query indicator.
 */
import i18next from 'i18next';
import {
  isCacheEngine,
  getCacheOverlay,
  ownerDistribution,
  splitRankingByRole,
  getQueryCacheInfo,
  formatCacheLayerLine,
  formatCachedByLine,
  targetEngineEntries,
  resolveCostBreakdown,
  isKeptCostEngine,
} from '../cacheLayer';
import en from '../../locales/en.json';

const i18n = i18next.createInstance();

beforeAll(() => i18n.init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
  keySeparator: false,
}));

const t = (key, options) => i18n.t(key, options);

describe('isCacheEngine', () => {
  test('elasticache is a cache engine, everything else is not', () => {
    expect(isCacheEngine('elasticache')).toBe(true);
    expect(isCacheEngine('dynamodb')).toBe(false);
    expect(isCacheEngine(undefined)).toBe(false);
  });
});

describe('getCacheOverlay', () => {
  test('reads cache_overlay off an artifact, null when absent', () => {
    expect(getCacheOverlay({ cache_overlay: { engine: 'elasticache' } })).toEqual({ engine: 'elasticache' });
    expect(getCacheOverlay({})).toBeNull();
    expect(getCacheOverlay(null)).toBeNull();
  });
});

describe('ownerDistribution', () => {
  const dist = { dynamodb: 69, elasticache: 34, opensearch: 4 };

  test('legacy artifact (no overlay): elasticache stays a real owner', () => {
    expect(ownerDistribution(dist, false)).toEqual(dist);
  });

  test('overlay present: elasticache is stripped out of the owner distribution', () => {
    expect(ownerDistribution(dist, true)).toEqual({ dynamodb: 69, opensearch: 4 });
  });

  test('handles a missing/undefined distribution', () => {
    expect(ownerDistribution(undefined, true)).toEqual({});
    expect(ownerDistribution(undefined, false)).toEqual({});
  });
});

describe('splitRankingByRole', () => {
  test('pulls the cache_layer entry out, keeps owner order', () => {
    const ranking = [
      { target: 'dynamodb', role: 'owner', workload_percent: 70 },
      { target: 'elasticache', role: 'cache_layer', workload_percent: 0, assigned_queries: 0, cache_overlay_queries: 20 },
      { target: 'opensearch', role: 'owner', workload_percent: 30 },
    ];
    const { owners, cacheLayer } = splitRankingByRole(ranking);
    expect(owners).toEqual([ranking[0], ranking[2]]);
    expect(cacheLayer).toEqual(ranking[1]);
  });

  test('legacy ranking with no role field: everything is an owner, no cache layer', () => {
    const ranking = [{ target: 'dynamodb' }, { target: 'elasticache' }];
    const { owners, cacheLayer } = splitRankingByRole(ranking);
    expect(owners).toEqual(ranking);
    expect(cacheLayer).toBeNull();
  });

  test('handles a missing/undefined ranking array', () => {
    expect(splitRankingByRole(undefined)).toEqual({ owners: [], cacheLayer: null });
  });
});

describe('getQueryCacheInfo', () => {
  test('returns engine/pattern/reason for a cached query', () => {
    const q = {
      query_id: 'q1',
      assigned_engine: 'dynamodb',
      cache_engine: 'elasticache',
      cache_pattern: 'point_lookup',
      cache_reason: 'Hot key read, >50 calls/sec',
    };
    expect(getQueryCacheInfo(q)).toEqual({
      engine: 'elasticache',
      pattern: 'point_lookup',
      reason: 'Hot key read, >50 calls/sec',
    });
  });

  test('returns null when the query has no cache_engine (not cached, or legacy artifact)', () => {
    expect(getQueryCacheInfo({ query_id: 'q1', assigned_engine: 'dynamodb' })).toBeNull();
    expect(getQueryCacheInfo(null)).toBeNull();
  });
});

describe('formatCacheLayerLine', () => {
  const overlay = { engine: 'elasticache', query_count: 20, calls_per_second: 120.4, call_share_percent: 83.4 };

  test('matches the spec example exactly: "Cache layer · 20 cached reads · 83.4% of calls"', () => {
    expect(formatCacheLayerLine(overlay, { t })).toBe('Cache layer · 20 cached reads · 83.4% of calls');
  });

  test('accepts a role: "cache_layer" ranking entry (the report results card)', () => {
    const entry = {
      target: 'elasticache', role: 'cache_layer', cache_overlay_queries: 20, cache_call_share_percent: 83.4,
    };
    expect(formatCacheLayerLine(entry, { t, withLabel: false })).toBe('20 cached reads · 83.4% of calls');
  });

  test('withLabel: false omits the leading "Cache layer" label (for use next to an explicit badge)', () => {
    expect(formatCacheLayerLine(overlay, { t, withLabel: false })).toBe('20 cached reads · 83.4% of calls');
  });

  test('pluralises a single cached read', () => {
    expect(formatCacheLayerLine({ ...overlay, query_count: 1 }, { t })).toBe('Cache layer · 1 cached read · 83.4% of calls');
  });

  test('omits the call-share fragment when call_share_percent is not a finite number', () => {
    expect(formatCacheLayerLine({ engine: 'elasticache', query_count: 5 }, { t })).toBe('Cache layer · 5 cached reads');
  });

  test('null overlay (no cache fields) yields null, not a dangling "Cache layer" line', () => {
    expect(formatCacheLayerLine(null, { t })).toBeNull();
    expect(formatCacheLayerLine(undefined, { t })).toBeNull();
  });

  test('falls back to sane English when no translation function is supplied', () => {
    expect(formatCacheLayerLine(overlay)).toBe('Cache layer · 20 cached reads · 83.4% of calls');
  });
});

describe('formatCachedByLine', () => {
  test('renders the lowercase "cached by <engine>" indicator', () => {
    expect(formatCachedByLine('ElastiCache', { t })).toBe('cached by ElastiCache');
  });

  test('falls back to sane English when no translation function is supplied', () => {
    expect(formatCachedByLine('ElastiCache')).toBe('cached by ElastiCache');
  });
});

describe('buildOverrideList (#296 cache toggle)', () => {
  const { buildOverrideList } = require('../cacheLayer');

  test('engine changes and cache toggles merge per query', () => {
    expect(buildOverrideList({ q1: 'aurora_mysql' }, { q1: true, q2: false })).toEqual([
      { query_id: 'q1', assigned_engine: 'aurora_mysql', cached: true },
      { query_id: 'q2', cached: false },
    ]);
  });

  test('a cache engine is never sent as an owner', () => {
    expect(buildOverrideList({ q1: 'elasticache' }, {})).toEqual([{ query_id: 'q1', cached: true }]);
  });

  test('nothing pending, nothing sent', () => {
    expect(buildOverrideList({}, {})).toEqual([]);
  });
});

// #358: Results page fixture shaped like a real synthesis report.json
// (wordpress job e6a0127b) -- after_distribution has three owners, the cache
// overlay fronts a slice of their reads, and tco_analysis.cost_breakdown has
// a fourth, elasticache, entry plus a projected_monthly_cost that already
// includes it.
const REPORT_FIXTURE = {
  reality_check: {
    after_distribution: { dynamodb: 82, aurora_mysql: 21, opensearch: 4 },
  },
  cache_overlay: {
    engine: 'elasticache',
    query_count: 14,
    calls_per_second: 158.85,
    call_share_percent: 71.5,
  },
  tco_analysis: {
    projected_monthly_cost: 823.72,
    cost_breakdown: [
      { database: 'dynamodb', monthly_cost_usd: 98.41, pricing_mode: 'on-demand' },
      { database: 'elasticache', monthly_cost_usd: 165.55, pricing_mode: 'on-demand' },
      { database: 'opensearch', monthly_cost_usd: 240.96, pricing_mode: 'on-demand' },
      { database: 'aurora_mysql', monthly_cost_usd: 318.8, pricing_mode: 'on-demand' },
    ],
  },
};

describe('targetEngineEntries (#358 Results page "Target engines")', () => {
  test('lists the owners from after_distribution, then the cache layer last, flagged distinctly', () => {
    const afterDist = ownerDistribution(
      REPORT_FIXTURE.reality_check.after_distribution,
      !!getCacheOverlay(REPORT_FIXTURE)
    );
    expect(targetEngineEntries(afterDist, getCacheOverlay(REPORT_FIXTURE))).toEqual([
      { engine: 'dynamodb', isCacheLayer: false },
      { engine: 'aurora_mysql', isCacheLayer: false },
      { engine: 'opensearch', isCacheLayer: false },
      { engine: 'elasticache', isCacheLayer: true },
    ]);
  });

  test('no cache overlay: owners only, nothing flagged as a cache layer', () => {
    const afterDist = { dynamodb: 82, aurora_mysql: 21 };
    expect(targetEngineEntries(afterDist, null)).toEqual([
      { engine: 'dynamodb', isCacheLayer: false },
      { engine: 'aurora_mysql', isCacheLayer: false },
    ]);
  });

  test('legacy artifact where elasticache is still a real owner: not duplicated as a cache layer', () => {
    const afterDist = { dynamodb: 69, elasticache: 34 };
    expect(targetEngineEntries(afterDist, null)).toEqual([
      { engine: 'dynamodb', isCacheLayer: false },
      { engine: 'elasticache', isCacheLayer: false },
    ]);
  });

  test('handles a missing/undefined distribution', () => {
    expect(targetEngineEntries(undefined, null)).toEqual([]);
    expect(targetEngineEntries(undefined, { engine: 'elasticache' })).toEqual([
      { engine: 'elasticache', isCacheLayer: true },
    ]);
  });
});

describe('isKeptCostEngine (#358, shared with the standalone export\'s mirrored predicate)', () => {
  test('an owner engine is kept', () => {
    expect(isKeptCostEngine('dynamodb', { dynamodb: 82 }, 'elasticache')).toBe(true);
  });

  test('the cache engine is kept even though it owns nothing', () => {
    expect(isKeptCostEngine('elasticache', { dynamodb: 82 }, 'elasticache')).toBe(true);
  });

  test('neither an owner nor the cache engine is dropped', () => {
    expect(isKeptCostEngine('documentdb', { dynamodb: 82 }, 'elasticache')).toBe(false);
  });

  test('no cache engine (null): only owners are kept', () => {
    expect(isKeptCostEngine('elasticache', { dynamodb: 82 }, null)).toBe(false);
  });
});

describe('resolveCostBreakdown (#358 Results page "Cost breakdown" + "Projected cost")', () => {
  test('keeps the cache layer cost card even though it owns no workload share', () => {
    const afterDist = { dynamodb: 82, aurora_mysql: 21, opensearch: 4 };
    const { items } = resolveCostBreakdown(
      REPORT_FIXTURE.tco_analysis,
      afterDist,
      REPORT_FIXTURE.cache_overlay
    );
    expect(items.map(cb => cb.database).sort()).toEqual(
      ['aurora_mysql', 'dynamodb', 'elasticache', 'opensearch']
    );
  });

  test('the total is the report\'s own projected_monthly_cost, not a sum recomputed from a filtered subset', () => {
    const afterDist = { dynamodb: 82, aurora_mysql: 21, opensearch: 4 };
    const { total } = resolveCostBreakdown(
      REPORT_FIXTURE.tco_analysis,
      afterDist,
      REPORT_FIXTURE.cache_overlay
    );
    expect(total).toBe(823.72);
    // Matches the decision report / chat total: DynamoDB + Aurora MySQL + OpenSearch + ElastiCache.
    expect(total).toBeCloseTo(98.41 + 318.8 + 240.96 + 165.55, 2);
  });

  test('cache engine in cache_overlay but absent from cost_breakdown: no card, total is still the report\'s', () => {
    const tco = {
      projected_monthly_cost: 823.72,
      cost_breakdown: [
        { database: 'dynamodb', monthly_cost_usd: 98.41 },
        { database: 'aurora_mysql', monthly_cost_usd: 318.8 },
        { database: 'opensearch', monthly_cost_usd: 240.96 },
        // no elasticache entry here, even though cache_overlay names it.
      ],
    };
    const afterDist = { dynamodb: 82, aurora_mysql: 21, opensearch: 4 };
    const { items, total } = resolveCostBreakdown(tco, afterDist, { engine: 'elasticache' });
    expect(items.map(cb => cb.database)).toEqual(['dynamodb', 'aurora_mysql', 'opensearch']);
    expect(total).toBe(823.72);
  });

  test('drops cost cards for engines that are neither an owner nor the cache layer', () => {
    const tco = {
      projected_monthly_cost: 100,
      cost_breakdown: [
        { database: 'dynamodb', monthly_cost_usd: 60 },
        { database: 'documentdb', monthly_cost_usd: 40 },
      ],
    };
    const { items } = resolveCostBreakdown(tco, { dynamodb: 10 }, null);
    expect(items.map(cb => cb.database)).toEqual(['dynamodb']);
  });

  test('falls back to summing the kept items when projected_monthly_cost is missing (legacy artifact)', () => {
    const tco = {
      cost_breakdown: [
        { database: 'dynamodb', monthly_cost_usd: 60 },
        { database: 'aurora_mysql', monthly_cost_usd: 40 },
      ],
    };
    const { total } = resolveCostBreakdown(tco, { dynamodb: 10, aurora_mysql: 5 }, null);
    expect(total).toBe(100);
  });

  test('the fallback sum only counts finite numbers (PR #244 review): a string/NaN cost never turns it into string concatenation', () => {
    const tco = {
      cost_breakdown: [
        { database: 'dynamodb', monthly_cost_usd: 2.5 },
        { database: 'x', monthly_cost_usd: '5' },
        { database: 'y', monthly_cost_usd: '<img src=x onerror=alert(1)>' },
        { database: 'z', monthly_cost_usd: Number.NaN },
        { database: 'w', monthly_cost_usd: 1.25 },
      ],
    };
    const afterDist = { dynamodb: 1, x: 1, y: 1, z: 1, w: 1 };
    const { total } = resolveCostBreakdown(tco, afterDist, null);
    expect(total).toBe(3.75);
  });

  test('handles a missing/undefined tco_analysis', () => {
    expect(resolveCostBreakdown(undefined, {}, null)).toEqual({ items: [], total: 0 });
  });
});
