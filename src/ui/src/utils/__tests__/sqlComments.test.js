/**
 * #814: `sqlComments.js` is the JavaScript copy of `src/shared/sql_comments.py`'s
 * leading-comment scanner. Both run the same cases from
 * `tests/unit/shared/sql_comment_cases.json`, so the UI and the server keep one
 * comment rule.
 */
import fs from 'fs';
import path from 'path';
import { stripLeadingComments } from '../sqlComments';
import { engineOptionsForQuery } from '../assignmentSummary';

// nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
const CASES_PATH = path.resolve(__dirname, '../../../../../tests/unit/shared/sql_comment_cases.json');
const CASES = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));

function jsOptions(options) {
  return {
    hashComments: Boolean(options.hash_comments),
    stripFollowingSpace: Boolean(options.strip_following_space),
    nested: Boolean(options.nested),
  };
}

describe('stripLeadingComments', () => {
  test.each(CASES.leading.map((c) => [JSON.stringify(c.text), c]))(
    'matches the Python scanner on %s',
    (_label, c) => {
      expect(stripLeadingComments(c.text, jsOptions(c.options))).toBe(c.expected);
    },
  );

  test('the shared cases are loaded', () => {
    expect(CASES.leading.length).toBeGreaterThan(10);
  });

  test('null and undefined read as empty text', () => {
    expect(stripLeadingComments(null)).toBe('');
    expect(stripLeadingComments(undefined)).toBe('');
  });
});

// The old regex, /^(\s*(--[^\n]*\n|#[^\n]*\n|\/\*[\s\S]*?\*\/)\s*)+/, overflowed
// the stack between 1M and 1.5M repeated comments on Node 22; every case here
// throws RangeError on it.
const OPTIONS = [
  { label: 'OpenSearch', value: 'opensearch' },
  { label: 'Aurora PostgreSQL', value: 'aurora_postgresql' },
];
const RUNS = 3_000_000;
const PATHOLOGICAL = {
  'many /**/': '/**/'.repeat(RUNS) + 'INSERT INTO t VALUES (1)',
  'many # lines': '#\n'.repeat(RUNS) + 'INSERT INTO t VALUES (1)',
  'many -- lines': '--\n'.repeat(RUNS) + 'INSERT INTO t VALUES (1)',
};

describe('long comment runs', () => {
  test.each(Object.entries(PATHOLOGICAL))('%s finishes without overflowing', (_name, text) => {
    const start = Date.now();
    const options = engineOptionsForQuery(OPTIONS, 'OTHER', text);
    expect(Date.now() - start).toBeLessThan(5000);
    expect(options).toEqual([{ label: 'Aurora PostgreSQL', value: 'aurora_postgresql' }]);
  });

  test('nested scanning of an unterminated run stays linear', () => {
    const time = (n) => {
      const text = '/*'.repeat(n);
      const start = process.hrtime.bigint();
      stripLeadingComments(text, { nested: true });
      return Number(process.hrtime.bigint() - start);
    };
    time(250_000);
    time(1_000_000);
    const small = Math.min(time(250_000), time(250_000), time(250_000));
    const large = Math.min(time(1_000_000), time(1_000_000), time(1_000_000));
    expect(large / Math.max(small, 1)).toBeLessThan(8);
  });
});
