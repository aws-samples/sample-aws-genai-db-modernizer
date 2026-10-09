/**
 * #478: buildAuroraDesign with no Aurora engine in the assessment -- separate
 * file from ExportReport.auroraDesign.test.js because the embedded script
 * declares top-level consts, so it can only be loaded once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const baseData = (schemaDesigns) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns,
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { dynamodb: 54 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
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

describe('buildAuroraDesign with no Aurora engine in the assessment (#478)', () => {
  it('omits the whole section entirely, no error', () => {
    const schemaDesigns = [{ target_type: 'dynamodb', content: { table_definitions: [] } }];
    const doc = loadInteractiveReport(generateHTMLReport(baseData(schemaDesigns))).document;
    const container = doc.getElementById('aurora-design-container');
    const section = container.closest('.section');
    expect(section.style.display).toBe('none');
    expect(container.innerHTML).toBe('');
  });
});
