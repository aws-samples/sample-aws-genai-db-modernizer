// #1536: a statement that names no table (SET, SHOW, BEGIN/COMMIT, a procedure call)
// is stored with the collector's placeholder `tables_accessed: ["unknown"]`. The stored
// value stays as it is, so saved jobs still load; what a reader sees says the statement
// has no source table. Mirrors `display_group_name` and
// `UNRESOLVED_TABLE_GROUP_LABEL` in src/shared/report_facts.py.
export const NO_SOURCE_TABLE = 'No source table';

// The same six names as PLACEHOLDER_TABLE_NAMES in src/shared/report_facts.py (the
// collector's placeholder and the dummy tables of a SELECT with no FROM).
const PLACEHOLDER_TABLES = new Set(['unknown', 'UNKNOWN', 'None', 'null', 'DUAL', 'SYSIBM.SYSDUMMY1']);

// The group label a report saved before #1536 carries for the same statements.
const LEGACY_NO_SOURCE_GROUP_LABELS = new Set(['Table not identified by the collector']);

export const isPlaceholderTable = (table) => PLACEHOLDER_TABLES.has(table);

/** A query group's name as shown: the legacy "no table" label reads as the current one. */
export const displayGroupName = (name) =>
  LEGACY_NO_SOURCE_GROUP_LABELS.has(name) ? NO_SOURCE_TABLE : name;

/**
 * A query's table names as shown. Placeholders are dropped; when only placeholders
 * were there, the single entry is "No source table". `short` strips the schema prefix.
 */
export function displayTables(tables, { short = true } = {}) {
  const list = Array.isArray(tables) ? tables.filter(Boolean) : [];
  const real = list.filter((t) => !isPlaceholderTable(t));
  if (real.length === 0) return list.length > 0 ? [NO_SOURCE_TABLE] : [];
  return short ? real.map((t) => t.split('.').pop()) : real;
}
