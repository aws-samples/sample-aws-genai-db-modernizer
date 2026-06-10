// Per-query custom k6 metrics.
//
// Each access pattern gets its own latency Trend and request/error Counters
// so the runner can report per-pattern percentiles in the load test results.
// Metric naming convention matches DynamoDB's pattern: `latency_${queryId}`,
// `requests_${queryId}`, `errors_${queryId}`.

import { Counter, Trend } from 'k6/metrics';

export function createPatternMetrics(queryId) {
  return {
    latency: new Trend(`latency_${queryId}`, true),
    requests: new Counter(`requests_${queryId}`),
    errors: new Counter(`errors_${queryId}`),
  };
}
