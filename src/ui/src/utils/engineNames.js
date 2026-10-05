/**
 * Display names for the engine keys the pipeline emits, shared by the
 * live React results page and the "Export to HTML" button's standalone
 * export (PR #315 review finding 14: badges showed the raw engine key,
 * e.g. "dynamodb", instead of a display name like the rest of the report).
 *
 * Kept in sync with ``src/shared/engine_names.py`` by hand -- not by
 * ``scripts/sync_report_template.py`` (that script only lifts
 * ``ExportReport.js``'s own copy, used by the interactive-report export,
 * into the ATX renderer's templates).
 */
export const ENGINE_LABELS = {
  dynamodb: 'DynamoDB',
  documentdb: 'DocumentDB',
  opensearch: 'OpenSearch',
  elasticache: 'ElastiCache',
  aurora_postgresql: 'Aurora PostgreSQL',
  aurora_mysql: 'Aurora MySQL',
  neptune: 'Neptune',
  keyspaces: 'Keyspaces',
  aurora: 'Aurora',
};

export const displayEngine = (engine) => ENGINE_LABELS[engine] || engine;
