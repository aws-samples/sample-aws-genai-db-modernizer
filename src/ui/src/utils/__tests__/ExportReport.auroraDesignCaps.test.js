/**
 * #478: buildAuroraDesign caps what it renders -- DDL at display
 * time (separate from src/report/analysis_report.py's MAX_EMBEDDED_DDL_CHARS,
 * which caps the embedded DATA itself) and the number of table groups/
 * queries per group, in both export paths (this file covers the in-browser
 * "Export to HTML" path; the ATX path embeds the same script).
 */
import { generateHTMLReport } from '../ExportReport';

const manyGroups = (groupCount, queriesPerGroup) =>
  Array.from({ length: groupCount }, (_, gi) => ({
    group_name: `table_${gi}`,
    engines: ['aurora_mysql'],
    access_patterns: Array.from({ length: queriesPerGroup }, (_, qi) => ({
      engine: 'aurora_mysql',
      design_rps: 1,
      query_ids: [`g${gi}q${qi}`],
    })),
    source_queries: Array.from({ length: queriesPerGroup }, (_, qi) => ({
      query_id: `g${gi}q${qi}`,
      query_text: `SELECT ${qi} FROM table_${gi}`,
      query_type: 'SELECT',
    })),
  }));

const baseData = (schemaDesigns, queryGroups) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns,
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

describe('buildAuroraDesign caps (#478)', () => {
  let doc;

  beforeAll(() => {
    const schemaDesigns = [
      {
        target_type: 'aurora_mysql',
        content: {
          migration_strategy: 'carry_over',
          generated_ddl: 'x'.repeat(25000),
          table_definitions: [{ table_name: 't', columns: [{ name: 'id' }] }],
        },
      },
    ];
    doc = loadInteractiveReport(
      generateHTMLReport(baseData(schemaDesigns, manyGroups(25, 60)))
    ).document;
  });

  it('truncates the DDL display with a note', () => {
    const container = doc.getElementById('aurora-design-container');
    const pre = container.querySelector('pre');
    expect(pre.textContent.length).toBeLessThan(21000);
    expect(pre.textContent).toContain('truncated');
    expect(pre.textContent).toContain('5000 more characters omitted');
  });

  it('caps the number of table groups shown, with a note', () => {
    const container = doc.getElementById('aurora-design-container');
    // One <details> per shown group plus the outer DDL/tables/optimizations
    // <details> -- count only the per-table ones via their query tables.
    const tableHeaders = [...container.querySelectorAll('table')];
    expect(tableHeaders.length).toBe(20);
    expect(container.textContent).toContain('+5 more tables not shown');
  });

  it('caps the number of queries per group shown, with a note', () => {
    const container = doc.getElementById('aurora-design-container');
    const firstTable = container.querySelector('table');
    const rows = firstTable.querySelectorAll('tbody tr');
    expect(rows.length).toBe(50);
    expect(container.textContent).toContain('+10 more queries not shown');
  });
});
