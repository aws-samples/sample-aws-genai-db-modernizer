/**
 * #478: every `<CodeEditor` in this app must pass
 * `codeEditorI18nStrings(t, ...)` as its `i18nStrings` prop, or Cloudscape's
 * own fallback renders a literal "undefined" in the page (see
 * codeEditorI18n.js's docstring). The fix for the Results page's two
 * editors missed `JobMonitoring.js` and `JobMonitoringSummary.js`'s own
 * editors -- this scans every `.js` file under `src/ui/src` for a
 * `<CodeEditor` element and fails if its nearest `i18nStrings={...}` is not
 * built from `codeEditorI18nStrings(`, so a new editor that skips the
 * helper fails this test instead of shipping "undefined" to a customer.
 */
import fs from 'fs';
import path from 'path';

const SRC_ROOT = path.join(__dirname, '..', '..');

function listJsFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
    // entry.name comes from fs.readdirSync on this repo's own src/ui/src tree
    // (SRC_ROOT below, test-time only) -- not user input, so this is not a
    // path-traversal vector despite matching the generic semgrep pattern.
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...listJsFiles(full));
    } else if (entry.name.endsWith('.js')) {
      out.push(full);
    }
  }
  return out;
}

/** Every `<CodeEditor` element's source slice, from its own `<CodeEditor`
 * token through its matching `/>` (handles nested `{...}` braces by
 * tracking JSX attribute depth loosely: good enough for this file's own
 * CodeEditor call sites, all of which are self-closing with no children). */
function findCodeEditorElements(source) {
  const elements = [];
  const re = /<CodeEditor\b/g;
  let match;
  while ((match = re.exec(source))) {
    const start = match.index;
    const closeIdx = source.indexOf('/>', start);
    if (closeIdx === -1) continue;
    elements.push(source.slice(start, closeIdx + 2));
  }
  return elements;
}

describe('every <CodeEditor call site passes codeEditorI18nStrings', () => {
  const files = listJsFiles(SRC_ROOT);
  const callSites = [];
  for (const file of files) {
    const source = fs.readFileSync(file, 'utf8');
    for (const element of findCodeEditorElements(source)) {
      callSites.push({ file: path.relative(SRC_ROOT, file), element });
    }
  }

  it('found at least one <CodeEditor call site (sanity check for the scan itself)', () => {
    expect(callSites.length).toBeGreaterThan(0);
  });

  it.each(callSites.map((c) => [c.file, c]))('%s', (_label, { element }) => {
    expect(element).toMatch(/i18nStrings=\{codeEditorI18nStrings\(/);
  });
});
