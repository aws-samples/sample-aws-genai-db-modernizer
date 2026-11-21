// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { helpPageUrl, RESULTS_HELP_URL } from './helpSite';
import { adoptionOf } from './adoptionPaths';
import { migrationWaves, waveReadiness } from './migrationWaves';
import { tableOwnershipOf, tableSourcesOf } from './reportFacts';
import { WORKLOAD_FIT_DEFINITION } from './workloadFitLabels';

/**
 * #2512: the results page's help panels. A section header's Info link opens its topic in
 * the AppLayout drawer that shares the tools area with "On this page" (#2313). Each
 * topic holds the explanation the section used to state inline, so the section keeps
 * its facts; every panel ends with a link to the matching help site section.
 *
 * A topic's `paragraphs(t, context)` returns the panel's text. `context` comes from
 * `resultsHelpContext(synthesis)`: the facts a panel words its text by (it reads them,
 * never counts them; ADR-032). The sentences are the same strings the sections used to
 * render, so nothing a deliverable states changes.
 */

export const HELP_DRAWER_ID = 'help';
export const ON_THIS_PAGE_DRAWER_ID = 'on-this-page';
export const DEFAULT_HELP_TOPIC = 'results-page';

const PAGE = 'understand-the-results.md';
const key = (topic, name) => `analysis-results.help.${topic}.${name}`;

// #2166 review: the owner rule the report's own lines follow (facts.table_owner_lines.
// unwritten_owner_rule). A report saved before #2166 gave a table nothing writes to the
// engine serving most of its queries, so its panel states that rule.
export function ownerRuleKey(ownership) {
  return ownership.unwritten_owner_rule === 'most_queries'
    ? 'analysis-results.table-owners.rule-most-queries'
    : 'analysis-results.table-owners.rule';
}

export const HELP_TOPICS = {
  'results-page': {
    anchor: 'the-results-page',
    paragraphs: t => [t(key('results-page', 'body'))],
  },
  'migration-roadmap': {
    anchor: 'migration-waves',
    // The step, stage and unit definitions with the adoption paths that use them, and
    // the readiness rule with the readiness lines it explains, as the section had them.
    paragraphs: (t, context) => [
      t(key('migration-roadmap', 'body')),
      context.adoptionShown ? t('analysis-results.adoption.definitions') : null,
      t(key('migration-roadmap', 'groups')),
      context.readinessShown ? t('analysis-results.waves.readiness-rule') : null,
    ],
  },
  'close-calls': {
    anchor: 'close-calls',
    paragraphs: t => [t(key('close-calls', 'body')), t(key('close-calls', 'review'))],
  },
  'query-flow': {
    anchor: 'the-cache-layer',
    paragraphs: t => [t(key('query-flow', 'body')), t(key('query-flow', 'cache'))],
  },
  'query-routing': {
    anchor: 'how-to-read-an-assignment',
    paragraphs: t => [t(key('query-routing', 'body')), WORKLOAD_FIT_DEFINITION],
  },
  'table-owners': {
    anchor: 'sources-of-truth-and-replicas',
    // The CDC sentence led the section's consistency note, which a report with no CDC
    // copy (no replicated table, no shared risk sentence) does not have.
    paragraphs: (t, context) => [
      t(key('table-owners', 'body')),
      context.ownerRuleKey ? t(context.ownerRuleKey) : null,
      context.cdcShown ? t('analysis-results.sources-of-truth.intro') : null,
    ],
  },
};

export const HELP_TOPIC_IDS = Object.keys(HELP_TOPICS);

/** The topic `id` names, or the default one for an id no topic has. */
export function helpTopicOf(id) {
  return HELP_TOPICS[id] ? id : DEFAULT_HELP_TOPIC;
}

/** The help site section a topic's panel links to. */
export function helpTopicUrl(id) {
  const topic = helpTopicOf(id);
  return topic === DEFAULT_HELP_TOPIC ? RESULTS_HELP_URL : helpPageUrl(PAGE, HELP_TOPICS[topic].anchor);
}

/** The facts the panels word their text by. */
export function resultsHelpContext(synthesis) {
  const ownership = tableOwnershipOf(synthesis);
  const sources = tableSourcesOf(synthesis);
  return {
    cdcShown: Boolean(sources && (((sources.replicated_tables || []).length > 0) || sources.shared_risk_sentence)),
    ownerRuleKey: ownership ? ownerRuleKey(ownership) : null,
    adoptionShown: Boolean(adoptionOf(synthesis)),
    readinessShown: migrationWaves(synthesis).some(w => waveReadiness(synthesis, w)),
  };
}

/** The panel's title, paragraphs and help site link for topic `id`. */
export function helpPanelContent(id, t, context = {}) {
  const topic = helpTopicOf(id);
  return {
    id: topic,
    title: t(key(topic, 'title')),
    paragraphs: HELP_TOPICS[topic].paragraphs(t, context).filter(Boolean),
    url: helpTopicUrl(topic),
  };
}
