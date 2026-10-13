/**
 * #573: the Query Flow labels printed each engine's owned query COUNT with a
 * "%" sign ("Aurora PostgreSQL (897.0%)"), a figure no other deliverable shows.
 * Each label now states the count and its share of the owned queries, which is
 * what the decision report, deck and engineering report show for the engine.
 *
 * One `generateHTMLReport`/script load for the whole file (its embedded script
 * declares top-level `const`s in the jsdom global scope).
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
      database_name: 'discourse',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_postgresql: 897, dynamodb: 563, documentdb: 1 } },
      tco_analysis: { projected_monthly_cost: 100, cost_breakdown: [] },
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

describe('buildQueryFlow labels (#573)', () => {
  let labels;
  beforeAll(() => {
    const doc = loadInteractiveReport(generateHTMLReport(data));
    labels = [...doc.querySelectorAll('#query-flow-container [data-figure="engine-queries"]')];
  });

  it('labels every owner engine, largest first, with a data-engine hook', () => {
    expect(labels.map((l) => l.getAttribute('data-engine'))).toEqual(['aurora_postgresql', 'dynamodb', 'documentdb']);
  });

  it('states the query count and its share, never the count as a percentage', () => {
    expect(labels.map((l) => l.textContent)).toEqual([
      'Aurora PostgreSQL (897 queries, 61.4%)',
      'DynamoDB (563 queries, 38.5%)',
      'DocumentDB (1 query, 0.1%)',
    ]);
    expect(labels.some((l) => l.textContent.includes('897.0%'))).toBe(false);
  });
});
