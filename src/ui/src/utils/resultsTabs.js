// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #2313: the results page's tabs, one per reader question, in the order of the
// decision: what to do and what it costs (Summary), how to do it (Plan), the evidence
// (Queries, Data model), and what can go wrong (Risks and decisions). Each tab lists
// its sections by the id the section renders, in the order the tab shows them; a
// section with a labelKey is listed in the tab's "On this page" navigation.
//
// The URL names the tab (`?tab=`), so a tab can be linked. A hash naming a section
// (an old anchor, or a #2300 `?engine=` link) lands on that section's tab.

import { formatCount } from './format';
import { resultsTabCountOf } from './reportFacts';

export const RESULTS_TAB_PARAM = 'tab';
export const DEFAULT_RESULTS_TAB = 'summary';

const section = (id, labelKey = null) => ({ id, labelKey });
const anchor = name => `analysis-results.on-this-page.${name}`;

export const RESULTS_TABS = [
  {
    id: 'summary',
    labelKey: 'analysis-results.tabs.summary',
    sections: [
      section('executive-summary-section', anchor('executive-summary')),
      section('cost-breakdown-section', anchor('cost-breakdown')),
      section('query-flow-section', anchor('query-flow')),
      section('assumptions-panel', anchor('assumptions')),
    ],
  },
  {
    id: 'plan',
    labelKey: 'analysis-results.tabs.plan',
    sections: [
      section('migration-roadmap-section', anchor('migration-roadmap')),
      section('step-gate-section', anchor('step-gate')),
      section('unservable-section', anchor('unservable')),
      section('close-calls-section', anchor('close-calls')),
      // A one-line note on the assignment: on the tab, not in its navigation.
      section('unreviewed-moves-note'),
    ],
  },
  {
    id: 'queries',
    labelKey: 'analysis-results.tabs.queries',
    sections: [
      section('query-routing-section', anchor('query-routing')),
    ],
  },
  {
    id: 'data-model',
    labelKey: 'analysis-results.tabs.data-model',
    sections: [
      section('table-owners-section', anchor('table-owners')),
      section('table-design-section', anchor('table-design')),
      section('source-tables-section', anchor('source-tables')),
    ],
  },
  {
    id: 'risks',
    labelKey: 'analysis-results.tabs.risks',
    sections: [
      section('risk-assessment-section', anchor('risk-assessment')),
      section('dynamodb-merge-conflicts', anchor('merge-conflicts')),
      section('tradeoffs-section', anchor('tradeoffs')),
      section('dynamodb-merge-reviews', anchor('merge-reviews')),
    ],
  },
];

const TAB_IDS = RESULTS_TABS.map(tab => tab.id);

// Anchors inside a section that a link can name: they land on the section's tab.
const NESTED_ANCHORS = {
  'target-engines': 'summary',
  // #2318: in wave 1 of the roadmap, not sections of their own.
  'routines-section': 'plan',
  'engine-choices-note': 'plan',
  'no-open-risks-note': 'risks',
  // #2321: the section a link made before it joined Table owners named.
  'sources-of-truth-section': 'data-model',
};

const TAB_OF_ANCHOR = RESULTS_TABS.reduce(
  (map, tab) => {
    tab.sections.forEach((s) => { map[s.id] = tab.id; });
    return map;
  },
  { ...NESTED_ANCHORS },
);

// The tab that holds the section (or the anchor inside one), or null when no tab does
// (the deliverables above the tabs, or an id the page does not know).
export function tabOfAnchor(id) {
  return (id && Object.prototype.hasOwnProperty.call(TAB_OF_ANCHOR, id)) ? TAB_OF_ANCHOR[id] : null;
}

// #2446: sections that were folded into another. A link made before keeps landing:
// the access pattern explorer's rows are the query table's now.
const SECTION_ALIASES = {
  'access-patterns-section': 'query-routing-section',
};

// The id a location hash names, without its '#', or null for none. A folded
// section's old id names the section that holds it now.
export function hashId(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  if (!raw) return null;
  let id;
  try {
    id = decodeURIComponent(raw);
  } catch {
    id = raw;
  }
  return Object.prototype.hasOwnProperty.call(SECTION_ALIASES, id) ? SECTION_ALIASES[id] : id;
}

// The tab a URL opens: the tab of the section its hash names, else the tab `?tab=`
// names, else the summary. The hash wins because a link to a section is to that
// section, whatever tab the page was on when the link was made.
export function resultsTabFromLocation(search, hash) {
  const fromHash = tabOfAnchor(hashId(hash));
  if (fromHash) return fromHash;
  const named = new URLSearchParams(search || '').get(RESULTS_TAB_PARAM);
  return TAB_IDS.includes(named) ? named : DEFAULT_RESULTS_TAB;
}

// The tab `?tab=` names, as written (null when none).
export function tabParamOf(search) {
  return new URLSearchParams(search || '').get(RESULTS_TAB_PARAM);
}

// The search string with `?tab=` set to the tab, every other parameter kept.
export function searchWithTab(search, tabId) {
  const params = new URLSearchParams(search || '');
  params.set(RESULTS_TAB_PARAM, tabId);
  return `?${params.toString()}`;
}

// The "On this page" anchors of a tab: its listed sections that are on the page
// (`presentIds`), in the tab's order.
export function onThisPageAnchors(tabId, presentIds, t) {
  const tab = RESULTS_TABS.find(x => x.id === tabId);
  if (!tab) return [];
  const present = new Set(presentIds || []);
  return tab.sections
    .filter(s => s.labelKey && present.has(s.id))
    .map(s => ({ text: t(s.labelKey), href: `#${s.id}`, level: 1 }));
}

// Every section id of a tab, in its order.
export function sectionIdsOf(tabId) {
  const tab = RESULTS_TABS.find(x => x.id === tabId);
  return tab ? tab.sections.map(s => s.id) : [];
}

// #2363: a tab's label with the count the facts state for it (resultsTabCountOf), "Queries
// (107)". The page only formats the figure, it never counts; a tab the facts give no count for,
// or a report saved before them, shows the plain label.
export function tabLabel(t, tab, synthesis) {
  const label = t(tab.labelKey);
  const n = resultsTabCountOf(synthesis, tab.id);
  if (n === null) return label;
  return t('analysis-results.tabs.with-count', { label, number: formatCount(n) });
}
