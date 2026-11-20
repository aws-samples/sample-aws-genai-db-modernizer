// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The UI prints a figure only through utils/format.js (#2309, #2347).
 *
 * format.js is the one place that turns a number, a money amount, a percentage, a
 * rate or a size into text, with the reports' rules (fmt_num, _fmt_usd), so the
 * same figure reads the same on every page and in every deliverable (ADR-032,
 * "Deliverables agree"). This guard parses every non-test UI source file
 * (@babel/parser, as rendererOnlyFormat.test.js does) and fails on a figure
 * formatted by hand anywhere else:
 *
 * - `x.toFixed(..)`, `x.toLocaleString(..)`, `x.toPrecision(..)`;
 * - `Intl.NumberFormat`;
 * - `Math.round(..)`, the hand-rolled "round to one place";
 * - a template literal that writes "%" right after a value (`${share}%`) or "$"
 *   right before one (`$${cost}`), and a `+` that glues "%" or "$" to a value;
 * - a locale string that writes "$" before a placeholder ("${{cost}}/mo"): the
 *   amount comes in already formatted by formatMoney.
 *
 * ALLOWED lists the few uses that format no figure, each with its reason and its
 * count in that file. A count may only match: a use that is gone must leave the
 * list in the same change.
 */

const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');
const t = require('@babel/types');

const UI_SRC = path.resolve(__dirname, '..', '..');

// The files that are the formatter, or are generated.
const FORMATTER_FILES = new Set(['utils/format.js', 'utils/engineCatalog.generated.js']);

const ALLOWED = [
  { file: 'utils/explorerLayout.js', kind: 'Math.round', count: 1, reason: 'the details panel width in whole pixels, never printed' },
  { file: 'pages/CreateAnalysis.js', kind: 'Math.round', count: 1, reason: 'the upload progress value the ProgressBar draws' },
  { file: 'pages/JobMonitoringSummary.js', kind: 'Math.round', count: 2, reason: 'the pipeline progress value the ProgressBar draws' },
  { file: 'pages/AssignmentGate.js', kind: 'percent literal', count: 1, reason: 'the CSS width of a distribution bar segment' },
];

const NUMBER_METHODS = new Set(['toFixed', 'toLocaleString', 'toPrecision']);

function sources(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === '__tests__' ? [] : sources(full);
    return entry.name.endsWith('.js') ? [full] : [];
  });
}

function children(node) {
  const keys = t.VISITOR_KEYS[node.type] || [];
  const out = [];
  keys.forEach((k) => {
    const v = node[k];
    if (Array.isArray(v)) v.forEach((c) => c && out.push(c));
    else if (v) out.push(v);
  });
  return out;
}

function* walk(node) {
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    stack.push(...children(n));
  }
}

function propName(m) {
  if (!t.isMemberExpression(m) && !t.isOptionalMemberExpression(m)) return null;
  if (!m.computed && t.isIdentifier(m.property)) return m.property.name;
  if (t.isStringLiteral(m.property)) return m.property.value;
  return null;
}

function isIdent(node, name) {
  return t.isIdentifier(node) && node.name === name;
}

function isSymbolLiteral(node, test) {
  return t.isStringLiteral(node) && test(node.value);
}

/** Every hand formatting in `code`, as `{ kind, line }`. */
function handFormattingIn(code) {
  const ast = parse(code, { sourceType: 'unambiguous', plugins: ['jsx', 'optionalChaining', 'classProperties'] });
  const found = [];
  const add = (kind, node) => found.push({ kind, line: node.loc ? node.loc.start.line : 0 });
  for (const node of walk(ast.program)) {
    if (t.isCallExpression(node) || t.isOptionalCallExpression(node)) {
      const name = propName(node.callee);
      if (NUMBER_METHODS.has(name)) add(name, node);
      if (name === 'round' && isIdent(node.callee.object, 'Math')) add('Math.round', node);
    }
    if ((t.isMemberExpression(node) || t.isOptionalMemberExpression(node))
        && isIdent(node.object, 'Intl') && propName(node) === 'NumberFormat') {
      add('Intl.NumberFormat', node);
    }
    if (t.isJSXElement(node) || t.isJSXFragment(node)) {
      // JSX text next to an expression: `{share}%` or `${cost}` (also `{"$"}{cost}`).
      node.children.forEach((child, i) => {
        const prev = node.children[i - 1];
        const next = node.children[i + 1];
        const isExpr = (c) => c && t.isJSXExpressionContainer(c) && !t.isJSXEmptyExpression(c.expression);
        if (t.isJSXText(child)) {
          if (isExpr(prev) && child.value.startsWith('%')) add('percent literal', child);
          if (isExpr(next) && child.value.trimEnd().endsWith('$')) add('dollar literal', child);
        }
        if (isExpr(child) && t.isStringLiteral(child.expression) && child.expression.value.endsWith('$') && isExpr(next)) {
          add('dollar literal', child);
        }
        if (isExpr(child) && t.isStringLiteral(child.expression) && child.expression.value.startsWith('%') && isExpr(prev)) {
          add('percent literal', child);
        }
      });
    }
    if (t.isTemplateLiteral(node)) {
      node.quasis.forEach((q, i) => {
        const text = q.value.cooked || '';
        if (i > 0 && text.startsWith('%')) add('percent literal', q);
        if (i < node.quasis.length - 1 && text.endsWith('$')) add('dollar literal', q);
      });
    }
    if (t.isBinaryExpression(node) && node.operator === '+') {
      if (isSymbolLiteral(node.right, (v) => v.startsWith('%'))) add('percent literal', node);
      if (isSymbolLiteral(node.left, (v) => v.endsWith('$'))) add('dollar literal', node);
    }
  }
  return found;
}

