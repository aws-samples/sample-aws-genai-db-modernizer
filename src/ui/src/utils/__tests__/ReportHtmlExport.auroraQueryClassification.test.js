/**
 * #478: ReportHtmlExport.js's "Query Classification" section
 * must use display engine names (not raw contract keys) and say "queries"
 * for a group served only by a relational engine (Aurora's query_groups
 * entries are built from the assignment, not a real access pattern -- it
 * has none; #157 adds real ones later).
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

describe('Query Classification wording for a relational-only group', () => {
  it('says "queries", not "patterns", and uses the display name badge', () => {
    const doc = parse(
      buildReportHtml({
        resultsData: resultsData([
          {
            group_name: 'wordpress.wp_posts',
            engines: ['aurora_mysql'],
            access_patterns: [
              { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 1, query_ids: ['q1'] },
            ],
            source_queries: [{ query_id: 'q1' }],
            total_design_rps: 1,
          },
        ]),
        jobId: 'job',
        t,
      })
    );
    const heading = [...doc.querySelectorAll('h3')].find((h) =>
      h.textContent.includes('wordpress.wp_posts')
    );
    expect(heading.textContent).toContain('1 queries');
    expect(heading.textContent).not.toContain('patterns');
    const groupCard = heading.closest('div');
    expect(groupCard.textContent).toContain('Aurora MySQL');
    expect(groupCard.querySelector('.badge').textContent).toBe('Aurora MySQL');
  });

  it('still says "patterns" for a non-relational engine, with its display name', () => {
    const doc = parse(
      buildReportHtml({
        resultsData: resultsData([
          {
            group_name: 'Option lookups',
            engines: ['dynamodb'],
            access_patterns: [
              { engine: 'dynamodb', operation: 'Query', design_rps: 1, query_ids: ['q1'] },
            ],
            source_queries: [{ query_id: 'q1' }],
            total_design_rps: 1,
          },
        ]),
        jobId: 'job',
        t,
      })
    );
    const heading = [...doc.querySelectorAll('h3')].find((h) =>
      h.textContent.includes('Option lookups')
    );
    expect(heading.textContent).toContain('1 patterns');
    const groupCard = heading.closest('div');
    expect(groupCard.querySelector('.badge').textContent).toBe('DynamoDB');
  });

  it('says "queries" only when every engine in the group is relational', () => {
    const doc = parse(
      buildReportHtml({
        resultsData: resultsData([
          {
            group_name: 'wordpress.wp_posts',
            engines: ['elasticache', 'aurora_mysql'],
            access_patterns: [
              { engine: 'elasticache', operation: 'HGETALL', design_rps: 1, query_ids: ['q1'] },
              { engine: 'aurora_mysql', operation: 'SELECT', design_rps: 1, query_ids: ['q2'] },
            ],
            source_queries: [{ query_id: 'q1' }, { query_id: 'q2' }],
            total_design_rps: 2,
          },
        ]),
        jobId: 'job',
        t,
      })
    );
    const heading = [...doc.querySelectorAll('h3')].find((h) =>
      h.textContent.includes('wordpress.wp_posts')
    );
    expect(heading.textContent).toContain('2 patterns');
  });
});
