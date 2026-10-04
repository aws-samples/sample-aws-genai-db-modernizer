/**
 * #250: the Assignment review hero must name the database and the workload size
 * whether or not the reality check carries an LLM-written executive summary.
 */
import { buildAssignmentSummary } from '../assignmentSummary';

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
});
