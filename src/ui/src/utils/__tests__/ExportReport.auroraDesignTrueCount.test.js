/**
 * #478: when analysis_report.py's
 * _project_query_groups_for_export has already trimmed a group's
 * access_patterns/source_queries to its busiest 50 entries, it carries
 * `query_count_by_engine` -- each engine's real, pre-trim count. Without
 * reading it, buildAuroraDesign summed only the survivors for its "N
 * queries on Aurora" heading (discourse showed 1213, not the true 1281),
 * and the "+N more" note never fired, since every group already looked
 * <= the cap by the time the client checked its own (post-trim) array
 * length. Separate file from ExportReport.auroraDesignCaps.test.js: the
 * embedded script declares top-level consts, so it can only be loaded
 * once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const trimmedGroup = () => ({
  group_name: 'table_0',
  engines: ['aurora_mysql'],
  // Only 50 entries, as if the server already trimmed it -- but
  // query_count_by_engine remembers the real total was 1281.
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
  query_count_by_engine: { aurora_mysql: 1281 },
});

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

describe('buildAuroraDesign true counts when the server already trimmed a group (#478)', () => {
  let doc;

  beforeAll(() => {
    doc = loadInteractiveReport(generateHTMLReport(baseData([trimmedGroup()]))).document;
  });

  it('sums the true per-engine count for the "N queries on Aurora" heading, not the trimmed array length', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('1281 queries on Aurora');
  });

  it('shows "+N more" using the true count minus what is actually rendered', () => {
    const container = doc.getElementById('aurora-design-container');
    // 50 rows rendered (the display cap), 1281 - 50 = 1231 not shown.
    expect(container.textContent).toContain('+1231 more queries not shown');
  });

  it('shows the true count in the per-table summary too', () => {
    const container = doc.getElementById('aurora-design-container');
    const summaries = [...container.querySelectorAll('details > summary')];
    const tableSummary = summaries.find((s) => s.textContent.includes('table_0'));
    expect(tableSummary.textContent).toContain('(1281)');
  });
});
