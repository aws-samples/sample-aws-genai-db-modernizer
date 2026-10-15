/**
 * #730: the interactive analysis report states the close calls from
 * facts.close_calls -- the same title, pair sentences and override path as the
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
      reality_check: { after_distribution: { aurora_mysql: 65, dynamodb: 41 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
      query_groups: [],
      facts: {
        close_calls: {
          margin_points: 5,
          close_call_query_count: 3,
          settled_tie_count: 2,
          engine_pairs: [
            { engine: 'dynamodb', alternative: 'aurora_mysql', pair_query_count: 2, exact_tie_count: 1, group_mean_count: 1, min_gap: 1, max_gap: 4, query_ids: ['q-b'], more_query_count: 0 },
            { engine: 'aurora_mysql', alternative: 'documentdb', pair_query_count: 0, exact_tie_count: 1, group_mean_count: 0, min_gap: 0, max_gap: 0, query_ids: ['q-t'], more_query_count: 0 },
            { engine: 'aurora_mysql', alternative: 'dynamodb', pair_query_count: 1, exact_tie_count: 0, group_mean_count: 0, min_gap: 2, max_gap: 2, query_ids: ['q-<z>'], more_query_count: 0 },
          ],
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

describe('buildCloseCalls (#730)', () => {
  let section;

  beforeAll(() => {
    loadInteractiveReport(generateHTMLReport(data));
    section = document.querySelector('[data-figure="close-calls"]');
  });

  test('states the same title and pair sentences as the reports', () => {
    expect(section).not.toBeNull();
    const text = section.textContent;
    expect(text).toContain('Close calls (3 queries), plus 2 exact ties a rule settled');
    expect(text).toContain('2 queries went to DynamoDB by less than 5 points over Aurora MySQL (a gap of 1 to 4 points); DynamoDB also won 1 exact tie with Aurora MySQL that a routing rule settled; 1 is compared on its co-dependency group\'s mean score and moves with the group. Aurora MySQL can serve every one of them.');
    expect(text).toContain('1 exact tie between Aurora MySQL and DocumentDB went to Aurora MySQL by a routing rule. DocumentDB can serve it.');
    expect(text).toContain('1 query went to Aurora MySQL by less than 5 points over DynamoDB (a gap of 2 points). DynamoDB can serve it.');
  });

  test('states the override path', () => {
    expect(section.textContent).toContain('change its engine in the assignment review');
  });
});
