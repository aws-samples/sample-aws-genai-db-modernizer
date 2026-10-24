/**
 * #957: the results page's wave plan prints each wave's headline exactly as the
 * decision and engineering reports do (src/report/roadmap.py's
 * _wave_headline_stats), from report.json's waves and facts, with no figure of
 * its own. The expected headlines come from the shared fixture that
 * tests/unit/report/test_wave_headline_parity.py checks against the Python
 * renderer, so the two sides cannot drift apart.
 */
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from './fixtures/waveHeadlines.json';
import { formatShare, migrationWaves, waveCount, waveHeadline, waveMovesNothing } from '../migrationWaves';

let t;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

describe('waveHeadline matches the reports for every wave in the shared fixture', () => {
  test.each(Object.entries(fixture.expected_headlines))('wave %s', (waveNo, expected) => {
    const wave = fixture.synthesis.migration_waves.find(w => String(w.wave) === waveNo);
    expect(waveHeadline(t, fixture.synthesis, wave)).toBe(expected);
  });
});

test('a wave with no facts row states its served count and nothing it would have to compute', () => {
  const wave = { wave: 9, engines: ['dynamodb'], query_count: 3, workload_share_percent: 5, table_count: 2 };
  expect(waveHeadline(t, { migration_waves: [wave], facts: { waves: [] } }, wave))
    .toBe('3 queries (5% of queries), 2 source tables');
});

test('a wave with no tables has no table phrase', () => {
  const wave = { wave: 2, engines: ['elasticache'], query_count: 1, workload_share_percent: 10, share_basis: 'calls', table_count: 0 };
  expect(waveHeadline(t, { facts: { waves: [] } }, wave)).toBe('1 query (10% of calls)');
});

test('a cutover wave without engines falls back to the plain count phrase, like the reports', () => {
  const wave = { wave: 1, engines: [], query_count: 4, cutover_query_count: 9, workload_share_percent: 40, table_count: 0 };
  expect(waveHeadline(t, { facts: { waves: [] } }, wave)).toBe('4 queries (40% of queries)');
});

describe('formatShare mirrors src/report/formatting.py fmt_num(value, 1)', () => {
  test.each([
    [60.7476, '60.7'],
    [60, '60'],
    [100.0, '100'],
    [0, '0'],
    [0.04, '<0.1'],
    [1234.56, '1,234.6'],
    [undefined, '0'],
  ])('%p -> %p', (value, expected) => {
    expect(formatShare(value)).toBe(expected);
  });
});

test('migrationWaves reads report.json waves in order and drops anything that is not a wave', () => {
  const synthesis = { migration_waves: [{ wave: 1 }, null, 'x', { wave: 2 }] };
  expect(migrationWaves(synthesis).map(w => w.wave)).toEqual([1, 2]);
  expect(migrationWaves({})).toEqual([]);
  expect(migrationWaves(null)).toEqual([]);
});

test('waveCount is the facts layer figure, never a count of the list', () => {
  expect(waveCount({ migration_waves: [{ wave: 1 }], facts: { migration_wave_count: 3 } })).toBe(3);
  expect(waveCount({ migration_waves: [{ wave: 1 }], facts: {} })).toBeNull();
  expect(waveCount({})).toBeNull();
});

test('a wave that moves nothing states no headline (#1383)', () => {
  const wave = { wave: 1, title: 'No move to Aurora', engines: [], query_count: 0, workload_share_percent: 0, table_count: 50 };
  const synthesis = { migration_waves: [wave], facts: { waves: [{ wave: 1, no_move: true, served_tables: 50 }] } };
  expect(waveMovesNothing(synthesis, wave)).toBe(true);
  expect(waveHeadline(t, synthesis, wave)).toBe('');
});

test('a wave without the no_move fact keeps its headline (#1383)', () => {
  const wave = { wave: 9, engines: ['dynamodb'], query_count: 3, workload_share_percent: 5, table_count: 2 };
  const synthesis = { migration_waves: [wave], facts: { waves: [{ wave: 9, served_tables: 2 }] } };
  expect(waveMovesNothing(synthesis, wave)).toBe(false);
  expect(waveHeadline(t, synthesis, wave)).toBe('3 queries (5% of queries), 2 source tables');
});
