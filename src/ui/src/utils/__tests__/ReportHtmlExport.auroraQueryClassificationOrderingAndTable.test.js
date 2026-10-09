/**
 * #478: ReportHtmlExport.js's "Query Classification" section
 * must not let a collector/utility "blame" group (see AURORA_BLAME_LABELS in
 * ../auroraDesign) occupy one of the top-10 slots ahead of a group that
 * names a real table, and its per-pattern "Table" column must fall back to
 * the group's own name for a relational engine's access patterns, which
 * carry no `table_name` of their own (#157 adds real ones later).
 */
import { buildReportHtml } from '../ReportHtmlExport';

const t = (key) => key;

const resultsData = (queryGroups) => ({
  synthesis: {
    database_name: 'wordpress',
    ranking: [{ target: 'aurora_mysql', workload_percent: 100 }],
    query_groups: queryGroups,
  },
});

const parse = (htmlString) => new DOMParser().parseFromString(htmlString, 'text/html');

const realGroup = (name, rps) => ({
  group_name: name,
  engines: ['aurora_mysql'],
  access_patterns: [
    { engine: 'aurora_mysql', operation: 'SELECT', design_rps: rps, query_ids: [`${name}-q1`] },
  ],
  source_queries: [{ query_id: `${name}-q1` }],
  total_design_rps: rps,
});

describe('Query Classification top-10 ordering never leads with a blame group', () => {
  it('sorts both blame labels after every real-table group regardless of calls/s', () => {
    const groups = [
      {
        group_name: 'Table not identified by the collector',
        engines: ['aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 999, query_ids: ['b1'] },
        ],
        source_queries: [{ query_id: 'b1' }],
        total_design_rps: 999,
      },
      {
        group_name: 'Utility and session statements',
        engines: ['aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 998, query_ids: ['b2'] },
        ],
        source_queries: [{ query_id: 'b2' }],
        total_design_rps: 998,
      },
      realGroup('wordpress.wp_posts', 1),
    ];
    const doc = parse(
      buildReportHtml({ resultsData: resultsData(groups), jobId: 'job', t })
    );
    const headings = [...doc.querySelectorAll('h3')].map((h) => h.textContent);
    const realIdx = headings.findIndex((h) => h.includes('wordpress.wp_posts'));
    const blameIdxs = headings
      .map((h, i) => [h, i])
      .filter(([h]) => h.includes('Table not identified') || h.includes('Utility and session'))
      .map(([, i]) => i);
    expect(realIdx).toBeGreaterThan(-1);
    blameIdxs.forEach((i) => expect(i).toBeGreaterThan(realIdx));
  });

  it('never excludes a real group from the top 10 in favor of a blame group', () => {
    const groups = [
      {
        group_name: 'Table not identified by the collector',
        engines: ['aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 999, query_ids: ['b1'] },
        ],
        source_queries: [{ query_id: 'b1' }],
        total_design_rps: 999,
      },
      ...Array.from({ length: 10 }, (_, i) => realGroup(`wordpress.t${i}`, 10 - i)),
    ];
    const doc = parse(
      buildReportHtml({ resultsData: resultsData(groups), jobId: 'job', t })
    );
    const headings = [...doc.querySelectorAll('h3')].map((h) => h.textContent);
    for (let i = 0; i < 10; i++) {
      expect(headings.some((h) => h.includes(`wordpress.t${i}`))).toBe(true);
    }
    expect(headings.some((h) => h.includes('Table not identified'))).toBe(false);
  });
});

describe('Query Classification "Table" column falls back to the group name', () => {
  it('shows the group name when the access pattern has no table_name of its own', () => {
    const doc = parse(
      buildReportHtml({ resultsData: resultsData([realGroup('wordpress.wp_posts', 5)]), jobId: 'job', t })
    );
    const codes = [...doc.querySelectorAll('td code')].map((el) => el.textContent);
    expect(codes).toContain('wordpress.wp_posts');
  });
});

describe('Query Classification "Description" column resolves a deduplicated reason', () => {
  it('reads through reason_index + the group\'s reasons list (#478)', () => {
    const group = {
      group_name: 'wordpress.wp_posts',
      engines: ['aurora_mysql'],
      reasons: ['highest confidence for aurora_mysql'],
      access_patterns: [
        { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 1, reason_index: 0, query_ids: ['q1'] },
      ],
      source_queries: [{ query_id: 'q1' }],
      total_design_rps: 1,
    };
    const doc = parse(buildReportHtml({ resultsData: resultsData([group]), jobId: 'job', t }));
    const rows = [...doc.querySelectorAll('tbody tr')];
    const row = rows.find((r) => r.textContent.includes('SELECT'));
    expect(row.textContent).toContain('highest confidence for aurora_mysql');
    expect(row.textContent).not.toContain('undefined');
  });
});
