/**
 * #250: the Assignment review hero must name the database and the workload size
 * whether or not the reality check carries an LLM-written executive summary.
 *
 * #254: those strings must be translatable and the access-pattern/table counts
 * must pluralise correctly (CLDR `_one`/`_other`, not always-plural English).
 * `t` is wired up to a real i18next instance initialised with locales/en.json
 * so plural resolution is actually exercised, not just the JS fallback.
 */
import i18next from 'i18next';
import {
  buildAssignmentSummary,
  engineOptionsForAssignment,
  engineOptionsForQuery,
} from '../assignmentSummary';
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

const LABELS = { dynamodb: 'DynamoDB', elasticache: 'ElastiCache', opensearch: 'OpenSearch' };
const label = (e) => LABELS[e] || e;
const AFTER = { dynamodb: 69, elasticache: 34, opensearch: 4 };
const PATTERNS = [{ name: 'Command Query Responsibility Segregation (CQRS)' }];
const LLM = 'Your workload consolidates onto Amazon DynamoDB as the primary engine.';

const build = (overrides = {}) => buildAssignmentSummary({
  llmSummary: null,
  databaseName: 'wordpress',
  afterDist: AFTER,
  tableCount: 50,
  patterns: PATTERNS,
  engineLabel: label,
  t,
  ...overrides,
});

describe('buildAssignmentSummary', () => {
  test('deterministic run: fact line, distribution and pattern', () => {
    expect(build()).toBe(
      'Your wordpress workload has 107 access patterns across 50 tables. '
      + 'We map 69 to DynamoDB, 34 to ElastiCache, 4 to OpenSearch. '
      + 'Recommended integration pattern: Command Query Responsibility Segregation (CQRS).',
    );
  });

  test('LLM summary present: fact line still leads, LLM prose follows', () => {
    expect(build({ llmSummary: LLM })).toBe(
      `Your wordpress workload has 107 access patterns across 50 tables. ${LLM}`,
    );
  });

  test('LLM summary is trimmed and blank summaries fall back to the deterministic text', () => {
    expect(build({ llmSummary: `  ${LLM}\n` })).toBe(
      `Your wordpress workload has 107 access patterns across 50 tables. ${LLM}`,
    );
    expect(build({ llmSummary: '   ' })).toBe(build());
  });

  test('single engine and unknown table count', () => {
    expect(build({ afterDist: { dynamodb: 12 }, tableCount: 0 })).toBe(
      'Your wordpress workload has 12 access patterns. All access patterns map to DynamoDB.',
    );
  });

  test('no surviving engines: LLM summary alone, else empty', () => {
    expect(build({ afterDist: {} })).toBe('');
    expect(build({ afterDist: {}, llmSummary: LLM })).toBe(LLM);
  });

  test('missing database name falls back to a generic subject', () => {
    expect(build({ databaseName: '', llmSummary: LLM })).toBe(
      `Your database workload has 107 access patterns across 50 tables. ${LLM}`,
    );
  });

  test('#254 count=1: access pattern and table both pluralise to the singular form', () => {
    expect(build({ afterDist: { dynamodb: 1 }, tableCount: 1 })).toBe(
      'Your wordpress workload has 1 access pattern across 1 table. All access patterns map to DynamoDB.',
    );
  });

  test('#254 count=0: zero access patterns use the CLDR "other" form, not "one"', () => {
    expect(build({ afterDist: { dynamodb: 0 }, tableCount: 0 })).toBe(
      'Your wordpress workload has 0 access patterns. All access patterns map to DynamoDB.',
    );
  });

  test('#254 falls back to sane English when no translation function is supplied', () => {
    const { t: _omitted, ...withoutT } = {
      llmSummary: null,
      databaseName: 'wordpress',
      afterDist: { dynamodb: 1 },
      tableCount: 1,
      patterns: PATTERNS,
      engineLabel: label,
      t,
    };
    expect(buildAssignmentSummary(withoutT)).toBe(
      'Your wordpress workload has 1 access pattern across 1 table. All access patterns map to DynamoDB.',
    );
  });
});

