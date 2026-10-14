/**
 * #629 / #713 review round 3: the assignment gate marks a row the post-schema
 * router moved after approval, not only reality-check moves.
 */
import { reroutedQueryIds, rowMoveKind } from '../postSchemaReroutes';

const assignment = {
  post_schema_reroutes: [{ query_id: 'q-1', from_engine: 'dynamodb', to_engine: 'aurora_mysql' }],
};

test('a rerouted row is marked as moved after approval', () => {
  const ids = reroutedQueryIds(assignment);
  expect(
    rowMoveKind({ query_id: 'q-1', assignment_reason: 'post-schema routing: flagged on DynamoDB' }, ids),
  ).toBe('rerouted');
});

test('the structured list marks a row even without the reason prefix', () => {
  expect(rowMoveKind({ query_id: 'q-1', assignment_reason: 'anything' }, reroutedQueryIds(assignment))).toBe(
    'rerouted',
  );
});

test('a reality-check move keeps its own marker', () => {
  expect(
    rowMoveKind({ query_id: 'q-2', assignment_reason: 'reality check: consolidated from documentdb' }, new Set()),
  ).toBe('moved');
});

test('an unmoved row has no marker', () => {
  expect(rowMoveKind({ query_id: 'q-3', assignment_reason: 'highest confidence' }, new Set())).toBeNull();
  expect(reroutedQueryIds(null).size).toBe(0);
});
