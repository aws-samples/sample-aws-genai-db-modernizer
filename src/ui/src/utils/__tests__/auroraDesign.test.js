/**
 * #478: Aurora has no `access_patterns` field (Aurora carries tables over 1:1;
 * #157 adds real access patterns later), so the Results page can't reach it
 * through the access pattern explorer. These tests cover the data-shaping
 * that builds the "Aurora design" section instead, straight from the schema
 * design's `table_definitions`/`generated_ddl`/`optimizations` and the
 * synthesis report's `query_groups` (which the relational branch in
 * `build_query_groups` now populates for Aurora from the assignment).
 */
import {
  isAuroraEngine,
  buildAuroraTables,
  buildAuroraQueryGroups,
  buildAuroraDesign,
  buildAuroraDesigns,
  reasonTextFor,
} from '../auroraDesign';

const AURORA_CONTENT = {
  source_database: 'wordpress',
  migration_strategy: 'carry_over',
  generated_ddl: 'CREATE TABLE wp_posts (...);',
  table_definitions: [
    {
      table_name: 'wp_posts',
      columns: [
        { name: 'ID', aurora_type: 'BIGINT UNSIGNED', source_type: 'integer' },
        { name: 'post_title', aurora_type: 'VARCHAR(255)', source_type: 'string' },
      ],
      primary_key: ['ID'],
      indexes: ['CREATE INDEX type_status_date ON wp_posts (post_type, post_status)'],
      foreign_keys: [],
    },
    {
      table_name: 'wp_postmeta',
      columns: [{ name: 'meta_id', aurora_type: 'BIGINT UNSIGNED', source_type: 'integer' }],
      primary_key: ['meta_id'],
      indexes: [],
      foreign_keys: ['ALTER TABLE wp_postmeta ADD FOREIGN KEY (post_id) REFERENCES wp_posts(ID)'],
    },
  ],
  optimizations: [
    { category: 'index', target: 'wp_posts', recommendation: 'Reorder the index', rationale: 'because' },
  ],
  app_layer_notes: [],
};

const QUERY_GROUPS = [
  {
    group_name: 'wordpress.wp_posts',
    engines: ['aurora_mysql'],
    access_patterns: [
      {
        pattern_id: 'relational-q1',
        engine: 'aurora_mysql',
        operation: 'SELECT',
        table_name: 'wp_posts',
        design_rps: 4.5,
        description: 'relational core',
        in_scope: true,
        query_ids: ['q1'],
      },
    ],
    source_queries: [
      {
        query_id: 'q1',
        query_text: 'SELECT * FROM wp_posts WHERE ID = ?',
        query_type: 'SELECT',
        linked_patterns: ['relational-q1'],
      },
    ],
    total_design_rps: 4.5,
  },
  {
    group_name: 'DynamoDB group',
    engines: ['dynamodb'],
    access_patterns: [{ pattern_id: 'DDB-AP-1', engine: 'dynamodb', query_ids: ['q2'] }],
    source_queries: [{ query_id: 'q2', query_text: 'GetItem', query_type: 'GetItem' }],
    total_design_rps: 10,
  },
];

describe('isAuroraEngine', () => {
  test('recognizes both Aurora engines and the legacy bare key', () => {
    expect(isAuroraEngine('aurora_mysql')).toBe(true);
    expect(isAuroraEngine('aurora_postgresql')).toBe(true);
    expect(isAuroraEngine('aurora')).toBe(true);
    expect(isAuroraEngine('dynamodb')).toBe(false);
    expect(isAuroraEngine(undefined)).toBe(false);
  });
});

