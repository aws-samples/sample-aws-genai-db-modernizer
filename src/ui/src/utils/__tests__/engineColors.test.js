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
import {
  ENGINE_STYLES, DEFAULT_ENGINE_HEX, badgeTextColor, contrastRatio, engineBadgeStyle, engineHex,
} from '../engineColors';

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
    expect(engineHex(engine)).toBe(ENGINE_HEX[engine]);
  });
  // #1246: each Aurora engine has its own colour.
  expect(engineHex('aurora_mysql')).toBe('#9c5700');
  expect(engineHex('aurora_postgresql')).toBe('#c33d69');
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

// #1246 (with #1088, building on #1105): one engine palette for every view. Badges used
// four named Cloudscape colours, so Aurora read grey on the cost cards (a private table
// on the results page missed it), the roadmap drew every engine in DynamoDB's blue, and
// the pattern analysis page kept its own seven-engine table. Every engine badge is now
// filled with the engine's catalog colour.
const PATTERN_ANALYSIS_BADGES_BEFORE = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'grey',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
};

test('every catalog engine badge is filled with its catalog colour, with readable text', () => {
  Object.keys(ENGINE_LABELS).forEach((engine) => {
    const { root } = engineBadgeStyle(engine);
    expect(root.background).toBe(ENGINE_HEX[engine]);
    expect(root.borderColor).toBe(ENGINE_HEX[engine]);
    // The better of white or near-black text: at least 4.5:1 on every catalog colour.
    expect(contrastRatio(root.background, root.color)).toBeGreaterThanOrEqual(4.5);
  });
});

test('the engines the pattern analysis table missed now get their catalog colour (#1088)', () => {
  const missed = Object.keys(ENGINE_LABELS).filter((engine) => !(engine in PATTERN_ANALYSIS_BADGES_BEFORE));
  expect(missed.sort()).toEqual(['aurora_mysql', 'aurora_postgresql']);
  missed.forEach((engine) => {
    expect(engineBadgeStyle(engine).root.background).toBe(ENGINE_HEX[engine]);
  });
});

test('an unknown engine is neutral grey, never another engine colour', () => {
  expect(DEFAULT_ENGINE_HEX).toBe('#5f6b7a');
  expect(Object.values(ENGINE_HEX)).not.toContain(DEFAULT_ENGINE_HEX);
  expect(engineBadgeStyle('new_engine_from_a_future_catalog').root.background).toBe(DEFAULT_ENGINE_HEX);
  expect(engineBadgeStyle(undefined).root.background).toBe(DEFAULT_ENGINE_HEX);
});

test('badge text picks the higher-contrast colour', () => {
  expect(badgeTextColor('#ffffff')).not.toBe('#ffffff');
  expect(badgeTextColor('#000000')).toBe('#ffffff');
  expect(contrastRatio('#ffffff', '#000000')).toBeCloseTo(21, 5);
  expect(contrastRatio('#123456', '#123456')).toBeCloseTo(1, 5);
});

// #1246 review: neighbouring engines (share-bar segments, Sankey nodes, badges side by
// side) must read apart, Aurora MySQL and Aurora PostgreSQL included. CIE76 colour
// difference in CIELAB: 20 is a clearly visible difference for adjacent fills.
const lab = (hex) => {
  const lin = (i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = [lin(1), lin(3), lin(5)];
  const f = (v) => (v > 0.008856 ? Math.cbrt(v) : 7.787 * v + 16 / 116);
  const x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047);
  const y = f(0.2126 * r + 0.7152 * g + 0.0722 * b);
  const z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
};
const deltaE = (a, b) => Math.hypot(...lab(a).map((v, i) => v - lab(b)[i]));

test('every two catalog engines are clearly different colours', () => {
  const engines = Object.keys(ENGINE_LABELS);
  expect(engines).toEqual(expect.arrayContaining(['aurora_mysql', 'aurora_postgresql']));
  engines.forEach((a, i) => engines.slice(i + 1).forEach((b) => {
    expect([a, b, deltaE(ENGINE_HEX[a], ENGINE_HEX[b]) >= 20]).toEqual([a, b, true]);
  }));
});
