/**
 * #838: `sqlLocking.js` is the JavaScript copy of
 * `src/agents/referee/query_shape.py`'s `locking_clause`, the lock the ownership
 * gate reads (`cache_overlay.is_locking_read`). Both run the same cases from
 * `tests/unit/agents/referee/locking_clause_cases.json`, so the engine picker and
 * the server agree on which reads OpenSearch may own.
 */
import fs from 'fs';
import path from 'path';
import { lockingClause, sourceEngineOf } from '../sqlLocking';
import { engineOptionsForQuery } from '../assignmentSummary';

// nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
const CASES_PATH = path.resolve(
  __dirname,
  '../../../../../tests/unit/agents/referee/locking_clause_cases.json',
);
const { cases: CASES } = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));

const OPTIONS = [
  { label: 'OpenSearch', value: 'opensearch' },
  { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
];

describe('lockingClause', () => {
  test.each(CASES.map((c) => [JSON.stringify(c.text), c]))(
    'matches the Python detector on %s',
    (_label, c) => {
      expect(lockingClause(c.text, c.engine)).toBe(c.clause);
    },
  );

  test('the shared cases are loaded', () => {
    expect(CASES.length).toBeGreaterThan(40);
    expect(CASES.some((c) => c.clause === null)).toBe(true);
  });

  test('the shared cases name the dialects (#1143)', () => {
    const engines = new Set(CASES.map((c) => c.engine));
    ['mysql', 'mariadb', 'postgresql'].forEach((engine) => expect(engines).toContain(engine));
    expect(engines).toContain(undefined);
  });

  test('the dialect changes the answer for the same text', () => {
    expect(lockingClause('SELECT 1 # FOR UPDATE')).toBe('FOR UPDATE');
    expect(lockingClause('SELECT 1 # FOR UPDATE', 'mysql')).toBeNull();
    expect(lockingClause('SELECT $$ FOR UPDATE $$')).toBe('FOR UPDATE');
    expect(lockingClause('SELECT $$ FOR UPDATE $$', 'postgresql')).toBeNull();
  });

  test('sourceEngineOf reads metadata.source_database.engine, lower-cased', () => {
    expect(sourceEngineOf({ metadata: { source_database: { engine: ' MySQL ' } } })).toBe('mysql');
    expect(sourceEngineOf({ metadata: {} })).toBe('');
    expect(sourceEngineOf(null)).toBe('');
  });

  test('null and undefined take no lock', () => {
    expect(lockingClause(null)).toBeNull();
    expect(lockingClause(undefined)).toBeNull();
  });

  test('a long OF list finishes quickly', () => {
    const text = `SELECT * FROM t FOR UPDATE OF ${'a, '.repeat(200_000)}b NOWAIT`;
    const start = Date.now();
    expect(lockingClause(text)).toBe('FOR UPDATE NOWAIT');
    expect(Date.now() - start).toBeLessThan(5000);
  });
});

// CPU time of fn() in this process, in seconds (the JS side of
// tests/unit/tools/sql/_timing.py: wall-clock time also counts time spent
// waiting for a core on a busy runner).
function cpuSeconds(fn) {
  const start = process.cpuUsage();
  fn();
  const used = process.cpuUsage(start);
  return (used.user + used.system) / 1e6;
}

// _timing.py's assert_linear: the 4M-character input takes under 8x the 1M
// one (best of up to 12 alternating runs), and 1M stays under 10s CPU.
function expectLinear(makeText, fn) {
  const small = makeText(1_000_000);
  const large = makeText(4_000_000);
  fn(small);
  fn(large);
  let tSmall = Infinity;
  let tLarge = Infinity;
  for (let round = 0; round < 12; round += 1) {
    tSmall = Math.min(tSmall, cpuSeconds(() => fn(small)));
    expect(tSmall).toBeLessThan(10);
    tLarge = Math.min(tLarge, cpuSeconds(() => fn(large)));
    if (tLarge / Math.max(tSmall, 1e-6) < 8) {
      return;
    }
  }
  throw new Error(`4M took ${tLarge}s CPU vs ${tSmall}s for 1M: not linear`);
}

// Review of #871: a run of unterminated `[` did not finish at 1M characters when
// quoted identifiers had their own regex. Same units as test_cache_overlay.py.
const UNITS = ['[', '`', '"', "'", "'\\", '"\\', '/*', '*/', '--', "'[", '["', 'FOR UPDATE OF a,', 'WITH (', '('];

describe('lockingClause stays linear', () => {
  test.each(UNITS)('on a long run of %s', (unit) => {
    const prefix = 'SELECT * FROM t WHERE a[1] = 1 FOR UPDATE ';
    expectLinear(
      (n) => prefix + unit.repeat(Math.floor(n / unit.length)),
      (text) => lockingClause(text),
    );
  });
});

describe('engineOptionsForQuery reads the same lock', () => {
  test.each(CASES.map((c) => [JSON.stringify(c.text), c]))(
    'OpenSearch is offered for a SELECT only when it takes no lock: %s',
    (_label, c) => {
      const values = engineOptionsForQuery(OPTIONS, 'SELECT', c.text, c.engine).map(
        (o) => o.value,
      );
      expect(values.includes('opensearch')).toBe(c.clause === null);
      expect(values).toContain('aurora_postgresql');
    },
  );
});
