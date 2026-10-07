/**
 * #334: generateHTMLReport's Executive Summary never showed the current-cost
 * baseline or savings at all, and a report with no baseline (current_cost_known:
 * false, or an older report whose current_monthly_cost is a bare 0 placeholder)
 * must say so instead of "$0.00/mo"/"0%" (indistinguishable from a genuine
 * zero-savings outcome). These are plain shell interpolations (no script
 * execution needed, unlike the risk-assessment/cost-breakdown containers).
 */
import { generateHTMLReport } from '../ExportReport';

const baseData = (tco) => ({
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
      tco_analysis: tco,
    },
  },
});

const statValue = (doc, label) => [...doc.querySelectorAll('.stat-card')]
  .find((c) => c.querySelector('.stat-label')?.textContent === label)
  ?.querySelector('.stat-value')?.textContent;

describe('generateHTMLReport Executive Summary cost baseline (#334)', () => {
  it('shows the real current cost and savings when the baseline is known', () => {
    const doc = new DOMParser().parseFromString(
      generateHTMLReport(baseData({
        current_cost_known: true,
        current_monthly_cost: 500,
        projected_monthly_cost: 280,
        savings_percent: 44,
        cost_breakdown: [],
      })),
      'text/html',
    );
    expect(statValue(doc, 'Current Monthly Cost')).toBe('$500.00/mo');
    expect(statValue(doc, 'Savings')).toBe('44%');
  });

  it('says the baseline is unknown, not "$0.00"/"0%", when current_cost_known is false', () => {
    const doc = new DOMParser().parseFromString(
      generateHTMLReport(baseData({
        current_cost_known: false,
        current_monthly_cost: 0,
        projected_monthly_cost: 823.72,
        savings_percent: 0,
        cost_breakdown: [],
      })),
      'text/html',
    );
    expect(statValue(doc, 'Current Monthly Cost')).toBe('source cost not provided');
    expect(statValue(doc, 'Savings')).toBe('not available');
  });

  it('treats an older report (no current_cost_known field, 0 cost) as unknown too (real wordpress sample shape)', () => {
    const doc = new DOMParser().parseFromString(
      generateHTMLReport(baseData({
        current_monthly_cost: 0.0,
        projected_monthly_cost: 823.72,
        savings_percent: 0.0,
        cost_breakdown: [],
      })),
      'text/html',
    );
    expect(statValue(doc, 'Current Monthly Cost')).toBe('source cost not provided');
    expect(statValue(doc, 'Savings')).toBe('not available');
  });

  it('an older report with no current_cost_known field but a real nonzero cost still shows it', () => {
    const doc = new DOMParser().parseFromString(
      generateHTMLReport(baseData({
        current_monthly_cost: 500,
        projected_monthly_cost: 280,
        savings_percent: 44,
        cost_breakdown: [],
      })),
      'text/html',
    );
    expect(statValue(doc, 'Current Monthly Cost')).toBe('$500.00/mo');
    expect(statValue(doc, 'Savings')).toBe('44%');
  });
});
