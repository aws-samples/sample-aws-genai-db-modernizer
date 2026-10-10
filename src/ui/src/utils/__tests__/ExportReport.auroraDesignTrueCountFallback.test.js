/**
 * #478: without `query_count_by_engine` (the live
 * "Export to HTML" button's own DATA, never projected -- built straight
 * from the page's own state), the group's rendered array length is
 * already the true count. Separate file from
 * ExportReport.auroraDesignTrueCount.test.js: the embedded script
 * declares top-level consts, so it can only be loaded once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const baseData = (queryGroups) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [
    {
      target_type: 'aurora_mysql',
      content: { migration_strategy: 'carry_over', table_definitions: [] },
    },
  ],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 1 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
      query_groups: queryGroups,
    },
  },
});

const loadInteractiveReport = (markup) => {
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter((el) => !el.src);
  expect(inline).toHaveLength(1);
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

describe('buildAuroraDesign without query_count_by_engine (the live "Export to HTML" path)', () => {
  it('falls back to the rendered array length, unaffected', () => {
    const group = {
      group_name: 'table_0',
      engines: ['aurora_mysql'],
      access_patterns: Array.from({ length: 50 }, (_, qi) => ({
        engine: 'aurora_mysql',
        design_rps: 1,
        query_ids: [`q${qi}`],
      })),
      source_queries: Array.from({ length: 50 }, (_, qi) => ({
        query_id: `q${qi}`,
        query_text: `SELECT ${qi} FROM table_0`,
        query_type: 'SELECT',
      })),
    };
    const doc = loadInteractiveReport(generateHTMLReport(baseData([group]))).document;
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('50 queries on Aurora');
    expect(container.textContent).not.toContain('more queries not shown');
  });
});