describe('buildAuroraTables', () => {
  test('maps table_definitions to compact table entries', () => {
    const tables = buildAuroraTables(AURORA_CONTENT);
    expect(tables).toHaveLength(2);
    expect(tables[0]).toEqual({
      tableName: 'wp_posts',
      columns: [
        { name: 'ID', auroraType: 'BIGINT UNSIGNED', sourceType: 'integer' },
        { name: 'post_title', auroraType: 'VARCHAR(255)', sourceType: 'string' },
      ],
      primaryKey: ['ID'],
      indexes: ['CREATE INDEX type_status_date ON wp_posts (post_type, post_status)'],
      foreignKeys: [],
    });
    expect(tables[1].foreignKeys).toEqual([
      'ALTER TABLE wp_postmeta ADD FOREIGN KEY (post_id) REFERENCES wp_posts(ID)',
    ]);
  });

  test('empty when there are no table_definitions', () => {
    expect(buildAuroraTables({})).toEqual([]);
    expect(buildAuroraTables(undefined)).toEqual([]);
  });
});

describe('buildAuroraQueryGroups', () => {
  test('only includes groups carrying patterns for the requested engine', () => {
    const groups = buildAuroraQueryGroups('aurora_mysql', QUERY_GROUPS);
    expect(groups).toHaveLength(1);
    expect(groups[0].sourceTable).toBe('wordpress.wp_posts');
    expect(groups[0].totalCallsPerSecond).toBe(4.5);
    expect(groups[0].queries).toEqual([
      {
        queryId: 'q1',
        excerpt: 'SELECT * FROM wp_posts WHERE ID = ?',
        queryType: 'SELECT',
        callsPerSecond: 4.5,
        reason: 'relational core',
        inScope: true,
      },
    ]);
  });

  test('a different engine from the same groups sees nothing', () => {
    expect(buildAuroraQueryGroups('aurora_postgresql', QUERY_GROUPS)).toEqual([]);
  });

  test('sorts groups and queries by calls per second, descending', () => {
    const groups = [
      {
        group_name: 'low',
        engines: ['aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', design_rps: 1, query_ids: ['a'] },
        ],
        source_queries: [{ query_id: 'a', query_text: 'q-a' }],
      },
      {
        group_name: 'high',
        engines: ['aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', design_rps: 9, query_ids: ['b'] },
        ],
        source_queries: [{ query_id: 'b', query_text: 'q-b' }],
      },
    ];
    const result = buildAuroraQueryGroups('aurora_mysql', groups);
    expect(result.map((g) => g.sourceTable)).toEqual(['high', 'low']);
  });

  test('handles missing/empty query_groups', () => {
    expect(buildAuroraQueryGroups('aurora_mysql', null)).toEqual([]);
    expect(buildAuroraQueryGroups('aurora_mysql', [])).toEqual([]);
  });

  test('resolves a deduplicated reason through reason_index and the group\'s reasons list (#478)', () => {
    const groups = [
      {
        group_name: 'wordpress.wp_posts',
        engines: ['aurora_mysql'],
        reasons: ['highest confidence for aurora_mysql'],
        access_patterns: [
          { engine: 'aurora_mysql', design_rps: 4.5, reason_index: 0, query_ids: ['q1'] },
        ],
        source_queries: [{ query_id: 'q1', query_text: 'SELECT * FROM wp_posts' }],
      },
    ];
    const result = buildAuroraQueryGroups('aurora_mysql', groups);
    expect(result[0].queries[0].reason).toBe('highest confidence for aurora_mysql');
  });
});

describe('reasonTextFor', () => {
  test('resolves through reason_index when present', () => {
    const group = { reasons: ['a reason', 'another reason'] };
    expect(reasonTextFor(group, { reason_index: 1 })).toBe('another reason');
  });

  test('falls back to description when there is no reason_index (every non-relational engine)', () => {
    const group = { reasons: ['unused'] };
    expect(reasonTextFor(group, { description: 'raw description' })).toBe('raw description');
  });

  test('empty string, never undefined, when neither is present', () => {
    expect(reasonTextFor({}, {})).toBe('');
    expect(reasonTextFor(undefined, undefined)).toBe('');
  });

  test('an out-of-range reason_index resolves to empty string rather than throwing', () => {
    const group = { reasons: ['only one'] };
    expect(reasonTextFor(group, { reason_index: 5 })).toBe('');
  });
});

