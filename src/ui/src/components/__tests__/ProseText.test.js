/**
 * #1321: a backtick span in report prose renders as inline code. Synthesis
 * writes a query cited by id as its label ("query `8c66eaee`"); the engineering
 * report renders it as a code span, and the UI must too, so the deliverables
 * agree and no literal backtick shows. The text is untrusted: it is rendered as
 * React text nodes, never as HTML.
 */
import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ProseText, { proseNodes } from '../ProseText';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function show(text) {
  act(() => root.render(<ProseText text={text} />));
  return container;
}

test('renders a query label in backticks as inline code, without the backticks', () => {
  const el = show('The count (query `8c66eaee`) runs as a Query.');
  const code = el.querySelectorAll('code');
  expect(code).toHaveLength(1);
  expect(code[0].textContent).toBe('8c66eaee');
  expect(el.textContent).toBe('The count (query 8c66eaee) runs as a Query.');
});

test('renders several spans and keeps the prose between them', () => {
  const el = show('`a1` and `b2`; then c');
  expect([...el.querySelectorAll('code')].map((c) => c.textContent)).toEqual(['a1', 'b2']);
  expect(el.textContent).toBe('a1 and b2; then c');
});

test.each(['plain prose', 'one ` backtick', 'empty `` span', 'line `a\nb` break'])(
  'leaves %j as written',
  (text) => {
    const el = show(text);
    expect(el.querySelectorAll('code')).toHaveLength(0);
    expect(el.textContent).toBe(text);
  },
);

test('never renders markup in the text as HTML', () => {
  const el = show('<img src=x onerror="alert(1)"> and `<b>bold</b>`');
  expect(el.querySelector('img')).toBeNull();
  expect(el.querySelector('b')).toBeNull();
  expect(el.querySelector('code').textContent).toBe('<b>bold</b>');
  expect(el.textContent).toBe('<img src=x onerror="alert(1)"> and <b>bold</b>');
});

test('renders nothing for missing text', () => {
  expect(proseNodes(null)).toEqual([]);
  expect(proseNodes(undefined)).toEqual([]);
  expect(show(null).textContent).toBe('');
});

// The pages render model and report prose through ProseText; a raw render
// would show the backticks again. Cloudscape's ESM cannot load in this Jest
// config, so the pages are checked by their AST (as rendererOnlyFormat.test.js
// is): a prose field must never be a JSX child or a table cell's value as it
// is, whatever the spacing, line breaks, optional chaining or `||` / `&&` /
// `?:` around it, and must be passed to ProseText at least once.
const { parse } = require('@babel/parser');
const t = require('@babel/types');

const PAGES = {
  'AnalysisResults.js': fs.readFileSync(path.join(__dirname, '../../pages/AnalysisResults.js'), 'utf8'),
  'AssignmentGate.js': fs.readFileSync(path.join(__dirname, '../../pages/AssignmentGate.js'), 'utf8'),
};
const PROSE = {
  'AnalysisResults.js': [
    'synthesis.summary',
    'ap.description',
    'ap.patternDescription',
    'body',
    'risk.mitigation',
    'detail',
    'tradeOff.description',
    'tradeOff.impact',
    'opt.recommendation',
    'opt.rationale',
    'q.reason',
    'item.description',
    'ap.outOfScopeReason',
  ],
  'AssignmentGate.js': ['executiveSummary', 'c.reason'],
};

function parseSource(code) {
  return parse(code, { sourceType: 'unambiguous', plugins: ['jsx', 'optionalChaining', 'classProperties'] });
}

function walk(node, visit, parent = null) {
  if (!node || typeof node.type !== 'string') return;
  visit(node, parent);
  (t.VISITOR_KEYS[node.type] || []).forEach((k) => {
    const v = node[k];
    if (Array.isArray(v)) v.forEach((c) => walk(c, visit, node));
    else walk(v, visit, node);
  });
}

// `a.b`, `a?.b` and `a` as a dotted path; anything else is null.
function dotted(node) {
  if (t.isIdentifier(node)) return node.name;
  if ((t.isMemberExpression(node) || t.isOptionalMemberExpression(node)) && !node.computed) {
    const head = dotted(node.object);
    return head && `${head}.${node.property.name}`;
  }
  return null;
}

