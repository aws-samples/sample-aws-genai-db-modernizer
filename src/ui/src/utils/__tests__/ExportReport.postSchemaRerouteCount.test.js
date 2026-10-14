/**
 * #615: the interactive analysis report prints the stored reroute count
 * (facts.post_schema_reroute_count) in the heading and the sentence, never
 * the number of rows. One file per load: the embedded script declares
 * top-level consts.
 */
import { generateHTMLReport } from '../ExportReport';

const REROUTE = {
  query_id: 'q-rerouted-1',
  from_engine: 'dynamodb',
  to_engine: 'aurora_mysql',
  reason: 'flagged on DynamoDB',
  reason_category: 'client_side_join',
  score_before: 69,
  score_after: 54,
  approved_version: 2,
};

const data = {
  jobId: 'job-1',
  exportDate: '2026-10-14T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 65, dynamodb: 41 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
      query_groups: [],
      post_schema_reroutes: [REROUTE],
      facts: { post_schema_reroute_count: 5 },
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

describe('post-schema reroute count (#615)', () => {
  test('prints the stored fact, not the number of rows', () => {
    loadInteractiveReport(generateHTMLReport(data));
    const text = document.querySelector('[data-figure="post-schema-reroutes"]').textContent;
    expect(text).toContain('Moved after your approval (5 queries)');
    expect(text).toContain('found that 5 queries cannot run');
  });
});
