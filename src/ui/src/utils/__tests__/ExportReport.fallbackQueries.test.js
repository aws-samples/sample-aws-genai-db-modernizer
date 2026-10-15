/**
 * #729: the interactive analysis report states the queries on a fallback
 * engine from facts.unservable -- the same title, rows and re-run path as the
 * decision and engineering reports. One file per load: the embedded script
 * declares top-level consts.
 */
import { generateHTMLReport } from '../ExportReport';

const data = {
  jobId: 'job-1',
  exportDate: '2026-10-15T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 65 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
      query_groups: [],
      facts: {
        unservable: {
          unservable_query_count: 1,
          fallback_query_count: 1,
          gated_everywhere_query_count: 0,
          unservable_queries: [{
            query_id: 'q-<1>',
            engine: 'aurora_mysql',
            kind: 'fallback',
            rule: 'fallback.no_score',
            explanation: 'No engine could score this query, so it falls back to Aurora MySQL.',
            blocking_gates: [{ engine: 'dynamodb', gate: 'capability', gate_text: 'missing a capability the query needs' }],
            required_capabilities: ['aggregation'],
            capability_labels: ['aggregation'],
            redesign_hints: ['For DynamoDB: keep the aggregate as an item updated on each write.'],
          }],
          more_unservable_count: 2,
        },
      },
    },
  },
};

const loadInteractiveReport = (markup) => {
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter((el) => !el.src);
  const code = inline[0].textContent;
  parsed.querySelectorAll('script').forEach((el) => el.remove());
  window.Chart = class { destroy() {} };
  jest.spyOn(console, 'log').mockImplementation(() => {});
  document.head.innerHTML = parsed.head.innerHTML;
  document.body.innerHTML = parsed.body.innerHTML;
  const script = document.createElement('script');
  script.textContent = code;
  document.body.appendChild(script);
  document.dispatchEvent(new Event('DOMContentLoaded'));
  return window;
};

describe('buildFallbackQueries (#729)', () => {
  let section;

  beforeAll(() => {
    loadInteractiveReport(generateHTMLReport(data));
    section = document.querySelector('[data-figure="unservable-queries"]');
  });

  test('states where each query landed, why, the gates and the redesign', () => {
    expect(section).not.toBeNull();
    const text = section.textContent;
    expect(text).toContain('Queries on a fallback engine (1 query)');
    expect(text).toContain('q-<1> landed on Aurora MySQL: No engine could score this query, so it falls back to Aurora MySQL. Ruled out: DynamoDB (missing a capability the query needs). Keep it on Aurora MySQL, or redesign it. For DynamoDB: keep the aggregate as an item updated on each write.');
    expect(text).toContain('2 more queries: see the Engineering Report.');
    expect(section.querySelector('code').textContent).toBe('q-<1>');
  });

  test('states the re-run path', () => {
    expect(section.textContent).toContain('To re-run, open the assignment review');
  });
});
