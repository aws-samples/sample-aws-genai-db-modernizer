/**
 * #373: buildRiskAssessment must hide its section entirely when there is
 * nothing to show (no overall_risk_level, no open risks, no resolved risks) --
 * a legacy report.json written before risk_assessment existed, for example.
 * Own file: see ExportReport.riskAssessment.test.js's header for why only one
 * script load is possible per test file.
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
      // No risk_assessment at all.
    },
  },
};

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

describe('buildRiskAssessment with no risk_assessment at all (#373)', () => {
  it('hides the section instead of showing an empty shell', () => {
    const doc = loadInteractiveReport(generateHTMLReport(data));
    const container = doc.getElementById('risk-assessment-container');
    expect(container.style.display).toBe('none');
    expect(container.innerHTML).toBe('');
  });
});