describe('buildAuroraDesign', () => {
  test('assembles the full per-engine shape', () => {
    const design = buildAuroraDesign(
      'aurora_mysql',
      { target_type: 'aurora_mysql', content: AURORA_CONTENT },
      QUERY_GROUPS
    );
    expect(design.engine).toBe('aurora_mysql');
    expect(design.displayName).toBe('Aurora MySQL');
    expect(design.migrationStrategy).toBe('carry_over');
    expect(design.ddl).toBe('CREATE TABLE wp_posts (...);');
    expect(design.optimizations).toHaveLength(1);
    expect(design.tables).toHaveLength(2);
    expect(design.queryGroups).toHaveLength(1);
  });

  test('design-dependent fields are empty with no schema design, but queries still show', () => {
    const design = buildAuroraDesign('aurora_mysql', null, QUERY_GROUPS);
    expect(design.engine).toBe('aurora_mysql');
    expect(design.displayName).toBe('Aurora MySQL');
    expect(design.migrationStrategy).toBeNull();
    expect(design.ddl).toBe('');
    expect(design.optimizations).toEqual([]);
    expect(design.tables).toEqual([]);
    expect(design.queryGroups).toHaveLength(1);
  });
});

describe('buildAuroraDesigns', () => {
  test('filters schemaDesigns down to Aurora engines with content', () => {
    const designs = buildAuroraDesigns(
      [
        { target_type: 'aurora_mysql', content: AURORA_CONTENT },
        { target_type: 'dynamodb', content: { table_definitions: [] } },
        { target_type: 'aurora_postgresql', content: null },
      ],
      QUERY_GROUPS
    );
    expect(designs).toHaveLength(1);
    expect(designs[0].engine).toBe('aurora_mysql');
  });

  test('empty when there are no schema designs at all and no Aurora queries', () => {
    expect(buildAuroraDesigns([], [])).toEqual([]);
    expect(buildAuroraDesigns(null, null)).toEqual([]);
  });

  test('includes an Aurora engine with queries but no schema design (#478)', () => {
    const designs = buildAuroraDesigns([], QUERY_GROUPS);
    expect(designs).toHaveLength(1);
    expect(designs[0].engine).toBe('aurora_mysql');
    expect(designs[0].tables).toEqual([]);
    expect(designs[0].ddl).toBe('');
    expect(designs[0].queryGroups).toHaveLength(1);
  });

  test('an engine with both a design and queries is listed once, with both', () => {
    const designs = buildAuroraDesigns(
      [{ target_type: 'aurora_mysql', content: AURORA_CONTENT }],
      QUERY_GROUPS
    );
    expect(designs).toHaveLength(1);
    expect(designs[0].tables.length).toBeGreaterThan(0);
    expect(designs[0].queryGroups).toHaveLength(1);
  });
});

describe('AURORA_BLAME_LABELS ordering (#478)', () => {
  test('a blame-label group sorts last even with the highest calls/s', () => {
    const groups = [
      {
        group_name: 'Table not identified by the collector',
        engines: ['aurora_mysql'],
        access_patterns: [{ engine: 'aurora_mysql', design_rps: 999, query_ids: ['q1'] }],
        source_queries: [{ query_id: 'q1', query_text: 'SELECT 1' }],
      },
      {
        group_name: 'wordpress.wp_posts',
        engines: ['aurora_mysql'],
        access_patterns: [{ engine: 'aurora_mysql', design_rps: 1, query_ids: ['q2'] }],
        source_queries: [{ query_id: 'q2', query_text: 'SELECT * FROM wp_posts' }],
      },
    ];
    const result = buildAuroraQueryGroups('aurora_mysql', groups);
    expect(result.map((g) => g.sourceTable)).toEqual([
      'wordpress.wp_posts',
      'Table not identified by the collector',
    ]);
  });
});
