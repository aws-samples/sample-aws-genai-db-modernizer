/**
 * #548 round 2 review item 3: a failed override PUT must show the API's real,
 * customer-facing reason (`response.detail`) -- not just "HTTP 400: Bad
 * Request" -- so a rejection like "DynamoDB can't run query q1: it uses a
 * leading-wildcard search (LIKE '%...')." actually reaches the customer.
 *
 * `errorFromResponse` is a pure function (status/statusText/responseText in,
 * an Error out) so these tests exercise the real parsing logic directly,
 * without mocking `XMLHttpRequest`.
 */
import { errorFromResponse } from '../apiErrors';

describe('errorFromResponse', () => {
  it('uses a plain string detail (ExcludedEngineOverride, LosingAuroraEngineOverride, UnknownQuery)', () => {
    const body = JSON.stringify({
      detail: "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...').",
    });
    const err = errorFromResponse(400, 'Bad Request', body);
    expect(err.message).toBe(
      "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...')."
    );
  });

  it('uses detail.message for the 422 hard-validation-error shape', () => {
    const body = JSON.stringify({
      detail: {
        message: 'Assignment validation failed with hard errors',
        errors: ["ERROR: Query q1 assigned to engine 'neptune' which did not analyze it."],
        warnings: [],
      },
    });
    const err = errorFromResponse(422, 'Unprocessable Entity', body);
    expect(err.message).toBe(
      "Assignment validation failed with hard errors: ERROR: Query q1 assigned to engine 'neptune' which did not analyze it."
    );
  });

  it('joins multiple detail.errors entries', () => {
    const body = JSON.stringify({
      detail: { message: 'Assignment validation failed with hard errors', errors: ['e1', 'e2'] },
    });
    const err = errorFromResponse(422, 'Unprocessable Entity', body);
    expect(err.message).toBe('Assignment validation failed with hard errors: e1; e2');
  });

  it('falls back to the HTTP status line when there is no response body', () => {
    const err = errorFromResponse(400, 'Bad Request', '');
    expect(err.message).toBe('HTTP 400: Bad Request');
  });

  it('falls back to the HTTP status line when the body is not JSON', () => {
    const err = errorFromResponse(502, 'Bad Gateway', '<html>502 Bad Gateway</html>');
    expect(err.message).toBe('HTTP 502: Bad Gateway');
  });

  it('falls back to the HTTP status line when detail is missing or empty', () => {
    expect(errorFromResponse(500, 'Internal Server Error', '{}').message).toBe(
      'HTTP 500: Internal Server Error'
    );
    expect(errorFromResponse(500, 'Internal Server Error', JSON.stringify({ detail: '' })).message).toBe(
      'HTTP 500: Internal Server Error'
    );
  });

  it('returns the detail as a plain string -- never HTML -- so React renders it as escaped text', () => {
    // A `detail` containing markup is still just data: errorFromResponse never
    // parses or executes it, so a consumer that renders `error.message` as a
    // normal React child (as the Flashbar in AssignmentGate.js does, with no
    // dangerouslySetInnerHTML anywhere on this path) gets it auto-escaped.
    const body = JSON.stringify({ detail: '<script>alert(1)</script> can\'t run this query' });
    const err = errorFromResponse(400, 'Bad Request', body);
    expect(err).toBeInstanceOf(Error);
    expect(typeof err.message).toBe('string');
    expect(err.message).toBe('<script>alert(1)</script> can\'t run this query');
  });

  // #1328 review: FastAPI's 422 detail is a list; the scope's own message reaches the form.
  it('uses each message of a FastAPI 422 list, without the "Value error, " prefix', () => {
    const body = JSON.stringify({
      detail: [{
        type: 'value_error',
        loc: ['body', 'target_databases'],
        msg: "Value error, target_databases: Neptune (neptune) is not available yet (a planned engine no analysis covers); choose from dynamodb",
      }],
    });
    expect(errorFromResponse(422, 'Unprocessable Entity', body).message).toBe(
      'target_databases: Neptune (neptune) is not available yet (a planned engine no analysis covers); choose from dynamodb',
    );
  });

  it('falls back for a 422 list with no message', () => {
    const body = JSON.stringify({ detail: [{ loc: ['body'] }] });
    expect(errorFromResponse(422, 'Unprocessable Entity', body).message).toBe('HTTP 422: Unprocessable Entity');
  });
});
