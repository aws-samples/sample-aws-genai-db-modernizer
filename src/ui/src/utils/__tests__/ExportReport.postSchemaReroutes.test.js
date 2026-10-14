/**
 * #629 / #713 review round 3: the interactive analysis report lists the
 * queries the post-schema router moved after approval, with the same
 * sentences and override path as the decision and engineering reports.
 * One file per load: the embedded script declares top-level consts.
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

describe('buildPostSchemaReroutes (#629)', () => {
  let section;

  beforeAll(() => {
    loadInteractiveReport(generateHTMLReport(data));
    section = document.querySelector('[data-figure="post-schema-reroutes"]');
  });

  test('renders the moved-after-approval section with the query and both engines', () => {
    expect(section).not.toBeNull();
    expect(section.textContent).toContain('Moved after your approval (1 query)');
    expect(section.textContent).toContain('q-rerouted-1');
    expect(section.textContent).toContain('it would need a client-side join; score 54 against 69');
  });

  test('states the approved version and the override path', () => {
    expect(section.textContent).toContain('After you approved the assignment (version 2)');
    expect(section.textContent).toContain(
      'To keep any of these on the engine you approved, change its engine in the assignment review',
    );
  });
});
