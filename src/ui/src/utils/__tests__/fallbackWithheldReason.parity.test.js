/**
 * #269 round 2: `fallbackWithheldReason.js` ports `src/report/renderers.py`'s
 * `_FALLBACK_REASONS` table by hand (JS cannot import Python). This test reads
 * the Python source directly and asserts the two tables still agree marker
 * for marker, reason for reason, and in the same order -- so a future edit to
 * one side that forgets the other fails CI instead of silently drifting (same
 * class of guard as `test_engine_names_js_sync.py` / `src/ui/src/utils/__tests__/
 * codeEditorI18n.allCallSites.test.js` use for their own hand-kept-in-sync
 * copies).
 */
import fs from 'fs';
import path from 'path';
import { FALLBACK_REASONS } from '../fallbackWithheldReason';

// nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
const RENDERERS_PY = path.resolve(__dirname, '../../../../../src/report/renderers.py');

/** Parse `_FALLBACK_REASONS`'s tuple-of-tuples literal out of renderers.py. */
function parsePythonFallbackReasons() {
  const source = fs.readFileSync(RENDERERS_PY, 'utf8');
  const tableMatch = source.match(
    /_FALLBACK_REASONS: tuple\[tuple\[str, str\], \.\.\.\] = \(([\s\S]*?)\n\)\n/,
  );
  if (!tableMatch) {
    throw new Error('could not find _FALLBACK_REASONS in src/report/renderers.py');
  }
  const body = tableMatch[1];
  const pairRe = /\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*,?\s*\)/g;
  const pairs = [...body.matchAll(pairRe)].map((m) => [m[1], m[2]]);
  if (pairs.length === 0) {
    throw new Error('parsed zero pairs out of _FALLBACK_REASONS -- regex is out of date');
  }
  return pairs;
}

test('fallbackWithheldReason.js has the same marker/reason table as renderers.py, in order', () => {
  const pythonTable = parsePythonFallbackReasons();
  expect(FALLBACK_REASONS).toEqual(pythonTable);
});
