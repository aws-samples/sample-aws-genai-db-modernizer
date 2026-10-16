/**
 * #510: the Create Analysis form validates the database name with the same
 * rule the API enforces (src/shared/database_name.py), so an invalid name is
 * caught in the form instead of coming back as a raw 422.
 */
import fs from 'fs';
import path from 'path';
import { DATABASE_NAME_PATTERN, isValidDatabaseName, normalizeDatabaseName } from '../databaseName';

test.each([
  'wordpress',
  'my_db',
  'app-prod-01',
  'A',
  'x'.repeat(128),
  '  shop  ',
  '\tshop\n',
  '\nshop\t',
])('accepts %p', (name) => {
  expect(isValidDatabaseName(name)).toBe(true);
});

test.each([
  '',
  '   ',
  'my.app',
  'Sales Data',
  'café',
  '../etc',
  'a/b',
  'x'.repeat(129),
  null,
  undefined,
  42,
])('rejects %p', (name) => {
  expect(isValidDatabaseName(name)).toBe(false);
});

test('uses the same pattern as src/shared/database_name.py', () => {
  const pythonSource = fs.readFileSync(
    path.join(__dirname, '../../../../shared/database_name.py'),
    'utf8',
  );
  const match = pythonSource.match(/^DATABASE_NAME_PATTERN = r"(.*)"$/m);
  expect(match).not.toBeNull();
  expect(DATABASE_NAME_PATTERN).toBe(match[1]);
});

test.each([
  ['  shop  ', 'shop'],
  ['\tshop\n', 'shop'],
  ['\ufeffshop', 'shop'],
])('normalizes %p to %p', (name, expected) => {
  expect(normalizeDatabaseName(name)).toBe(expected);
});

test('CreateAnalysis.js sends every database name through normalizeDatabaseName', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../pages/CreateAnalysis.js'), 'utf8');
  // Five request fields carry the name: the online create body and its
  // connection, and the offline prepare body, confirm query string and create body.
  const sends = source.match(/\b(?:database_name|database)(?::\s*|=\$\{encodeURIComponent\()[^,\n)]+/g);
  expect(sends).toHaveLength(5);
  for (const send of sends) {
    expect(send).toMatch(/normalizeDatabaseName\((?:formData\.databaseName|offlineDatabaseName)$/);
  }
});
