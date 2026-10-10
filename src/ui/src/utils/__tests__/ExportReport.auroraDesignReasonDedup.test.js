/**
 * #478: the relational branch of ``build_query_groups``
 * (``src/agents/referee/synthesis_report.py``) stores a group's reason text
 * once in its own ``reasons`` list and indexes into it from each access
 * pattern (``reason_index``) instead of copying the full string onto every
 * one of them -- 146 distinct reasons backed 1281 access patterns on the
 * discourse sample. The embedded report script's own ``reasonTextFor``
 * must resolve this the same way ``auroraDesign.js``'s does, or every query
 * row in the "Aurora design" section loses its reason text.
 *
 * Separate file from the other ``ExportReport.auroraDesign*.test.js`` files:
 * the embedded script declares top-level consts, so it can only be loaded
 * once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const dedupedGroup = () => ({
  group_name: 'table_0',
  engines: ['aurora_mysql'],
  reasons: ['highest confidence for aurora_mysql'],
  access_patterns: [
    { engine: 'aurora_mysql', design_rps: 4.5, reason_index: 0, query_ids: ['q1'] },
  ],
  source_queries: [
    { query_id: 'q1', query_text: 'SELECT * FROM table_0', query_type: 'SELECT' },
  ],
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

describe('Aurora design reason text resolves through reason_index + reasons (#478)', () => {
  it('shows the deduplicated reason text, not "undefined" or a blank cell', () => {
    const doc = loadInteractiveReport(generateHTMLReport(baseData([dedupedGroup()]))).document;
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('highest confidence for aurora_mysql');
    expect(container.textContent).not.toContain('undefined');
  });
});
