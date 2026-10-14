/**
 * #510: the Create Analysis form validates the database name with the same
 * rule the API enforces (src/shared/database_name.py), so an invalid name is
 * caught in the form instead of coming back as a raw 422.
 */
import fs from 'fs';
import path from 'path';
import { DATABASE_NAME_PATTERN, isValidDatabaseName } from '../databaseName';

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
