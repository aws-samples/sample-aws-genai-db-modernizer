// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2313: how the results page wires its tabs. The page cannot render under this repo's
 * Jest config (see AnalysisResultsSectionOrder.test.js), so this reads its JSX; what
 * the wiring does is tested on its parts: useResultsTabs (the URL, the landing on a
 * linked section), ResultsOnThisPage (the anchor navigation) and resultsTabs (which
 * tab holds which section). The browser checks are in tests/e2e/test_ui.py.
 */
const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');
const AST = parse(SOURCE, { sourceType: 'module', plugins: ['jsx', 'optionalChaining', 'classProperties'] });

function jsxElements(node, out = []) {
  if (!node || typeof node.type !== 'string') return out;
  if (node.type === 'JSXElement') out.push(node);
  Object.keys(node).forEach((key) => {
    if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') return;
    const v = node[key];
    if (Array.isArray(v)) v.forEach(c => jsxElements(c, out));
    else if (v && typeof v === 'object') jsxElements(v, out);
  });
  return out;
}
const nameOf = el => (el.openingElement.name.type === 'JSXIdentifier' ? el.openingElement.name.name : null);
const attr = (el, name) => el.openingElement.attributes.find(a => a.type === 'JSXAttribute' && a.name.name === name);
const src = node => SOURCE.slice(node.start, node.end);

const ELEMENTS = jsxElements(AST.program);
const RESULTS_TABS_ELEMENT = ELEMENTS.filter(el => nameOf(el) === 'Tabs' && attr(el, 'ariaLabel'));
// The AppLayout of the loaded page: the one that holds the tabs.
const LOADED_LAYOUT = ELEMENTS.filter(el => nameOf(el) === 'AppLayoutToolbar' && src(el).includes('ResultsOnThisPage'));

test('the tabs are the URL\'s: the active tab comes from useResultsTabs and a change goes back to it', () => {
  expect(SOURCE).toMatch(/import useResultsTabs, \{[^}]*\} from "\.\.\/hooks\/useResultsTabs"/);
  expect(SOURCE).toMatch(/const \{ activeTab, selectTab, goToSection \} = useResultsTabs\(/);
  expect(RESULTS_TABS_ELEMENT).toHaveLength(1);
  const tabs = RESULTS_TABS_ELEMENT[0];
  expect(src(attr(tabs, 'activeTabId').value)).toBe('{activeTab}');
  expect(src(attr(tabs, 'onChange').value)).toMatch(/selectTab\(detail\.activeTabId\)/);
});

test('every tab is listed from RESULTS_TABS with its content, kept once viewed', () => {
  const tabs = src(attr(RESULTS_TABS_ELEMENT[0], 'tabs').value);
  expect(tabs).toMatch(/RESULTS_TABS\.map\(/);
  expect(tabs).toMatch(/content: tabContent\[tab\.id\]/);
  // A tab the reader opened keeps its state (a section they expanded, a filter).
  expect(tabs).toMatch(/contentRenderStrategy: 'lazy'/);
});

test('each tab label carries the count the facts state, never one the page counts (#2363)', () => {
  const tabs = src(attr(RESULTS_TABS_ELEMENT[0], 'tabs').value);
  expect(tabs).toMatch(/label: tabLabel\(t, tab, synthesis\)/);
  expect(SOURCE).toMatch(/import \{[^}]*\btabLabel\b[^}]*\} from "\.\.\/utils\/resultsTabs"/);
});

test('On this page sits in the tools panel, listing the open tab\'s sections', () => {
  expect(LOADED_LAYOUT).toHaveLength(1);
  const layout = LOADED_LAYOUT[0];
  const tools = src(attr(layout, 'tools').value);
  expect(tools).toMatch(/<ResultsOnThisPage[^>]*anchors=\{onThisPage\}/);
  expect(tools).toMatch(/onGo=\{goToSection\}/);
  expect(SOURCE).toMatch(/const onThisPage = onThisPageAnchors\(activeTab, sectionsOnPage, t\)/);
  expect(SOURCE).toMatch(/useSectionsOnPage\(activeTab, tabsRoot\)/);
  // The tools panel is no longer hidden outright: only when a tab has one section or none.
  expect(SOURCE).toMatch(/const toolsHidden = onThisPage\.length < 2;/);
  const hide = attr(layout, 'toolsHide');
  expect(hide && hide.value && src(hide.value)).toBe('{toolsHidden}');
  expect(src(attr(layout, 'toolsOpen').value)).toBe('{toolsOpen && !toolsHidden}');
});