// The values an expression renders as it is: both sides of `||` and `??`, the
// right side of `&&` (its left side is a guard: a non-empty string is truthy,
// so it is never what shows), and both branches of `?:`.
function rendered(node) {
  if (t.isLogicalExpression(node, { operator: '&&' })) return rendered(node.right);
  if (t.isLogicalExpression(node)) return [...rendered(node.left), ...rendered(node.right)];
  if (t.isConditionalExpression(node)) return [...rendered(node.consequent), ...rendered(node.alternate)];
  return [node];
}

// The prose fields `src` renders raw, and the ones it passes to ProseText.
function proseRenders(src) {
  const raw = [];
  const wrapped = [];
  walk(parseSource(src), (node, parent) => {
    let shown = null;
    if (t.isJSXExpressionContainer(node) && (t.isJSXElement(parent) || t.isJSXFragment(parent))) {
      shown = node.expression;
    } else if (t.isObjectProperty(node) && t.isIdentifier(node.key, { name: 'cell' })
      && t.isArrowFunctionExpression(node.value) && !t.isBlockStatement(node.value.body)) {
      shown = node.value.body;
    }
    if (shown) rendered(shown).forEach((n) => { const p = dotted(n); if (p) raw.push(p); });
    if (t.isJSXElement(node) && t.isJSXIdentifier(node.openingElement.name, { name: 'ProseText' })) {
      node.openingElement.attributes.forEach((a) => {
        if (t.isJSXAttribute(a) && a.name.name === 'text' && t.isJSXExpressionContainer(a.value)) {
          let e = a.value.expression;
          while (t.isCallExpression(e) || t.isOptionalCallExpression(e)) e = e.callee.object || e.callee;
          const p = dotted(e);
          if (p) wrapped.push(p);
        }
      });
    }
  });
  return { raw, wrapped };
}

test.each(Object.entries(PROSE))('%s renders every prose field through ProseText', (file, fields) => {
  const { raw, wrapped } = proseRenders(PAGES[file]);
  for (const field of fields) {
    expect(wrapped).toContain(field);
    expect(raw).not.toContain(field);
  }
});

test.each([
  ['a child', '<Box>{risk.mitigation}</Box>'],
  ['a child on its own lines', '<Box>\n  {\n    risk.mitigation\n  }\n</Box>'],
  ['after a label', '<Box><b>x:</b> {risk.mitigation}\n</Box>'],
  ['optional chaining', '<Box>{synthesis?.summary}</Box>'],
  ['a fallback', "<Box>{synthesis?.summary || 'No summary'}</Box>"],
  ['a guard', '<Box>{risk.mitigation && risk.mitigation}</Box>'],
  ['a ternary', "<Box>{x ? risk.mitigation : '-'}</Box>"],
  ['a table cell', "const c = [{ cell: q => q.reason || '-' }];"],
])('the check finds a raw render as %s', (_name, code) => {
  const { raw } = proseRenders(`const A = () => (${code.startsWith('const') ? 'null' : code});\n${code.startsWith('const') ? code : ''}`);
  expect(raw.some((p) => ['risk.mitigation', 'synthesis.summary', 'q.reason'].includes(p))).toBe(true);
});

test('the check takes a ProseText render for a wrapped one', () => {
  const { raw, wrapped } = proseRenders(
    "const A = () => (<Box>{synthesis?.summary ? <ProseText text={synthesis.summary} /> : 'none'}</Box>);",
  );
  expect(wrapped).toEqual(['synthesis.summary']);
  expect(raw).not.toContain('synthesis.summary');
});

test('paragraphs renders one paragraph per line, each with its code spans (#1403)', () => {
  act(() => root.render(
    <ProseText text={'Recommendation: move `x`.\n\nRisk: low.\nDecision: approve wave 1.'} paragraphs />,
  ));
  const ps = container.querySelectorAll('p');
  expect([...ps].map((p) => p.textContent)).toEqual([
    'Recommendation: move x.',
    'Risk: low.',
    'Decision: approve wave 1.',
  ]);
  expect(ps[0].querySelector('code').textContent).toBe('x');
});
