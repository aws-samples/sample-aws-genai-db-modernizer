/**
 * #157 review, S1: Aurora's '__seq_scan__' sentinel is script-internal and
 * must never reach the reader as-is, in either the HTML export's
 * pattern-detail modal or the live Results page (AnalysisResults-02.js's
 * auroraIndexUsedDisplay -- same rule, kept in parity by hand per this
 * file's usual note).
 *
 * Three cases read as different facts and must look different:
 * - the predicate is unknown (needs_judgment) -- nothing can be claimed
 *   either way, so the Index Used field is hidden entirely;
 * - the query's table could not be identified at all (load_testable=false
 *   with that reason, round 2 of the review) -- there was nothing to
 *   resolve an index against in the first place, hidden the same way;
 * - the predicate is confirmed empty (not needs_judgment, table identified)
 *   -- a sequential scan really is the resolved answer, shown as
 *   "None (sequential scan)".
 */
import { generateHTMLReport } from '../ExportReport';

const UNKNOWN_PREDICATE_PATTERN = {
  pattern_id: 'aur-unknown-predicate',
  description: 'SELECT on wordpress.wp_options',
  operation: 'SELECT',
  sql_text: 'SELECT * FROM wordpress.wp_options',
  source_tables: ['wordpress.wp_options'],
  source_query_ids: ['q1'],
  filter_columns: [],
  index_used: '__seq_scan__',
  design_rps: 1.0,
  script_derived: false,
  needs_judgment: true,
  judgment_reason: 'Filter predicate unknown; the collector did not record one for this query.',
  in_scope: true,
  load_testable: true,
};

const CONFIRMED_EMPTY_PREDICATE_PATTERN = {
  pattern_id: 'aur-confirmed-scan',
  description: 'SELECT on wordpress.wp_comments; aggregated',
  operation: 'SELECT',
  sql_text: 'SELECT count(*) FROM wordpress.wp_comments',
  source_tables: ['wordpress.wp_comments'],
  source_query_ids: ['q2'],
  filter_columns: [],
  index_used: '__seq_scan__',
  design_rps: 1.0,
  script_derived: true,
  needs_judgment: false,
  judgment_reason: 'No filter predicate; a full scan is the expected plan.',
  in_scope: true,
  load_testable: true,
};

const UNIDENTIFIED_TABLE_PATTERN = {
  pattern_id: 'aur-unidentified-table',
  description: 'OTHER on unknown',
  operation: 'OTHER',
  sql_text: 'SET autocommit=1',
  source_tables: ['unknown'],
  source_query_ids: ['q3'],
  filter_columns: [],
  index_used: '__seq_scan__',
  design_rps: 0.1,
  script_derived: true,
  needs_judgment: false,
  judgment_reason:
    "None of this query's tables could be identified in the Aurora design (commonly the "
    + "collector's own 'unknown' placeholder); no index can be resolved and this pattern "
    + 'cannot be load-tested.',
  in_scope: true,
  load_testable: false,
  load_testable_reason:
    "None of this query's tables could be identified in the Aurora design (commonly the "
    + "collector's own 'unknown' placeholder); no index can be resolved and this pattern "
    + 'cannot be load-tested.',
};

const reportData = () => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [
    {
      target_type: 'aurora_mysql',
      content: {
        access_patterns: [
          UNKNOWN_PREDICATE_PATTERN,
          CONFIRMED_EMPTY_PREDICATE_PATTERN,
          UNIDENTIFIED_TABLE_PATTERN,
        ],
      },
    },
  ],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 2 } },
      tco_analysis: { projected_monthly_cost: 50, cost_breakdown: [] },
      query_groups: [],
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

describe('embedded showPatternDetails never shows the raw __seq_scan__ sentinel (#157 review, S1)', () => {
  it('hides Index Used entirely when the predicate is unknown', () => {
    window.showPatternDetails('aur-unknown-predicate');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).not.toContain('__seq_scan__');
    expect(body).not.toContain('Index Used');
    window.closePatternModal();
  });

  it('shows "None (sequential scan)" when the predicate is confirmed empty', () => {
    window.showPatternDetails('aur-confirmed-scan');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).not.toContain('__seq_scan__');
    expect(body).toContain('Index UsedNone (sequential scan)');
    window.closePatternModal();
  });

  it('hides Index Used entirely when the query\'s table could not be identified (round 2)', () => {
    window.showPatternDetails('aur-unidentified-table');
    const body = doc.getElementById('pattern-modal-body').textContent;
    expect(body).not.toContain('__seq_scan__');
    expect(body).not.toContain('Index Used');
    window.closePatternModal();
  });
});
