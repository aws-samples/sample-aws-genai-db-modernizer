// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2313: the results page is split into tabs, one per reader question, and the URL
 * names the tab (`?tab=`), so a tab can be linked. A link that names a section in its
 * hash (an old anchor, or the `?engine=` links of #2300) lands on that section's tab.
 */
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import {
  DEFAULT_RESULTS_TAB,
  RESULTS_TABS,
  RESULTS_TAB_PARAM,
  hashId,
  onThisPageAnchors,
  resultsTabFromLocation,
  searchWithTab,
  tabLabel,
  tabOfAnchor,
} from '../resultsTabs';
import { linkedSectionId, RESULTS_ENGINE_SECTION_ID } from '../resultsLinks';

const t = (key) => en[key] || key;
const tabSections = (id) => RESULTS_TABS.find(tab => tab.id === id).sections.map(s => s.id);

test('five tabs in the reader\'s order: summary, plan, queries, data model, risks and decisions', () => {
  expect(RESULTS_TABS.map(tab => tab.id)).toEqual(['summary', 'plan', 'queries', 'data-model', 'risks']);
  expect(RESULTS_TABS.map(tab => t(tab.labelKey))).toEqual(
    ['Summary', 'Plan', 'Queries', 'Data model', 'Risks and decisions'],
  );
  expect(DEFAULT_RESULTS_TAB).toBe('summary');
  expect(RESULTS_TAB_PARAM).toBe('tab');
});

test('each tab holds its sections, in the order of the decision', () => {
  expect(tabSections('summary')).toEqual([
    'executive-summary-section', 'cost-breakdown-section', 'query-flow-section', 'assumptions-panel',
  ]);
  expect(tabSections('plan')).toEqual([
    'migration-roadmap-section', 'step-gate-section', 'unservable-section', 'close-calls-section',
    'unreviewed-moves-note',
  ]);
  expect(tabSections('queries')).toEqual(['query-routing-section', 'access-patterns-section']);
  expect(tabSections('data-model')).toEqual([
    'table-owners-section', 'table-design-section',
  ]);
  expect(tabSections('risks')).toEqual([
    'risk-assessment-section', 'dynamodb-merge-conflicts', 'tradeoffs-section', 'dynamodb-merge-reviews',
  ]);
});

test('a section is on exactly one tab', () => {
  const all = RESULTS_TABS.flatMap(tab => tab.sections.map(s => s.id));
  expect(new Set(all).size).toBe(all.length);
});

test('every tab and anchor label is an English string', () => {
  RESULTS_TABS.forEach((tab) => {
    expect([tab.labelKey, typeof en[tab.labelKey]]).toEqual([tab.labelKey, 'string']);
    tab.sections.filter(s => s.labelKey).forEach((s) => {
      expect([s.labelKey, typeof en[s.labelKey]]).toEqual([s.labelKey, 'string']);
    });
  });
  expect(en['analysis-results.on-this-page.title']).toBe('On this page');
});

describe('the tab a URL names', () => {
  test('no tab in the URL is the summary', () => {
    expect(resultsTabFromLocation('', '')).toBe('summary');
  });

  test('?tab= names the tab', () => {
    expect(resultsTabFromLocation('?tab=plan', '')).toBe('plan');
    expect(resultsTabFromLocation('?engine=dynamodb&tab=data-model', '')).toBe('data-model');
  });

  test('an unknown tab is the summary', () => {
    expect(resultsTabFromLocation('?tab=nope', '')).toBe('summary');
    expect(resultsTabFromLocation('?tab=', '')).toBe('summary');
  });

  test('an old anchor lands on its section\'s tab', () => {
    expect(resultsTabFromLocation('', '#sources-of-truth-section')).toBe('data-model');
    expect(resultsTabFromLocation('', '#close-calls-section')).toBe('plan');
    // #2318: the routines and the engine choices are in wave 1 of the roadmap, on the Plan tab.
    expect(resultsTabFromLocation('', '#routines-section')).toBe('plan');
    expect(resultsTabFromLocation('', '#engine-choices-note')).toBe('plan');
    expect(resultsTabFromLocation('', '#cost-breakdown-section')).toBe('summary');
    expect(resultsTabFromLocation('', '#query-routing-section')).toBe('queries');
    expect(resultsTabFromLocation('', '#risk-assessment-section')).toBe('risks');
  });

  test('anchors inside a section land on its tab too', () => {
    expect(tabOfAnchor('target-engines')).toBe('summary');
    expect(tabOfAnchor('no-open-risks-note')).toBe('risks');
    expect(resultsTabFromLocation('', '#target-engines')).toBe('summary');
  });

  test('the section a hash names wins over the tab in the query: the link is to the section', () => {
    expect(resultsTabFromLocation('?tab=plan', '#access-patterns-section')).toBe('queries');
  });

  test('a hash no tab holds leaves the tab the query names', () => {
    expect(resultsTabFromLocation('?tab=plan', '#deliverables-section')).toBe('plan');
    expect(tabOfAnchor('deliverables-section')).toBeNull();
    expect(tabOfAnchor('')).toBeNull();
  });

  test('the hash is read without its # and decoded', () => {
    expect(hashId('#table-design-section')).toBe('table-design-section');
    expect(hashId('#a%20b')).toBe('a b');
    expect(hashId('')).toBeNull();
    expect(hashId('#')).toBeNull();
    expect(hashId('#%E0%A4%A')).toBe('%E0%A4%A');
  });
});

describe('the URL of a tab', () => {
  test('sets ?tab= and keeps the other parameters', () => {
    expect(searchWithTab('', 'plan')).toBe('?tab=plan');
    expect(searchWithTab('?engine=dynamodb', 'queries')).toBe('?engine=dynamodb&tab=queries');
    expect(searchWithTab('?tab=plan&engine=dynamodb', 'risks')).toBe('?tab=risks&engine=dynamodb');
  });
});

describe('where an ?engine= link lands (#2300)', () => {
  test('an engine with explorer rows: the explorer, on the Queries tab', () => {
    const section = linkedSectionId('explorer', `#${RESULTS_ENGINE_SECTION_ID}`);
    expect(section).toBe('access-patterns-section');
    expect(tabOfAnchor(section)).toBe('queries');
  });

  test('a relational engine with a design: the Table design, on the Data model tab', () => {
    const section = linkedSectionId('design', `#${RESULTS_ENGINE_SECTION_ID}`);
    expect(section).toBe('table-design-section');
    expect(tabOfAnchor(section)).toBe('data-model');
  });

  test('an engine the plan has no part for stays on the section the link names', () => {
    expect(linkedSectionId('absent', '#access-patterns-section')).toBe('access-patterns-section');
    expect(linkedSectionId('not-designed', '#access-patterns-section')).toBe('access-patterns-section');
    expect(linkedSectionId('none', '')).toBeNull();
  });
});

describe('On this page', () => {
  test('lists the tab\'s sections that are on the page, in the tab\'s order', () => {
    const anchors = onThisPageAnchors('data-model', ['table-design-section', 'table-owners-section'], t);
    expect(anchors).toEqual([
      { text: 'Table owners', href: '#table-owners-section', level: 1 },
      { text: 'Table design', href: '#table-design-section', level: 1 },
    ]);
  });

  test('never lists another tab\'s section, or a note with no anchor of its own', () => {
    const anchors = onThisPageAnchors(
      'plan',
      ['migration-roadmap-section', 'engine-choices-note', 'unreviewed-moves-note', 'table-design-section'],
      t,
    );
    expect(anchors.map(a => a.href)).toEqual(['#migration-roadmap-section']);
  });

  test('an unknown tab lists nothing', () => {
    expect(onThisPageAnchors('nope', ['table-design-section'], t)).toEqual([]);
  });
});

// #2363: the counts on the tab labels are report facts (facts.results_tab_counts); the page
// formats them and never counts.
describe('the tab labels with their counts', () => {
  let tr;
  beforeAll(async () => {
    const i18n = createInstance();
    await i18n.init({ resources: { en: { translation: en } }, lng: 'en', interpolation: { escapeValue: false } });
    tr = i18n.t.bind(i18n);
  });
  const of = counts => ({ facts: { results_tab_counts: counts } });
  const tab = id => RESULTS_TABS.find(x => x.id === id);

  test('a tab with a count in the facts reads "Label (n)"', () => {
    const counts = { queries: 107, risks: 9 };
    const facts = { facts: { results_tab_counts: counts } };
    expect(tabLabel(tr, tab('queries'), facts)).toBe('Queries (107)');
    expect(tabLabel(tr, tab('risks'), facts)).toBe('Risks and decisions (9)');
  });

  test('a count is written with thousands separators', () => {
    expect(tabLabel(tr, tab('queries'), of({ queries: 1234 }))).toBe('Queries (1,234)');
  });

  test('a tab with no count in the facts, or a report with none, reads its plain label', () => {
    expect(tabLabel(tr, tab('summary'), of({ queries: 107 }))).toBe('Summary');
    expect(tabLabel(tr, tab('plan'), of({ queries: 107 }))).toBe('Plan');
    [null, undefined, {}, 'x'].forEach((counts) => {
      expect(tabLabel(tr, tab('queries'), { facts: { results_tab_counts: counts } })).toBe('Queries');
    });
  });

  test('a figure that is not a count is not shown', () => {
    ['9', null, -1, 2.5, NaN].forEach((bad) => {
      expect(tabLabel(tr, tab('risks'), of({ risks: bad }))).toBe('Risks and decisions');
    });
  });
});

// #2363 review: the guard reads the count through resultsTabCountOf, so arithmetic on it is a
// violation (ADR-032 Decision 2), and resultsTabs.js is a guarded file.