describe('#296 cache overlay', () => {
  const CACHE_OVERLAY = {
    engine: 'elasticache',
    query_count: 20,
    calls_per_second: 120.4,
    call_share_percent: 83.4,
    owners: { dynamodb: 20 },
  };

  test('new-shape artifact: afterDist already excludes elasticache (owners-only); the hero names it as a cache layer, never "N to ElastiCache"', () => {
    const summary = build({
      afterDist: { dynamodb: 69, opensearch: 4 },
      cacheOverlay: CACHE_OVERLAY,
    });
    expect(summary).toBe(
      'Your wordpress workload has 73 access patterns across 50 tables. '
      + 'We map 69 to DynamoDB, 4 to OpenSearch. '
      + 'Recommended integration pattern: Command Query Responsibility Segregation (CQRS). '
      + 'Cache layer · 20 cached reads · 83.4% of calls.',
    );
    expect(summary).not.toContain('ElastiCache');
    expect(summary).not.toMatch(/\b0%/);
  });

  test('defensively strips elasticache out of afterDist when a cacheOverlay is present, even if the artifact still lists it as an owner', () => {
    const summary = build({ afterDist: AFTER, cacheOverlay: CACHE_OVERLAY });
    expect(summary).toBe(
      'Your wordpress workload has 73 access patterns across 50 tables. '
      + 'We map 69 to DynamoDB, 4 to OpenSearch. '
      + 'Recommended integration pattern: Command Query Responsibility Segregation (CQRS). '
      + 'Cache layer · 20 cached reads · 83.4% of calls.',
    );
  });

  test('legacy artifact (no cacheOverlay): elasticache keeps rendering as a real owner, exactly as before', () => {
    expect(build()).toBe(
      'Your wordpress workload has 107 access patterns across 50 tables. '
      + 'We map 69 to DynamoDB, 34 to ElastiCache, 4 to OpenSearch. '
      + 'Recommended integration pattern: Command Query Responsibility Segregation (CQRS).',
    );
  });

  test('single surviving owner plus a cache overlay: cache-layer sentence still appended', () => {
    const summary = build({
      afterDist: { dynamodb: 12 },
      tableCount: 0,
      cacheOverlay: CACHE_OVERLAY,
    });
    expect(summary).toBe(
      'Your wordpress workload has 12 access patterns. All access patterns map to DynamoDB. '
      + 'Cache layer · 20 cached reads · 83.4% of calls.',
    );
  });

  test('no owners survive but a cache overlay exists: cache-layer line alone (no empty "We map" sentence)', () => {
    expect(build({ afterDist: {}, cacheOverlay: CACHE_OVERLAY })).toBe(
      'Cache layer · 20 cached reads · 83.4% of calls',
    );
  });

  test('LLM summary present alongside a cache overlay: fact line, then LLM prose, then the cache-layer sentence', () => {
    expect(build({
      afterDist: { dynamodb: 69, opensearch: 4 },
      llmSummary: LLM,
      cacheOverlay: CACHE_OVERLAY,
    })).toBe(
      `Your wordpress workload has 73 access patterns across 50 tables. ${LLM} `
      + 'Cache layer · 20 cached reads · 83.4% of calls.',
    );
  });
});

