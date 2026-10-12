/**
 * #270 round-1 review of PR #506: a risk whose severity is none of
 * CRITICAL/HIGH/MEDIUM/LOW used to silently count as LOW in the HTML
 * export's embedded buildRiskAssessment() -- the same risk the deck labels
 * "UNRATED" (pptx_report.py) read here as "known to be low severity"
 * instead of simply unrated. This file exercises that case on its own (see
 * ExportReport.riskAssessment.test.js's header for why only one
 * generateHTMLReport()/script load is possible per test file).
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
        overall_risk_level: 'MEDIUM',
        risks: [
          { risk_id: 'RISK-001', severity: 'MEDIUM', description: '[dynamodb] a real risk' },
          { risk_id: 'RISK-002', severity: 'WEIRD', description: '[dynamodb] an unrated risk' },
        ],
        resolved_risks: [],
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

describe('buildRiskAssessment UNRATED handling (#270 round-1 review)', () => {
  let container;
  beforeAll(() => {
    const doc = loadInteractiveReport(generateHTMLReport(data));
    container = doc.getElementById('risk-assessment-container');
  });

  it('shows an UNRATED section, appended after the always-shown four, only because an unrated risk is present', () => {
    const headings = [...container.querySelectorAll('div')]
      .map((d) => d.textContent)
      .filter((t) => /^(CRITICAL|HIGH|MEDIUM|LOW|UNRATED) \(\d+\)$/.test(t));
    expect(headings).toEqual(['CRITICAL (0)', 'HIGH (0)', 'MEDIUM (1)', 'LOW (0)', 'UNRATED (1)']);
  });

  it('lists the unrated risk under the UNRATED heading, not counted into LOW', () => {
    expect(container.textContent).toContain('an unrated risk');
    expect(container.textContent).toContain('LOW (0)');
  });
});
