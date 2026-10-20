/**
 * #1028: the results page shows the headline first and the detail on demand.
 * Section order, from the top: the three deliverable buttons, the executive
 * summary, the query flow (Sankey), the cost breakdown, the migration roadmap,
 * the fallback-engine warning and close-call banners, then query routing
 * (collapsed, see QueryRoutingTable.test.js), then the rest. #1232: the
 * access pattern explorer, then Sources of truth and replicas (collapsed, see
 * SourcesOfTruthCallout.test.js); the Table design (once "Aurora design")
 * comes later, every section of it collapsed.
 *
 * The page itself cannot render under this repo's Jest config (Cloudscape ships
 * ESM it cannot transform, see AnalysisResultsSummaryFallback.test.js), so this
 * reads the order of the sections in the page's JSX with the same parser the
 * renderers-only-format guard uses. The browser order is checked by the UI
 * smoke test (tests/e2e/test_ui.py).
 */
const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');

// A section is found by its component name or by its id attribute.
const SECTIONS = [
  ['deliverables', el => el.name === 'DeliverablesPanel'],
  ['executive-summary', el => el.id === 'executive-summary-section'],
  ['query-flow', el => el.id === 'query-flow-section'],
  ['cost-breakdown', el => el.id === 'cost-breakdown-section'],
  ['migration-roadmap', el => el.name === 'MigrationWavePlan'],
  ['unservable', el => el.name === 'UnservableQueriesCallout'],
  ['close-calls', el => el.name === 'CloseCallCallout'],
  ['query-routing', el => el.name === 'QueryRoutingTable'],
  ['access-patterns', el => el.id === 'access-patterns-section'],
  ['sources-of-truth', el => el.name === 'SourcesOfTruthCallout'],
  ['table-design', el => el.id === 'table-design-section'],
];

function openingElements(node, out) {
  if (!node || typeof node.type !== 'string') return out;
  if (node.type === 'JSXOpeningElement') {
    const idAttr = node.attributes.find(a => a.type === 'JSXAttribute' && a.name.name === 'id');
    out.push({
      node,
      start: node.start,
      name: node.name.type === 'JSXIdentifier' ? node.name.name : null,
      id: idAttr && idAttr.value && idAttr.value.type === 'StringLiteral' ? idAttr.value.value : null,
    });
  }
  Object.keys(node).forEach((key) => {
    if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') return;
    const v = node[key];
    if (Array.isArray(v)) v.forEach(c => openingElements(c, out));
    else if (v && typeof v === 'object') openingElements(v, out);
  });
  return out;
}

test('the sections appear once each, headline first, in the order the issue sets', () => {
  const ast = parse(SOURCE, { sourceType: 'module', plugins: ['jsx', 'optionalChaining', 'classProperties'] });
  const elements = openingElements(ast.program, []).sort((a, b) => a.start - b.start);
  const found = SECTIONS.map(([name, match]) => {
    const hits = elements.filter(match);
    expect([name, hits.length]).toEqual([name, 1]);
    return [name, hits[0].start];
  });
  const order = [...found].sort((a, b) => a[1] - b[1]).map(([name]) => name);
  expect(order).toEqual(SECTIONS.map(([name]) => name));
});

test('the old deliverables section lower on the page is gone', () => {
  // DeliverablesPanel is rendered once, before the executive summary (above);
  // it no longer carries a "Deliverables" container header of its own.
  const panel = fs.readFileSync(path.join(__dirname, '..', '..', 'components', 'DeliverablesPanel.js'), 'utf8');
  expect(panel).not.toMatch(/components\/container/);
});

test('the Table design opens with every section collapsed', () => {
  const ast = parse(SOURCE, { sourceType: 'module', plugins: ['jsx', 'optionalChaining', 'classProperties'] });
  const elements = openingElements(ast.program, []);
  const design = elements.find(el => el.id === 'table-design-section');
  // The Container element whose opening tag carries the id spans the whole section.
  const container = findElement(ast.program, design.node);
  const sections = openingElements(container, []).filter(el => el.name === 'ExpandableSection');
  expect(sections.length).toBeGreaterThanOrEqual(5);
  sections.forEach((el) => {
    const attr = el.node.attributes.find(a => a.type === 'JSXAttribute' && a.name.name === 'defaultExpanded');
    expect([el.start, attr && attr.value.expression && attr.value.expression.type]).toEqual([el.start, 'BooleanLiteral']);
    expect([el.start, attr.value.expression.value]).toEqual([el.start, false]);
  });
});

test('the Table design is called that, not "Aurora design"', () => {
  const en = require('../../locales/en.json');
  expect(en['analysis-results.aurora-design.title']).toBe('Table design');
  expect(Object.values(en).filter(v => /Aurora design/.test(v))).toEqual([]);
});

// The JSXElement whose opening element is `opening`.
function findElement(node, opening) {
  if (!node || typeof node.type !== 'string') return null;
  if (node.type === 'JSXElement' && node.openingElement === opening) return node;
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') continue;
    const v = node[key];
    const kids = Array.isArray(v) ? v : [v];
    for (const c of kids) {
      if (c && typeof c === 'object') {
        const hit = findElement(c, opening);
        if (hit) return hit;
      }
    }
  }
  return null;
}
