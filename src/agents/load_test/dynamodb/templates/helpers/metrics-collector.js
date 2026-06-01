import { Trend, Counter } from 'k6/metrics';

export function createPatternMetrics(queryId) {
  return {
    latency: new Trend(`latency_${queryId}`),
    requests: new Counter(`requests_${queryId}`),
    consumedRCU: new Trend(`consumed_rcu_${queryId}`),
    consumedWCU: new Trend(`consumed_wcu_${queryId}`),
    errors: new Counter(`errors_${queryId}`),
  };
}