test('the loading layout controls its tools panel too, so it can open once the results arrive', () => {
  // React keeps the loading state's AppLayout when the results arrive, and Cloudscape
  // decides on its first render whether toolsOpen is controlled: left out there, the
  // panel would never open.
  const loading = ELEMENTS.filter(el => nameOf(el) === 'AppLayoutToolbar' && !src(el).includes('ResultsOnThisPage'));
  expect(loading).toHaveLength(1);
  expect(src(attr(loading[0], 'toolsOpen').value)).toBe('{false}');
  expect(attr(loading[0], 'toolsHide')).toBeTruthy();
});

test('the linked engine (?engine=) lands on its section through the tabs (#2300)', () => {
  expect(SOURCE).toMatch(/const sectionId = linkedSectionId\(target, locationHash\);/);
  expect(SOURCE).toMatch(/goToSection\(sectionId\)/);
  // The page no longer scrolls on its own: a section on a closed tab is not in the DOM.
  expect(SOURCE).not.toMatch(/getElementById\(sectionId\)/);
});

test('the page\'s own jumps go through the tabs too', () => {
  // The tabs' content is in the section navigation context, so a jump from one tab to
  // another (the roadmap's one-step option) opens the target's tab.
  const provider = ELEMENTS.filter(el => src(el.openingElement).startsWith('<ResultsSectionNav.Provider'));
  expect(provider).toHaveLength(1);
  expect(src(attr(provider[0], 'value').value)).toBe('{goToSection}');
  expect(provider[0].start).toBeLessThan(RESULTS_TABS_ELEMENT[0].start);
  expect(provider[0].end).toBeGreaterThan(RESULTS_TABS_ELEMENT[0].end);
  expect(SOURCE).not.toMatch(/getElementById\(/);
});

test('the risk assessment and the trade-offs carry the ids their anchors use', () => {
  const ids = ELEMENTS.map(el => attr(el, 'id')).filter(a => a && a.value && a.value.type === 'StringLiteral').map(a => a.value.value);
  expect(ids).toEqual(expect.arrayContaining(['risk-assessment-section', 'tradeoffs-section']));
  // One risk section id, whether or not any risk is open.
  expect(ids.filter(id => id === 'risk-assessment-section')).toHaveLength(1);
  const risk = ELEMENTS.find(el => attr(el, 'id') && attr(el, 'id').value.value === 'risk-assessment-section');
  expect(src(risk)).toMatch(/<NoOpenRisksSection/);
  expect(src(risk)).toMatch(/t\('analysis-results\.risk-assessment\.title'\)/);
});

test('the unserved queries (Plan) and the merge conflicts and reviews (Risks) point at each other', () => {
  expect(SOURCE).toMatch(/const relatedPointers = unservedPointers\(resultsData\?\.synthesis\);/);
  const pointers = ELEMENTS.filter(el => nameOf(el) === 'RelatedSectionPointer');
  expect(pointers.map(el => src(attr(el, 'target').value))).toEqual([
    '{relatedPointers.toRisks}',
    // One pointer back, after the section the banner's points at.
    "{relatedPointers.toRisks === 'dynamodb-merge-conflicts' ? relatedPointers.toPlan : null}",
    "{relatedPointers.toRisks === 'dynamodb-merge-reviews' ? relatedPointers.toPlan : null}",
  ]);
  // Each right after the section it belongs to.
  const after = name => ELEMENTS.find(el => nameOf(el) === name).end;
  expect(pointers[0].start).toBeGreaterThan(after('UnservableQueriesCallout'));
  expect(pointers[0].start).toBeLessThan(ELEMENTS.find(el => nameOf(el) === 'CloseCallCallout').start);
  expect(pointers[1].start).toBeGreaterThan(after('DynamoDBMergeConflictsCallout'));
  expect(pointers[2].start).toBeGreaterThan(after('DynamoDBMergeReviewsCallout'));
});
