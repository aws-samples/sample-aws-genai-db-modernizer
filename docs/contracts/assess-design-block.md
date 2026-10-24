# Proposal: the stable design block of `assess()`

Status: built in contract `1.0` (not yet released), for review by library callers before the release that ships `1.0` ([#1493](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1493)). The reference is [assess.md, The design block](assess.md#the-design-block).

## What it is for

A caller that needs a data model runs `assess()` with `run_through: "schema_design"` (or `"synthesis"`) and `deterministic: false`. Until now its designs were only in `artifacts.schema_designs`, the designers' internal contracts, with no compatibility promise. The design block, `designs`, is the part of those designs a caller builds on, in the stable surface: target tables, keys, indexes and which statements each serves. Everything else (attributes, item collections, access-pattern details, sizing, trade-offs, the full DDL) stays in `artifacts`.

## Rules

- **Only on a designed result.** `designs` is set exactly when `label` is `designed`, and `null` otherwise. A placement-only result (`analysis_only` or `recommendation`) never carries a data model.
- **Only from schema design.** Every value is copied from the engine's schema design. Nothing comes from the deterministic analysis, whose key guesses are a routing input, not a design. Nothing is recomputed: the block groups the design's own access patterns by the table, index, collection or key they name.
- **One entry per designed engine**, in `summary.designed_engines` order, discriminated by `engine`. A new engine is a MINOR change: read the list with a default branch.
- **Failed designs are kept.** Each entry has `validation_passed`; `false` means the design failed its own checks (reasons in `artifacts.schema_designs.<engine>.validation_failures`) or records no result.
- **Small and stable.** Names are chosen for a caller and do not mirror the internal contracts field for field, so an internal change does not break the block.
- **Names are data.** Table, key and index names come from a model's design of collection text: escape them when you render them.

## Fields per engine

| `engine` | Per entry | Per item |
|---|---|---|
| `dynamodb` | `tables[]` | `name`, `source_tables`, `partition_key` `{name, type}`, `sort_key` (or `null`), `gsis[]` (`name`, ordered `partition_key[]` and `sort_key[]`, `projection`, `projected_attributes`, `query_ids`), `query_ids` (served from the table itself) |
| `aurora_mysql`, `aurora_postgresql` | `tables[]`, `ddl_artifact_path` | `name`, `primary_key`, `indexes` (the design's `CREATE INDEX` statements) |
| `documentdb` | `collections[]` | `name`, `source_tables`, `embedded[]` (`source_table`, `path`, `strategy` `embed`, `reference` or `hybrid`), `referenced_collections`, `indexes[]` (`name`, ordered `keys[]` `{field, direction}`, `type`, `query_ids`), `query_ids` |
| `elasticache` | `keys[]` | `pattern`, `data_type`, `ttl_seconds` (or `null`), `source_tables`, `query_ids` |

Every entry also has `engine` and `validation_passed`.

## A real example

A designed WordPress run (no engine scope) designed Aurora MySQL (20 tables), DynamoDB (19 tables, 11 with a sort key, 3 global secondary indexes) and ElastiCache (17 key patterns), all with `validation_passed: true`. One item of each, from its `designs`:

```json
[
  {
    "engine": "dynamodb",
    "validation_passed": true,
    "tables": [
      {
        "name": "WpComments",
        "source_tables": ["wordpress.wp_comments"],
        "partition_key": {"name": "comment_id", "type": "N"},
        "sort_key": null,
        "gsis": [
          {
            "name": "CommentsByPost",
            "partition_key": [{"name": "comment_post_id", "type": "N"}],
            "sort_key": [
              {"name": "comment_approved", "type": "S"},
              {"name": "comment_date_gmt", "type": "S"}
            ],
            "projection": "KEYS_ONLY",
            "projected_attributes": [],
            "query_ids": ["8c66eaee9125d49703cd068cdba72c305ac38bcbb8e1dab7f399e224cb40a7d6"]
          }
        ],
        "query_ids": [
          "7346ea7e6380af233fc40e0fa6427d3f17efe5fe63447f048542e8ff95f6a406",
          "de9a7ba3598eaa8b41839c2fce3889985e5636fc7489653be3b59378bf4779da"
        ]
      }
    ]
  },
  {
    "engine": "aurora_mysql",
    "validation_passed": true,
    "tables": [
      {
        "name": "wp_actionscheduler_groups",
        "primary_key": ["group_id"],
        "indexes": ["CREATE INDEX `slug` ON `wp_actionscheduler_groups` (`slug`);"]
      }
    ],
    "ddl_artifact_path": "artifacts.schema_designs.aurora_mysql.generated_ddl"
  },
  {
    "engine": "elasticache",
    "validation_passed": true,
    "keys": [
      {
        "pattern": "wp_options:{option_name}",
        "data_type": "hash",
        "ttl_seconds": 3600,
        "source_tables": ["wordpress.wp_options"],
        "query_ids": [
          "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799",
          "e223aefb06f2d48303ad26d2ee9f8aa957286f08174a6e92a6e583544f8099f0"
        ]
      }
    ]
  }
]
```

That run placed nothing on DocumentDB. A DocumentDB entry has the same form; for example (illustrative, from the test fixtures):

```json
{
  "engine": "documentdb",
  "validation_passed": true,
  "collections": [
    {
      "name": "wp_posts",
      "source_tables": ["wordpress.wp_posts"],
      "embedded": [{"source_table": "wordpress.wp_postmeta", "path": "meta", "strategy": "embed"}],
      "referenced_collections": ["wp_users"],
      "indexes": [
        {
          "name": "idx_wp_posts_status",
          "keys": [{"field": "post_status", "direction": 1}, {"field": "post_date", "direction": -1}],
          "type": "compound",
          "query_ids": ["Q01"]
        }
      ],
      "query_ids": ["Q01", "Q02"]
    }
  ]
}
```

## Open questions for callers

- **OpenSearch** has no entry yet: its design (index mappings) is in `artifacts` only. Tell us which of its fields you need.
- **Aurora indexes** are the design's `CREATE INDEX` statements, verbatim. If you need them parsed (name, columns, uniqueness), say so: that is a MINOR addition.
- **Validation reasons** are not in the block (only `validation_passed`); they stay in `artifacts`. Tell us if you need them in the stable surface.
