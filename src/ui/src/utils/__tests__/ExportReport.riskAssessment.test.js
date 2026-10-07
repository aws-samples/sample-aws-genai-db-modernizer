/**
 * #373: the standalone HTML export (generateHTMLReport, ExportReport.js) never
 * rendered synthesis.risk_assessment at all -- no risk list, no severity
 * grouping, no overall_risk_level, not even for a CRITICAL risk. This file
 * exercises the embedded client script's buildRiskAssessment(), checking it
 * groups by severity (CRITICAL/HIGH/MEDIUM/LOW, each shown even when empty),
 * treats CRITICAL at least as severely as HIGH (#270), counts the same risks
 * the decision/engineering reports do (filtered_risks parity, #201), skips a
 * mitigation that only repeats the risk body, and lists resolved risks
 * separately with their from -> to engines and reason.
 *
 * One `generateHTMLReport`/script load for the whole file (its embedded script
 * declares top-level `const`s in the jsdom global scope, so it can only be
 * loaded once per test file, see ExportReport.riskAssessmentEmpty.test.js for
 * the "nothing to show" case in its own file).
 */
import { generateHTMLReport } from '../ExportReport';

const data = {
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { dynamodb: 10 } },
      tco_analysis: { projected_monthly_cost: 100, cost_breakdown: [] },
      risk_assessment: {
        overall_risk_level: 'CRITICAL',
        risks: [
          { risk_id: 'RISK-900', severity: 'CRITICAL', description: '[dynamodb] no serving engine for an in-scope query' },
          { risk_id: 'RISK-001', severity: 'HIGH', description: '[dynamodb] no server-side aggregation', mitigation: 'Pre-compute aggregates' },
          // Contentless after its [engine] prefix -- renderers.filtered_risks drops this too (#201).
          { risk_id: 'RISK-002', severity: 'HIGH', description: '[documentdb] unknown:' },
          // Mitigation repeats the body -- renderers._repeats would state this once, not twice.
          { risk_id: 'RISK-003', severity: 'MEDIUM', description: '[opensearch] Keep it on Aurora MySQL.', mitigation: 'Keep it on Aurora MySQL.' },
        ],
        resolved_risks: [
          {
            engine: 'opensearch', resolved_on: 'aurora_mysql', severity: 'HIGH',
            description: '[opensearch] cross-index joins', reason: 'the queries run as SQL on Aurora MySQL',
          },
        ],
      },
    },
  },
};

/** Mirrors exportEscaping.test.js's loadInteractiveReport (own copy, see header). */
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
  return window.document;
};

describe('buildRiskAssessment (#373)', () => {
  let container;
  beforeAll(() => {
    const doc = loadInteractiveReport(generateHTMLReport(data));
    container = doc.getElementById('risk-assessment-container');
  });

  it('shows the section and the overall risk level, styled as severely as HIGH (#270)', () => {
    expect(container.style.display).not.toBe('none');
    const overallBadge = [...container.querySelectorAll('span.badge')].find((b) => b.textContent.startsWith('Overall risk'));
    expect(overallBadge.textContent).toBe('Overall risk: CRITICAL');
    expect(overallBadge.style.backgroundColor.toLowerCase()).toBe('rgb(209, 50, 18)'); // #d13212, the HIGH/error color
  });

  it('groups by severity in CRITICAL/HIGH/MEDIUM/LOW order, each shown even when empty', () => {
    const headings = [...container.querySelectorAll('div')]
      .map((d) => d.textContent)
      .filter((t) => /^(CRITICAL|HIGH|MEDIUM|LOW) \(\d+\)$/.test(t));
    // RISK-002 (contentless) is dropped from HIGH's count -- 1, not 2.
    expect(headings).toEqual(['CRITICAL (1)', 'HIGH (1)', 'MEDIUM (1)', 'LOW (0)']);
    expect(container.textContent).toContain('No open risks at this severity.');
  });

  it('CRITICAL and HIGH share the same (error/red) styling, never a plain warning', () => {
    const critHeading = [...container.querySelectorAll('div')].find((d) => d.textContent === 'CRITICAL (1)');
    const highHeading = [...container.querySelectorAll('div')].find((d) => d.textContent === 'HIGH (1)');
    expect(critHeading.style.color.toLowerCase()).toBe('rgb(209, 50, 18)');
    expect(highHeading.style.color.toLowerCase()).toBe('rgb(209, 50, 18)');
  });

  it('MEDIUM text is dark brown, not the raw #ffc107 amber (#421: axe color-contrast, 1.63:1 on white)', () => {
    // #5b4708 on white is the decision report's own .sev.MEDIUM text color
    // (src/report/renderers.py), which passes at 5.48:1 paired with #ffc107.
    const medHeading = [...container.querySelectorAll('div')].find((d) => d.textContent === 'MEDIUM (1)');
    expect(medHeading.style.color.toLowerCase()).toBe('rgb(91, 71, 8)'); // #5b4708
    expect(medHeading.style.color.toLowerCase()).not.toBe('rgb(255, 193, 7)'); // #ffc107
  });

  it('drops a contentless risk ("unknown:" with nothing after it) from the list, matching filtered_risks (#201)', () => {
    expect(container.textContent).not.toContain('RISK-002');
    expect(container.textContent).toContain('no serving engine for an in-scope query');
    expect(container.textContent).toContain('no server-side aggregation');
    expect(container.textContent).toContain('Pre-compute aggregates');
  });

  it('does not repeat a mitigation that already appears in the risk body', () => {
    // RISK-003's body and mitigation are the same text -- it must appear once, not
    // twice (no "Mitigation:" line for it), while RISK-001's genuinely different
    // mitigation still gets its own "Mitigation:" line.
    const occurrences = container.textContent.split('Keep it on Aurora MySQL.').length - 1;
    expect(occurrences).toBe(1);
    expect(container.textContent).toContain('Mitigation: Pre-compute aggregates');
  });

  it('lists resolved risks separately, with their from -> to engines and reason', () => {
    expect(container.textContent).toContain('1 resolved by the assignment');
    expect(container.textContent).toContain('cross-index joins');
    expect(container.textContent).toContain('Resolved because the queries run as SQL on Aurora MySQL');
  });
});
