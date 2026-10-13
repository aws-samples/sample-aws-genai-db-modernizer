/**
 * The plain-language workload-fit definition appears once in every UI
 * deliverable that shows the routing fit score (bug: Deliverables present a
 * fit score as a confidence percentage).
 *
 * The decision report, engineering report and deck are covered by
 * `tests/unit/report/test_workload_fit_definition.py`; this file covers the
 * three UI deliverables: the standalone analysis-report HTML export
 * (`ExportReport.js`'s `generateHTMLReport`, synced into
 * `src/report/templates/analysis_report.js` and shipped with every job as
 * `*_analysis-report_*.html` -- also the UI's "Download Report" button), the
 * approval gate (`AssignmentGate.js`, via its i18n description), and the live
 * React results page's query-journey detail (`AnalysisResults-02.js`), which
 * imports and renders the shared constant directly.
 *
 * `ReportHtmlExport.js`'s `buildReportHtml` is NOT exercised here: verified it
 * has had no live caller since #363 ("chore(ui): remove the legacy report
 * page", which deleted `ReportResults.js`, its only importer -- only its own
 * test files and `escapeHtml.js`'s unrelated re-export name it now). It is
 * dead code, left alone rather than fixed, since fixing a document nothing
 * renders would not close the actual gap (the live `ExportReport.js` export
 * never stating the definition).
 */
import { generateHTMLReport } from '../ExportReport';
import { WORKLOAD_FIT_DEFINITION } from '../workloadFitLabels';
import en from '../../locales/en.json';

const QID = 'q-fit-definition';

const exportData = () => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: {
    items: [
      {
        query_id: QID,
        source: { query_type: 'SELECT', tables_accessed: ['wp_posts'] },
        assignment: { assigned_engine: 'aurora_mysql', confidence: 90, in_scope: true },
      },
    ],
  },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'This summary is built from the final records.',
      reality_check: { after_distribution: { aurora_mysql: 100 } },
      tco_analysis: { projected_monthly_cost: 100 },
    },
  },
});

/**
 * Run the exported report's own embedded <script> in this test's jsdom
 * window, the way a browser does when the file is opened. Mirrors
 * exportEscaping.test.js's loadInteractiveReport; kept local and used exactly
 * once (the script declares top-level consts, so a second load in the same
 * realm throws "already declared").
 */
const loadInteractiveReport = (markup) => {
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter((el) => !el.src);
  expect(inline).toHaveLength(1);
  const code = inline[0].textContent;
  parsed.querySelectorAll('script').forEach((el) => el.remove());

  window.Chart = class {
    destroy() {}
  };
  jest.spyOn(console, 'log').mockImplementation(() => {});
  document.head.innerHTML = parsed.head.innerHTML;
  document.body.innerHTML = parsed.body.innerHTML;
  const script = document.createElement('script');
  script.textContent = code;
  document.body.appendChild(script);
  document.dispatchEvent(new Event('DOMContentLoaded'));
  return window;
};

describe('UI analysis export states the definition once (ExportReport.js)', () => {
  it('shows the definition in the query-journey modal, the figure\'s only appearance in this document', () => {
    const win = loadInteractiveReport(generateHTMLReport(exportData()));
    win.showQueryJourney(QID);
    const body = win.document.getElementById('query-modal-body').textContent;
    expect(body).toContain('Workload Fit');
    expect(body).toContain(WORKLOAD_FIT_DEFINITION);
    // Exactly once: the only fit figure in the whole exported document.
    expect(body.split(WORKLOAD_FIT_DEFINITION)).toHaveLength(2);
  });
});

describe('Approval gate states the definition (AssignmentGate.js, via i18n)', () => {
  it('includes the shared definition in the query table description', () => {
    expect(en['assignment-gate.table.description']).toContain(WORKLOAD_FIT_DEFINITION);
  });
});

describe('React results page states the definition (AnalysisResults-02.js)', () => {
  it('imports and renders the shared workload-fit definition', async () => {
    const fs = require('fs');
    const path = require('path');
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'pages', 'AnalysisResults-02.js'),
      'utf8',
    );
    expect(source).toMatch(/import\s*{\s*WORKLOAD_FIT_DEFINITION\s*}\s*from\s*"\.\.\/utils\/workloadFitLabels"/);
    expect(source).toMatch(/WORKLOAD_FIT_DEFINITION/);
  });
});
