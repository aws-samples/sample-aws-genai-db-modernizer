# The stable design block of `assess()`

Status: **the published standard** of contract `1.1` for the design block of a designed `assess()` result, for every engine the pipeline designs: DynamoDB, Aurora MySQL, Aurora PostgreSQL, DocumentDB, OpenSearch and ElastiCache ([#1493](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1493), [#1513](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1513)). We define it and library callers follow it. The reference is [assess.md, The design block](assess.md#the-design-block); the result's JSON Schema is [`schemas/assess_result.v1.schema.json`](schemas/assess_result.v1.schema.json).

## What it is for

A caller that needs a data model runs `assess()` with `run_through: "schema_design"` (or `"synthesis"`) and `deterministic: false`. The design block, `designs`, is the part of each engine's design a caller builds on, in the stable surface: target tables, collections, indexes, data streams and key patterns, their keys or mapped fields, and which statements each serves. Everything else (attributes, item collections, access-pattern details, sizing, shard settings, analyzers, lifecycle policies, trade-offs, the full DDL) stays in `artifacts.schema_designs`, the designers' internal contracts, which have no compatibility promise.

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
| `opensearch` | `indexes[]`, `data_streams[]` | indexes: `name`, `source_tables`, `fields[]` (`name`, `type`), `aliases`, `query_ids`; data streams: `name`, `source_tables`, `fields[]` (its index template's), `query_ids` |

Every entry also has `engine` and `validation_passed`.

### OpenSearch

The OpenSearch entry copies the designer's `index_designs` and `data_stream_designs`:

- `indexes[]`: one per search index. `name` is the index name; `source_tables` the source tables it holds; `fields[]` its mapped fields in the design's order, each `{name, type}` with `type` the OpenSearch field type as the design names it, never empty (a field the design gives no name or no type is left out) (`text`, `keyword`, `long`, `date`, `nested`, `wildcard` and so on); `aliases` its aliases; `query_ids` the statements the design serves from it (its in-scope access patterns whose `index_or_stream` names the index).
- `data_streams[]`: one per data stream (a time-series table). `name` is the data stream name; `fields[]` the mapped fields of its index template (including `@timestamp`); `source_tables` and `query_ids` as for an index.

A field of a `nested` or `object` field can be listed under its dotted path (`usermeta.meta_key`), as the design names it. A `text` field the design gives a `keyword` subfield (for sorting or aggregation, read as `<name>.keyword`) is listed once, with its own type; the subfield is in `artifacts.schema_designs.opensearch` (`multi_field`). Analyzers, `doc_values`, shard and replica settings, the index template's patterns and the lifecycle policy stay in `artifacts.schema_designs.opensearch`.

## What is guaranteed and what is not

Guaranteed, from contract `1.1` on (a result's `contract_version` says which version it was written for):

- every field listed above is present with the type the JSON Schema gives it; a list with nothing in it is `[]`, never missing;
- `designs` has one entry per engine of `summary.designed_engines`, in that order, and `validation_passed` says whether the design passed its own checks. The result model enforces it for a `1.1` result (`AssessmentResult` refuses one without it), and a test keeps a reader for every engine the pipeline designs. A `1.0` result is not checked: it is read as contract `1.0` read it, and can list `opensearch` as designed with no entry;
- every value is copied from the engine's schema design: a name, key, type or index statement is the design's own, and each `query_ids` lists the statements the design's access patterns name for that item, first seen first, each once (a DocumentDB index's `query_ids` is the index's own list, as the design wrote it);
- an access pattern the design marks out of scope (`in_scope: false`) adds no statement to any `query_ids`.

Not guaranteed:

- that every statement on an engine is in some `query_ids`: a design can serve a statement in a way the block does not name, or list it as unsupported (`artifacts.schema_designs.<engine>.unsupported_patterns`);
- names, types and the choice of tables, indexes and keys between runs: a model writes each design, so two runs of the same collection can differ (see `AssessmentResult`'s determinism note in [assess.md](assess.md));
- that a type is from a closed list where the block says "as the design names it" (OpenSearch field types, ElastiCache `data_type`): read them as strings;
- that a design with `validation_passed: false` is complete or consistent: check it before you build on it;
- any field of `artifacts`.

A new engine, or a new optional field in an entry, is a MINOR change: read `designs` with a default branch on `engine`, and ignore fields you do not know. A schema file refuses an engine it does not know, so validate a result against the schema of its own `contract_version` (the OpenSearch entry needs `1.1`).

## Examples

### A designed WordPress run

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

### Aurora PostgreSQL

A real run: the TrailGear sample (synthetic, SQL Server, no engine scope), designed on 2026-10-22 by the Aurora PostgreSQL designer on Amazon Bedrock with the designer's default model, Claude Opus 5.5, its reviewer approving it on the first iteration. The schema is translated to Aurora PostgreSQL (22 tables, `validation_passed: true`); two of its tables:

```json
{
  "engine": "aurora_postgresql",
  "validation_passed": true,
  "tables": [
    {
      "name": "Customer",
      "primary_key": ["CustomerID"],
      "indexes": ["CREATE UNIQUE INDEX \"UX_Customer_Email\" ON \"Customer\" (\"Email\");"]
    },
    {
      "name": "SalesOrder",
      "primary_key": ["SalesOrderID"],
      "indexes": [
        "CREATE UNIQUE INDEX \"UX_SalesOrder_OrderNumber\" ON \"SalesOrder\" (\"OrderNumber\");",
        "CREATE INDEX \"IX_SalesOrder_CustomerID_OrderDate\" ON \"SalesOrder\" (\"CustomerID\", \"OrderDate\");",
        "CREATE INDEX \"IX_SalesOrder_Status\" ON \"SalesOrder\" (\"Status\");",
        "CREATE INDEX \"IX_SalesOrder_OrderDate\" ON \"SalesOrder\" (\"OrderDate\") INCLUDE (\"TotalDue\");"
      ]
    }
  ],
  "ddl_artifact_path": "artifacts.schema_designs.aurora_postgresql.generated_ddl"
}
```

### OpenSearch

A real run: the WordPress sample with the engine scope DynamoDB and OpenSearch (12 statements placed on OpenSearch), designed on 2026-10-25 by the OpenSearch designer on Amazon Bedrock with Claude Opus 5.5, its reviewer approving it on the first iteration. The design has 3 indexes, no data stream (no WordPress table is a time-series workload) and `validation_passed: true`; one of its indexes:

```json
{
  "engine": "opensearch",
  "validation_passed": true,
  "indexes": [
    {
      "name": "wp-users",
      "source_tables": ["wordpress.wp_users", "wordpress.wp_usermeta"],
      "fields": [
        {"name": "id", "type": "long"},
        {"name": "user_login", "type": "text"},
        {"name": "user_nicename", "type": "keyword"},
        {"name": "user_email", "type": "keyword"},
        {"name": "user_url", "type": "keyword"},
        {"name": "user_registered", "type": "date"},
        {"name": "user_status", "type": "integer"},
        {"name": "display_name", "type": "text"},
        {"name": "usermeta", "type": "nested"},
        {"name": "usermeta.umeta_id", "type": "long"},
        {"name": "usermeta.meta_key", "type": "keyword"},
        {"name": "usermeta.meta_value", "type": "wildcard"}
      ],
      "aliases": ["wp-users-live"],
      "query_ids": [
        "797c033f24c616611d9d000bcef94d12e698d48aac1ea09e84c71805d1d47935",
        "05e0232d3850a4672d21c95cdebf907cd787cdd0353fff9adee1cd4009b5a653",
        "f4fcc7f6ef237eb98ceebe18f53efbc90ce9cc76e15fc20c0010447b449a1987"
      ]
    }
  ],
  "data_streams": []
}
```

A data stream item has the same form without `aliases`; its `fields` are its index template's and include `@timestamp`.

### DocumentDB

The runs above placed nothing on DocumentDB. A DocumentDB entry has the same form; for example (illustrative, from the test fixtures):

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

## Decisions

These were the proposal's open questions; the answers are part of the standard.

- **OpenSearch** has an entry (above): indexes and data streams with their source tables, mapped fields and types, index aliases and the statements each serves. Analyzers, settings and lifecycle policies stay in `artifacts`.
- **Aurora indexes** stay the design's `CREATE INDEX` statements, verbatim. A parsed form (name, columns, uniqueness) would be a MINOR addition beside them, never a replacement.
- **Validation reasons** stay in `artifacts.schema_designs.<engine>.validation_failures`; the stable signal is `validation_passed`.