function scan() {
  const byFile = {};
  sources(UI_SRC).forEach((full) => {
    const rel = path.relative(UI_SRC, full).split(path.sep).join('/');
    if (FORMATTER_FILES.has(rel)) return;
    const hits = handFormattingIn(fs.readFileSync(full, 'utf8'));
    if (hits.length) byFile[rel] = hits;
  });
  return byFile;
}

describe('the checker finds hand formatting (#2347)', () => {
  it.each([
    ['const s = n.toFixed(1);', 'toFixed'],
    ['const s = n?.toFixed(0);', 'toFixed'],
    ['const s = n.toLocaleString("en-US");', 'toLocaleString'],
    ['const s = n.toPrecision(3);', 'toPrecision'],
    ['const f = new Intl.NumberFormat("en-US");', 'Intl.NumberFormat'],
    ['const r = Math.round(n * 10) / 10;', 'Math.round'],
    ['const s = `${share}%`;', 'percent literal'],
    ['const s = `${share}% of calls`;', 'percent literal'],
    ['const s = `$${cost}/mo`;', 'dollar literal'],
    ["const s = share + '%';", 'percent literal'],
    ["const s = '$' + cost;", 'dollar literal'],
    ['const el = <b>{n.toFixed(2)}</b>;', 'toFixed'],
    ['const el = <b>{share}%</b>;', 'percent literal'],
    ['const el = <b>${cost}</b>;', 'dollar literal'],
    ['const el = <b>{"$"}{cost}</b>;', 'dollar literal'],
  ])('flags %s', (code, kind) => {
    expect(handFormattingIn(code).map((h) => h.kind)).toEqual([kind]);
  });

  it.each([
    'const s = formatPercent(share);',
    'const s = `${formatMoney(cost)}/mo`;',
    "const s = 'Cost: ' + formatMoney(cost);",
    'const el = <b>100% of {n}</b>;',
    'const el = <b>{formatMoney(cost)}</b>;',
    'const s = `100% of ${n}`;',
    'const s = `${a} ${b}`;',
    'const n = Math.max(1, Math.ceil(total / size));',
    "const s = t('x', { value: formatRate(v) });",
  ])('passes %s', (code) => {
    expect(handFormattingIn(code)).toEqual([]);
  });
});

describe('no UI file formats a figure by hand (#2347)', () => {
  const byFile = scan();
  const allowed = Object.create(null);
  ALLOWED.forEach(({ file, kind, count }) => {
    allowed[`${file}|${kind}`] = count;
  });

  it('every allowance names a reason', () => {
    ALLOWED.forEach((entry) => {
      expect([entry.file, typeof entry.reason === 'string' && entry.reason.length > 10]).toEqual([entry.file, true]);
    });
  });

  it('prints every figure through format.js', () => {
    const offending = [];
    Object.entries(byFile).forEach(([file, hits]) => {
      const counts = Object.create(null); // not {}: its inherited toLocaleString would be a "count"
      hits.forEach((h) => { counts[h.kind] = (counts[h.kind] || 0) + 1; });
      Object.entries(counts).forEach(([kind, n]) => {
        if (n > (allowed[`${file}|${kind}`] || 0)) {
          hits.filter((h) => h.kind === kind).forEach((h) => offending.push(`${file}:${h.line} ${kind}`));
        }
      });
    });
    expect(offending).toEqual([]);
  });

  it('keeps the allowance honest: it does not list uses that are gone', () => {
    ALLOWED.forEach(({ file, kind, count }) => {
      const n = (byFile[file] || []).filter((h) => h.kind === kind).length;
      expect([file, kind, n]).toEqual([file, kind, count]);
    });
  });

  it('no locale string writes a dollar sign before a placeholder', () => {
    const strings = JSON.parse(fs.readFileSync(path.join(UI_SRC, 'locales', 'en.json'), 'utf8'));
    const dollars = Object.entries(strings).filter(([, v]) => typeof v === 'string' && v.includes('${{')).map(([k]) => k);
    expect(dollars).toEqual([]);
  });

  it('format.js is where figures are printed', () => {
    expect(fs.readFileSync(path.join(UI_SRC, 'utils', 'format.js'), 'utf8')).toContain('Intl.NumberFormat');
  });
});
