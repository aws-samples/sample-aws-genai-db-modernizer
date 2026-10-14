/**
 * Client-side copy of the API's database-name rule (#510). The server
 * (src/shared/database_name.py, #420) accepts 1 to 128 ASCII letters, digits,
 * underscores or hyphens after trimming surrounding whitespace, and rejects
 * anything else with a 422. JS cannot import the Python constant, so
 * databaseName.test.js reads it from that file and fails if the two drift.
 */
export const DATABASE_NAME_PATTERN = '[A-Za-z0-9_-]{1,128}';

const DATABASE_NAME_RE = new RegExp(`^${DATABASE_NAME_PATTERN}$`);

export function isValidDatabaseName(name) {
  return typeof name === 'string' && DATABASE_NAME_RE.test(name.trim());
}