describe('engineOptionsForAssignment', () => {
  // #381 review round 2: the losing Aurora engine (a heterogeneous source's
  // resolver choice between aurora_mysql/aurora_postgresql) must never appear as a
  // per-query override target -- the server rejects it with no warning otherwise.
  const OPTIONS = [
    { label: 'DynamoDB', value: 'dynamodb' },
    { label: 'Aurora MySQL', value: 'aurora_mysql' },
    { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
  ];

  test('drops the losing engine when a choice is recorded', () => {
    const assignment = { aurora_engine_choice: { engine: 'aurora_postgresql' } };
    expect(engineOptionsForAssignment(OPTIONS, assignment)).toEqual([
      { label: 'DynamoDB', value: 'dynamodb' },
      { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
    ]);
  });

  test('drops the other losing engine when mysql wins', () => {
    const assignment = { aurora_engine_choice: { engine: 'aurora_mysql' } };
    expect(engineOptionsForAssignment(OPTIONS, assignment)).toEqual([
      { label: 'DynamoDB', value: 'dynamodb' },
      { label: 'Aurora MySQL', value: 'aurora_mysql' },
    ]);
  });

  test('returns every option unchanged when there is no recorded choice', () => {
    expect(engineOptionsForAssignment(OPTIONS, {})).toEqual(OPTIONS);
    expect(engineOptionsForAssignment(OPTIONS, undefined)).toEqual(OPTIONS);
    expect(engineOptionsForAssignment(OPTIONS, null)).toEqual(OPTIONS);
  });

  test('returns every option unchanged for a homogeneous source (non-Aurora winner)', () => {
    const assignment = { aurora_engine_choice: { engine: 'dynamodb' } };
    expect(engineOptionsForAssignment(OPTIONS, assignment)).toEqual(OPTIONS);
  });
});

describe('engineOptionsForQuery', () => {
  // #303: OpenSearch is a read model, it never owns a write -- a customer
  // override that put a write on it would be rejected server-side
  // (AssignmentValidator) with no warning here, so it is never offered.
  const OPTIONS = [
    { label: 'DynamoDB', value: 'dynamodb' },
    { label: 'OpenSearch', value: 'opensearch' },
    { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
  ];

  test.each(['INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'MERGE', 'UPSERT', 'insert'])(
    'drops OpenSearch for a %s row',
    (queryType) => {
      expect(engineOptionsForQuery(OPTIONS, queryType)).toEqual([
        { label: 'DynamoDB', value: 'dynamodb' },
        { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
      ]);
    },
  );

  test('returns every option unchanged for a read', () => {
    expect(engineOptionsForQuery(OPTIONS, 'SELECT')).toEqual(OPTIONS);
  });

  test('returns every option unchanged for an unknown or missing query type', () => {
    expect(engineOptionsForQuery(OPTIONS, 'OTHER')).toEqual(OPTIONS);
    expect(engineOptionsForQuery(OPTIONS, undefined)).toEqual(OPTIONS);
    expect(engineOptionsForQuery(OPTIONS, null)).toEqual(OPTIONS);
  });

  // Same shapes as cache_overlay.py's is_write_query/is_locking_read tests:
  // a data-modifying CTE, CALL, TRUNCATE, COPY, a comment-led statement, or
  // a locking read all come through as query_type "OTHER"/"SELECT".
  test.each([
    'WITH w AS (INSERT INTO user_badges(id) VALUES (?) RETURNING id) SELECT * FROM w',
    'UPDATE translation_overrides SET status = ? WHERE id = ?',
    'DELETE FROM user_badges WHERE id IN (SELECT id FROM x)',
    'MERGE INTO t USING s ON t.id = s.id',
    'CALL some_procedure(?)',
    'TRUNCATE TABLE sessions',
    "COPY t FROM 'file.csv'",
    '-- a leading comment\nINSERT INTO t VALUES (?)',
  ])('drops OpenSearch for OTHER-typed DML: %s', (queryText) => {
    expect(engineOptionsForQuery(OPTIONS, 'OTHER', queryText)).toEqual([
      { label: 'DynamoDB', value: 'dynamodb' },
      { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
    ]);
  });

  test('returns every option unchanged for an OTHER-typed read CTE', () => {
    expect(engineOptionsForQuery(OPTIONS, 'OTHER', 'WITH w AS (SELECT 1) SELECT * FROM w')).toEqual(
      OPTIONS,
    );
  });

  test.each([
    'SELECT * FROM t WHERE id = ? FOR UPDATE',
    'SELECT * FROM t WHERE id = ? FOR SHARE',
    'SELECT * FROM t WHERE id = ? LOCK IN SHARE MODE',
  ])('drops OpenSearch for a locking read: %s', (queryText) => {
    expect(engineOptionsForQuery(OPTIONS, 'SELECT', queryText)).toEqual([
      { label: 'DynamoDB', value: 'dynamodb' },
      { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
    ]);
  });

  test('a plain read with no locking clause keeps every option', () => {
    expect(engineOptionsForQuery(OPTIONS, 'SELECT', 'SELECT * FROM t WHERE id = ?')).toEqual(
      OPTIONS,
    );
  });
});
