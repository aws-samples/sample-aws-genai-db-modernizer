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
