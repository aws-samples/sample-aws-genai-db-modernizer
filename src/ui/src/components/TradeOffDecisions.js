// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useDeferredValue, useMemo, useState } from 'react';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import TextFilter from '@cloudscape-design/components/text-filter';
import TradeOffDecision from './TradeOffDecision';
import TradeOffTopicGroups from './TradeOffTopicGroups';

/** True when `text` (case-insensitive) is in the decision's text, impact, tables or query ids. */
export function decisionMatches(tradeOff, text) {
  const needle = String(text || '').trim().toLowerCase();
  if (!needle) return true;
  const hay = [
    tradeOff.description,
    tradeOff.impact,
    ...(tradeOff.source_tables || []),
    ...(tradeOff.target_tables || []),
    ...(tradeOff.query_ids || []),
  ];
  return hay.some((part) => part != null && String(part).toLowerCase().includes(needle));
}

/**
 * An engine's design decisions with a text filter (#2323), on the results page's
 * "Trade-offs and design decisions" tab. The filter only narrows the list on screen:
 * it matches the displayed text, tables and query ids. Topic headings keep the facts'
 * counts when nothing is typed; while a filter is active they show the label alone, and
 * the filter's own count says how many decisions are listed. `groups` are the facts'
 * topic groups (or null), `decisions` the flat fallback list.
 */
export default function TradeOffDecisions({ groups, decisions, engine, onOpenQuery, t }) {
  const [typed, setTyped] = useState('');
  // What the reader opened or closed, by decision key. It lives here, not in each
  // decision, so a decision the filter hides and shows again keeps its state.
  const [chosen, setChosen] = useState({});
  const onToggle = useCallback((id, expanded) => setChosen((c) => ({ ...c, [id]: expanded })), []);
  // A new filter lets a decision the reader closed be shown open again by its match.
  const onFilter = (text) => {
    setTyped(text);
    setChosen((c) => Object.fromEntries(Object.entries(c).filter(([, v]) => v)));
  };
  // The list filters on the deferred text, so typing stays responsive on a long list.
  const filteringText = useDeferredValue(typed);
  const all = useMemo(() => (groups ? groups.flatMap((g) => g.tradeOffs) : decisions), [groups, decisions]);
  // A decision's key is its place in the unfiltered list, so its open state stays with it
  // while the filter changes which decisions are listed.
  const keyOf = useMemo(
    () => new Map(all.map((d, i) => [d, `${engine}-${i}`])),
    [all, engine],
  );
  if (all.length === 0) return null;
  const active = filteringText.trim() !== '';
  // Heading order: a decision sits one level under the heading above it. With topic
  // headings (h3, under the section's h2) that is h4; with none, the section's h2 is.
  const titled = groups ? !(groups.length === 1 && groups[0].topic === 'other') : false;
  const headingLevel = titled ? 'h4' : 'h3';
  const keep = (tradeOff) => decisionMatches(tradeOff, filteringText);
  const shownGroups = groups && (active
    ? groups.map((g) => ({ ...g, tradeOffs: g.tradeOffs.filter(keep) })).filter((g) => g.tradeOffs.length > 0)
    : groups);
  const shownFlat = groups ? [] : decisions.filter(keep);
  const shownCount = groups ? shownGroups.reduce((n, g) => n + g.tradeOffs.length, 0) : shownFlat.length;
  const renderOne = (tradeOff) => {
    const id = keyOf.get(tradeOff);
    // A decision the filter matched only in its details is shown open, so the reader sees why.
    const revealed = active && !decisionMatches({ description: tradeOff.description }, filteringText);
    return (
      <TradeOffDecision
        key={id}
        id={id}
        tradeOff={tradeOff}
        engine={engine}
        onOpenQuery={onOpenQuery}
        expanded={chosen[id] ?? revealed}
        onToggle={onToggle}
        headingLevel={headingLevel}
        t={t}
      />
    );
  };
  return (
    <SpaceBetween size="m">
      <TextFilter
        filteringText={typed}
        filteringPlaceholder={t('analysis-results.tradeoffs.filter-placeholder')}
        filteringAriaLabel={t('analysis-results.tradeoffs.filter-label')}
        countText={t('analysis-results.tradeoffs.match-count', { count: shownCount })}
        onChange={({ detail }) => onFilter(detail.filteringText)}
      />
      {active && shownCount === 0 && (
        <Box color="text-body-secondary">{t('analysis-results.tradeoffs.no-match')}</Box>
      )}
      {groups ? (
        <TradeOffTopicGroups groups={shownGroups} renderTradeOff={renderOne} showCounts={!active} titled={titled} t={t} />
      ) : (
        <SpaceBetween size="xs">{shownFlat.map(renderOne)}</SpaceBetween>
      )}
    </SpaceBetween>
  );
}
