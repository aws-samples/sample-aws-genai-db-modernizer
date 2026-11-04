// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";

/**
 * An engine's design trade-offs by topic (#1221), on the results page's
 * "Trade-offs and design decisions" tab: each topic's label and count, then its
 * trade-offs, as `tradeOffTopicGroupsOf` reads them from report.json's
 * facts.trade_off_topics -- the groups the engineering report lists under the
 * same labels. It only formats the facts (ADR-032); `renderTradeOff` draws one
 * trade-off (the page's own card).
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * CloseCallCallout.
 */
export default function TradeOffTopicGroups({ groups, renderTradeOff, t }) {
  // A design saved before the topics has every trade-off under "other": no heading,
  // as in the engineering report.
  const titled = !(groups.length === 1 && groups[0].topic === 'other');
  return (
    <SpaceBetween size="m">
      {groups.map(g => (
        <section key={g.topic} data-trade-off-topic={g.topic}>
          <SpaceBetween size="xs">
            {titled && (
              <Box variant="h3">
                {t('analysis-results.tradeoffs.topic-heading', { label: g.label, count: g.count })}
              </Box>
            )}
            {g.tradeOffs.map((tradeOff, idx) => renderTradeOff(tradeOff, `${g.topic}-${idx}`))}
          </SpaceBetween>
        </section>
      ))}
    </SpaceBetween>
  );
}
