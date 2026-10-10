/**
 * #478: the standalone HTML export's "Aurora Design" section (buildAuroraDesign
 * in generateReportScript) -- tables, generated DDL, optimizations, migration
 * strategy and "queries on Aurora" grouped by source table, built from
 * DATA.schemaDesigns and the relational branch build_query_groups
 * (src/agents/referee/synthesis_report.py) already puts into
 * DATA.results.synthesis.query_groups.
 *
 * Two files, like ExportReport.cacheLayer.test.js: the embedded script
 * declares top-level consts, so it can only be loaded once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const AURORA_CONTENT = {
  source_database: 'wordpress',
  migration_strategy: 'carry_over',
  generated_ddl: 'CREATE TABLE wp_posts (ID BIGINT UNSIGNED, PRIMARY KEY (ID));',
  table_definitions: [
    {
      table_name: 'wp_posts',
      columns: [{ name: 'ID', aurora_type: 'BIGINT UNSIGNED' }],
      primary_key: ['ID'],
      indexes: ['CREATE INDEX idx_type ON wp_posts (post_type)'],
      foreign_keys: [],
    },
  ],
  optimizations: [
    { category: 'index', target: 'wp_posts', recommendation: 'Reorder the index', rationale: 'because' },
  ],
};

const baseData = (schemaDesigns, queryGroups) => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns,
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { aurora_mysql: 53, dynamodb: 54 } },
      tco_analysis: { projected_monthly_cost: 300, cost_breakdown: [] },
      query_groups: queryGroups,
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

describe('buildAuroraDesign (#478)', () => {
  let doc;

  beforeAll(() => {
    const schemaDesigns = [{ target_type: 'aurora_mysql', content: AURORA_CONTENT }];
    const queryGroups = [
      {
        group_name: 'wordpress.wp_posts',
        engines: ['aurora_mysql'],
        access_patterns: [
          {
            pattern_id: 'relational-q1',
            engine: 'aurora_mysql',
            operation: 'SELECT',
            design_rps: 4.5,
            description: 'relational core',
            in_scope: true,
            query_ids: ['q1'],
          },
        ],
        source_queries: [
          { query_id: 'q1', query_text: 'SELECT * FROM wp_posts WHERE ID = ?', query_type: 'SELECT' },
        ],
      },
    ];
    doc = loadInteractiveReport(generateHTMLReport(baseData(schemaDesigns, queryGroups))).document;
  });

  it('shows the Aurora engine display name', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('Aurora MySQL');
  });

  it('shows the table and its primary key/indexes', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('wp_posts');
    expect(container.textContent).toContain('Primary key');
    expect(container.textContent).toContain('idx_type');
  });

  it('shows the generated DDL', () => {
    const container = doc.getElementById('aurora-design-container');
    const pre = container.querySelector('pre');
    expect(pre.textContent).toContain('CREATE TABLE wp_posts');
  });

  it('shows the optimization', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('Reorder the index');
  });

  it('shows the query routed to Aurora, grouped by source table', () => {
    const container = doc.getElementById('aurora-design-container');
    expect(container.textContent).toContain('wordpress.wp_posts');
    const rows = container.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('SELECT * FROM wp_posts WHERE ID = ?');
    expect(rows[0].textContent).toContain('4.50');
    expect(rows[0].textContent).toContain('relational core');
  });
});
