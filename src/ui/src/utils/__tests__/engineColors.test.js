/**
 * #482: the Job summary page kept its own engine colour tables instead of reading
 * the shared palette, and its seven-engine hex table was missing two of the nine
 * catalog engines. `ENGINE_COLORS_HEX[primaryTarget]` was therefore `undefined`
 * for a signal routed to either Aurora engine, so the chip rendered with
 * `color: undefined` and `background: 'undefined18'` -- no colour at all.
 *
 * The table as it stood on main is kept verbatim below, so this file also fails if
 * an engine is dropped from the shared lookup again.
 */
import { ENGINE_HEX, ENGINE_LABELS, ENGINE_PICKER_ORDER } from '../engineCatalog.generated';
import { ENGINE_STYLES, DEFAULT_ENGINE_HEX, engineHex } from '../engineColors';

const JOB_SUMMARY_HEX_BEFORE = {
  dynamodb: '#3184e8',
  documentdb: '#1d8102',
  elasticache: '#d13212',
  opensearch: '#2ea597',
  neptune: '#7d2105',
  keyspaces: '#8b6ccb',
  aurora: '#ec7211',
};

test('every engine label resolves to a colour', () => {
  Object.keys(ENGINE_LABELS).forEach((engine) => {
    expect(engineHex(engine)).toMatch(/^#[0-9a-f]{6}$/i);
    expect(engineHex(engine)).toBe(ENGINE_HEX[engine]);
  });
});

test('the engines the job summary table missed resolve to the live UI colour', () => {
  const missed = Object.keys(ENGINE_LABELS).filter((engine) => !(engine in JOB_SUMMARY_HEX_BEFORE));

  // Exactly the two the private table never carried: both aurora_mysql and
  // aurora_postgresql are catalog engines, so both must resolve.
  expect(missed.sort()).toEqual(['aurora_mysql', 'aurora_postgresql']);
  missed.forEach((engine) => {
    expect(engineHex(engine)).toBe('#9c5700');
  });
});

test('the lookup agrees with the assignment gate palette', () => {
  ENGINE_PICKER_ORDER.forEach((engine) => {
    expect(engineHex(engine)).toBe(ENGINE_STYLES[engine].bg);
  });
});

test('an engine the palette does not know still gets a usable colour', () => {
  expect(engineHex('new_engine_from_a_future_catalog')).toBe(DEFAULT_ENGINE_HEX);
  expect(engineHex(undefined)).toBe(DEFAULT_ENGINE_HEX);
  expect(engineHex(null)).toBe(DEFAULT_ENGINE_HEX);
});
