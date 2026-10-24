/**
 * #1373: the query journey's "Workload Fit" is the assignment's `confidence` as
 * the API serves it, the query's score on the engine it is on. A query the
 * reality check consolidated from DocumentDB (79) onto DynamoDB (78) shows 78%,
 * the figure its decision trace ends on, never the engine it left's 79%.
 */
import { journeyWorkloadFit } from '../queryRouting';

// The shape of GET /assessments/{job_id}/query-journeys/{query_id} for such a query.
const consolidated = {
  query_id: '413c99e4',
  assignment: {
    assigned_engine: 'dynamodb',
    confidence: 78,
    assignment_reason:
      'reality check: consolidated from DocumentDB → DynamoDB (no unique value: DocumentDB scores 79.0, DynamoDB scores 78.0)',
    in_scope: true,
  },
  trace: { final_engine: 'dynamodb', final_score: 78 },
};

test('shows the score of the engine the query is on', () => {
  expect(journeyWorkloadFit(consolidated)).toBe('78%');
  expect(journeyWorkloadFit(consolidated)).toBe(`${consolidated.trace.final_score}%`);
});

test('no figure reads as null', () => {
  expect(journeyWorkloadFit(null)).toBeNull();
  expect(journeyWorkloadFit({})).toBeNull();
  expect(journeyWorkloadFit({ assignment: {} })).toBeNull();
});

// #1373 (maintainer decision on #1381): a utility pin shows its pin, not its score.
const pinned = {
  query_id: 'pgcatalog',
  assignment: { assigned_engine: 'aurora_postgresql', confidence: 0, pinned: 'utility', in_scope: true },
};

test('a utility pin shows the pinned label, translated', () => {
  const t = (key) => ({ 'common.fit.pinned-utility': 'pinned (utility)' }[key] || key);
  expect(journeyWorkloadFit(pinned, t)).toBe('pinned (utility)');
});

test('a pin whose real score is not 0 shows the label too', () => {
  const t = (key) => key;
  const scoredPin = { assignment: { ...pinned.assignment, confidence: 67 } };
  expect(journeyWorkloadFit(scoredPin, t)).toBe('common.fit.pinned-utility');
});
