/**
 * Client-side copy of the API's database-name rule (#510). The server
 * (src/shared/database_name.py, #420) accepts 1 to 128 ASCII letters, digits,
 * underscores or hyphens (not starting with a hyphen) after trimming surrounding whitespace, and rejects
 * anything else with a 422. JS cannot import the Python constant, so
 * databaseName.test.js reads it from that file and fails if the two drift.
 */
export const DATABASE_NAME_PATTERN = '[A-Za-z0-9_][A-Za-z0-9_-]{0,127}';

const DATABASE_NAME_RE = new RegExp(`^${DATABASE_NAME_PATTERN}$`);

/**
 * The name as the form sends it, so the value validated is the value sent.
 * JS trim() also strips U+FEFF, which the server's str.strip() keeps (#815).
 */
export function normalizeDatabaseName(name) {
  return name.trim();
}

export function isValidDatabaseName(name) {
  return typeof name === 'string' && DATABASE_NAME_RE.test(normalizeDatabaseName(name));
}
