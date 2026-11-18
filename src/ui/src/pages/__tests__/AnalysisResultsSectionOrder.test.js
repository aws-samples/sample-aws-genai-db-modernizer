// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1028: the results page shows the headline first and the detail on demand.
 * #2313: the page is split into tabs, one per reader question (Summary, Plan,
 * Queries, Data model, Risks and decisions), and each tab orders its sections by
 * the decision. The deliverable buttons stay above the tabs. Which tab holds which
 * section, in which order, is RESULTS_TABS (src/ui/src/utils/resultsTabs.js, pinned
 * by resultsTabs.test.js); this test checks the page renders each section in its
 * tab's content, in that order, once.
 *
 * The page itself cannot render under this repo's Jest config (Cloudscape ships
 * ESM it cannot transform, see AnalysisResultsSummaryFallback.test.js), so this
 * reads the page's JSX with the same parser the renderers-only-format guard uses.
 * The browser order is checked by the UI smoke test (tests/e2e/test_ui.py).
 */
const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');
const { RESULTS_TABS } = require('../../utils/resultsTabs');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');
const AST = parse(SOURCE, { sourceType: 'module', plugins: ['jsx', 'optionalChaining', 'classProperties'] });

// A section is found by its component name (the component renders the id) or by its
// id attribute on the page.
const BY_COMPONENT = {
  'assumptions-panel': 'AssumptionsPanel',
  'migration-roadmap-section': 'MigrationWavePlan',
  'step-gate-section': 'StepGateEvidence',
  'unservable-section': 'UnservableQueriesCallout',
  'close-calls-section': 'CloseCallCallout',
  'unreviewed-moves-note': 'UnreviewedMovesNote',
  'query-routing-section': 'QueryRoutingTable',
  'table-owners-section': 'TableOwnersCallout',
  'sources-of-truth-section': 'SourcesOfTruthCallout',
  'dynamodb-merge-conflicts': 'DynamoDBMergeConflictsCallout',
  'dynamodb-merge-reviews': 'DynamoDBMergeReviewsCallout',
};
const matcher = id => (BY_COMPONENT[id] ? el => el.name === BY_COMPONENT[id] : el => el.id === id);

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

// The `tabContent` object the page passes to its Tabs: tab id -> that tab's JSX.
function tabContentObject() {
  let found = null;
  (function walk(node) {
    if (!node || typeof node.type !== 'string' || found) return;
    if (node.type === 'VariableDeclarator' && node.id.name === 'tabContent') { found = node.init; return; }
    Object.keys(node).forEach((key) => {
      if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') return;
      const v = node[key];
      if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v === 'object') walk(v);
    });
  }(AST.program));
  return found;
}

test('every tab has its content, and nothing else does', () => {
  const content = tabContentObject();
  expect(content && content.type).toBe('ObjectExpression');
  const keys = content.properties.map(p => (p.key.type === 'StringLiteral' ? p.key.value : p.key.name));
  expect(keys).toEqual(RESULTS_TABS.map(tab => tab.id));
});

describe.each(RESULTS_TABS.map(tab => [tab.id, tab.sections.map(s => s.id)]))('the %s tab', (tabId, sectionIds) => {
  test('holds its sections, once each, in the order of the decision', () => {
    const content = tabContentObject();
    const prop = content.properties.find(p => (p.key.name || p.key.value) === tabId);
    const elements = openingElements(prop.value, []).sort((a, b) => a.start - b.start);
    const found = sectionIds.map((id) => {
      const hits = elements.filter(matcher(id));
      expect([id, hits.length]).toEqual([id, 1]);
      return [id, hits[0].start];
    });
    const order = [...found].sort((a, b) => a[1] - b[1]).map(([id]) => id);
    expect(order).toEqual(sectionIds);
  });
});

test('each section is rendered once on the whole page, so it is on one tab only', () => {
  const elements = openingElements(AST.program, []);
  RESULTS_TABS.flatMap(tab => tab.sections.map(s => s.id)).forEach((id) => {
    expect([id, elements.filter(matcher(id)).length]).toEqual([id, 1]);
  });
});

test('each component a tab names renders the section id the tab and its anchor use', () => {
  Object.entries(BY_COMPONENT).forEach(([id, name]) => {
    const file = fs.readFileSync(path.join(__dirname, '..', '..', 'components', `${name}.js`), 'utf8');
    expect([name, file.includes(`id="${id}"`)]).toEqual([name, true]);
  });
});

test('the deliverable buttons sit above the tabs, on every tab', () => {
  const elements = openingElements(AST.program, []);
  const panel = elements.filter(el => el.name === 'DeliverablesPanel');
  expect(panel).toHaveLength(1);
  const tabs = elements.filter(el => el.name === 'Tabs' && el.node.attributes.some(a => a.name && a.name.name === 'ariaLabel'));
  expect(tabs).toHaveLength(1);
  expect(panel[0].start).toBeLessThan(tabs[0].start);
  const content = tabContentObject();
  expect(openingElements(content, []).some(el => el.name === 'DeliverablesPanel')).toBe(false);
});

test('the old deliverables section lower on the page is gone', () => {
  // DeliverablesPanel is rendered once, before the executive summary (above);
  // it no longer carries a "Deliverables" container header of its own.
  const panel = fs.readFileSync(path.join(__dirname, '..', '..', 'components', 'DeliverablesPanel.js'), 'utf8');
  expect(panel).not.toMatch(/components\/container/);
});

test('the Table design opens with every section collapsed', () => {
  const elements = openingElements(AST.program, []);
  const design = elements.find(el => el.id === 'table-design-section');
  // The Container element whose opening tag carries the id spans the whole section.
  const container = findElement(AST.program, design.node);
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
