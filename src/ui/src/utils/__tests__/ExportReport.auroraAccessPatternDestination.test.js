/**
 * #157/#429 part 1: once Aurora's schema design has a real, script-derived
 * `access_patterns` entry (not a `query_groups` routing record -- that's
 * ExportReport.auroraDesign.test.js), the Access Pattern Explorer must show
 * its table, index used and SQL shape -- never a "—" destination, which is
 * what `extractPatterns`/`showPatternDetails` fell back to before this fix
 * because Aurora's own pattern has no `table_name`/`key_pattern`/etc. and
 * keys its ids `source_query_ids`, not `query_ids`.
 *
 * Same embedded-script-execution technique as ExportReport.cachePatterns.test.js;
 * kept in its own file for the same reason (top-level `const`s, loaded once).
 */
import { generateHTMLReport } from '../ExportReport';

const AURORA_PATTERN = {
  pattern_id: 'aur-q1',
  description: 'SELECT on wordpress.wp_posts; filtered by id',
  operation: 'SELECT',
  sql_text: 'SELECT * FROM wordpress.wp_posts WHERE id = ?',
  source_tables: ['wordpress.wp_posts'],
  source_query_ids: ['q1'],
  filter_columns: ['id'],
  sort_columns: [],
  index_used: 'PRIMARY',
  design_rps: 4.5,
  source_latency_ms_p95: 12.0,
  source_db_load_pct: 14.0,
  script_derived: true,
  needs_judgment: false,
  judgment_reason: null,
  in_scope: true,
  out_of_scope_reason: null,
  load_testable: true,
  load_testable_reason: null,
};

const reportData = () => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [
    { target_type: 'aurora_mysql', content: { access_patterns: [AURORA_PATTERN] } },
  ],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 1 } },
      tco_analysis: { projected_monthly_cost: 50, cost_breakdown: [] },
      // #157 review round 2: the same group build_query_groups would build,
      // carrying the assignment's own routing reason via reason_index --
      // the fact extractPatterns/showPatternDetails must surface in the
      // Description column instead of the pattern's structural text.
      query_groups: [
        {
          group_name: 'wordpress.wp_posts',
          engines: ['aurora_mysql'],
          reasons: ['co-dependency group → aurora_mysql'],
          access_patterns: [
            { pattern_id: 'aur-q1', engine: 'aurora_mysql', reason_index: 0, query_ids: ['q1'] },
          ],
        },
      ],
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

let doc;

beforeAll(() => {
  doc = loadInteractiveReport(generateHTMLReport(reportData())).document;
});

describe('embedded extractPatterns/showPatternDetails for a real Aurora access pattern (#157)', () => {
  it('shows the source table as the destination, never a dash', () => {
    const container = doc.getElementById('access-patterns-container');
    const row = container.querySelector('tbody tr');
    expect(row.textContent).toContain('wp_posts');
    expect(row.textContent).not.toContain('—');
  });

  it('the pattern-detail modal shows the real table as the destination', () => {
    window.showPatternDetails('aur-q1');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).toContain('Destination Tablewp_posts');
    window.closePatternModal();
  });

  it('the pattern-detail modal shows the index used', () => {
    window.showPatternDetails('aur-q1');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).toContain('Index UsedPRIMARY');
    window.closePatternModal();
  });

  it('the pattern-detail modal shows the SQL shape (sql_text) as the source query', () => {
    window.showPatternDetails('aur-q1');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).toContain('SELECT * FROM wordpress.wp_posts WHERE id = ?');
    window.closePatternModal();
  });

  it('the pattern-detail modal resolves source_query_ids as SQL IDs', () => {
    window.showPatternDetails('aur-q1');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).toContain('SQL IDs (1)');
    window.closePatternModal();
  });

  it('the explorer row shows the assignment reason, not the structural text, in the Description column (#157 review round 2)', () => {
    const container = doc.getElementById('access-patterns-container');
    const row = container.querySelector('tbody tr');
    expect(row.textContent).toContain('co-dependency group → aurora_mysql');
    expect(row.textContent).not.toContain('SELECT on wordpress.wp_posts; filtered by id');
  });

  it('the pattern-detail modal shows the reason as the main description, with the structural text as a secondary line', () => {
    window.showPatternDetails('aur-q1');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).toContain('co-dependency group → aurora_mysql');
    expect(body).toContain('SELECT on wordpress.wp_posts; filtered by id');
    window.closePatternModal();
  });
});
