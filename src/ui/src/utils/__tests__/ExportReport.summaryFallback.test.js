/**
 * #269: the standalone HTML export must say when the executive summary shown
 * is the deterministic fallback (a grounding check rejected the generated
 * text, same fact the Decision Report already states via
 * `summary_source === "deterministic_fallback"`, src/report/decision_report.py),
 * in the same wording the Decision Report uses, with the same
 * check-specific reason (`fallbackWithheldReason`, ported from
 * `formatting.fallback_withheld_reason`).
 *
 * No script execution needed here -- the fallback note is part of the shell
 * `generateHTMLReport` returns, built the same way `safeSummary` is.
 */
import { generateHTMLReport } from '../ExportReport';

const NOTICE = 'The generated narrative was withheld because it';

const baseData = (synthesisOverrides = {}) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'This summary is built from the final records.',
      reality_check: { after_distribution: { dynamodb: 100 } },
      tco_analysis: { projected_monthly_cost: 100 },
      ...synthesisOverrides,
    },
  },
});

describe('generateHTMLReport executive summary fallback note (#269)', () => {
  it('shows no fallback note when summary_source is "llm"', () => {
    const markup = generateHTMLReport(baseData({ summary_source: 'llm' }));
    expect(markup).not.toContain(NOTICE);
  });

  it('shows no fallback note when summary_source is missing (older artifact)', () => {
    const markup = generateHTMLReport(baseData());
    expect(markup).not.toContain(NOTICE);
  });

  it('names the check-specific reason when summary_source is deterministic_fallback', () => {
    const markup = generateHTMLReport(baseData({
      summary_source: 'deterministic_fallback',
      summary_validation_warnings: ['[high confidence] names an internal field, "tco_analysis".'],
    }));
    // escapeHtml encodes the apostrophe (&#39;), so the assertion is split around it.
    expect(markup).toContain('The generated narrative was withheld because it named one of this codebase');
    expect(markup).toContain('internal field names; this summary is built from the effective assignment.');
    expect(markup).not.toContain('Why the AI summary was replaced');
    expect(markup).not.toContain('Original AI-generated summary');
  });

  it('falls back to the default reason when there are no warnings', () => {
    const markup = generateHTMLReport(baseData({ summary_source: 'deterministic_fallback' }));
    expect(markup).toContain('named a table under an engine that does not serve it');
  });

  it('never lets a hostile warning reach the output -- it only ever selects a fixed, safe reason', () => {
    const markup = generateHTMLReport(baseData({
      summary_source: 'deterministic_fallback',
      summary_validation_warnings: ['<script>alert(1)</script>'],
    }));
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    // Chart.js CDN tag + the one inline report script -- nothing injected.
    expect(doc.querySelectorAll('script')).toHaveLength(2);
    // The hostile warning only ever selects which fixed, safe reason clause is
    // shown -- it never reaches the note itself (it is embedded elsewhere, in
    // DATA, for the client script's own unrelated use).
    [...doc.querySelectorAll('.stat-note')].forEach((note) => {
      expect(note.textContent).not.toContain('alert(1)');
    });
    expect(markup).toContain('named a table under an engine that does not serve it');
  });
});
