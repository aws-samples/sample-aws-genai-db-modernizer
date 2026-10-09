/**
 * #478: buildAuroraDesign (ExportReport.js) must show an
 * Aurora engine's queries even when it has no schema design yet -- the
 * assignment still routed queries there, same as the engineering report's
 * "Queries on Aurora by table" subsection, which never required one.
 * Separate file from ExportReport.auroraDesign.test.js: the embedded script
 * declares top-level consts, so it can only be loaded once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const baseData = (schemaDesigns, queryGroups) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns,
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'adventureworks',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 1, documentdb: 1 } },
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

describe('buildAuroraDesign with no schema design but real queries (#478)', () => {
  let doc;

  beforeAll(() => {
    const queryGroups = [
      {
        group_name: 'Table not identified by the collector',
        engines: ['aurora_mysql'],
        access_patterns: [{ engine: 'aurora_mysql', design_rps: 999, query_ids: ['q1'] }],
        source_queries: [{ query_id: 'q1', query_text: 'SELECT 1' }],
      },
      {
        group_name: 'Person.Person',
        engines: ['aurora_mysql'],
        access_patterns: [{ engine: 'aurora_mysql', design_rps: 1, query_ids: ['q2'] }],
        source_queries: [{ query_id: 'q2', query_text: 'SELECT * FROM Person.Person' }],
      },
    ];
    doc = loadInteractiveReport(generateHTMLReport(baseData([], queryGroups))).document;
  });

  it('shows the Aurora engine section even with no schema design', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('Aurora MySQL');
    expect(container.textContent).toContain('Person.Person');
  });

  it('shows zero tables and no DDL/optimizations sections', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('0 tables');
    expect(container.querySelector('pre')).toBeNull();
  });

  it('sorts the blame-label group last despite the highest calls/s', () => {
    const container = doc.getElementById('aurora-design-container');
    const summaries = [...container.querySelectorAll('details > summary')]
      .map((el) => el.textContent)
      .filter((t) => t.includes('(') && (t.includes('Person.Person') || t.includes('not identified')));
    expect(summaries[0]).toContain('Person.Person');
    expect(summaries[summaries.length - 1]).toContain('not identified');
  });
});
