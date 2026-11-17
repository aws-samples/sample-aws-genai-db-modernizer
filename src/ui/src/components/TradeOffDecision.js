// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from '@cloudscape-design/components/box';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import EngineBadge from './EngineBadge';
import ProseText from './ProseText';
import { formatShortId } from '../utils/format';

// A query id is a 64-character hash; formatShortId's eight characters (the engineering
// report's length) tell the ids apart on a page, and the full id stays in the button's
// title and its accessible name.
/**
 * One query id of a trade-off, as a button that opens the query's history (#2323). The
 * text is the first 8 characters; the title carries the full id. The colours come from
 * the `--dbm-id-chip-*` theme variables in styles/global.css, so the chip reads in both
 * the light and the dark theme. The id is untrusted text: React renders it as text.
 */
export function QueryIdChip({ id, onOpen, t }) {
  const full = String(id);
  return (
    <button
      type="button"
      className="dbm-id-chip"
      title={full}
      aria-label={t('analysis-results.query-routing.open-label', { id: full })}
      onClick={() => onOpen(full)}
    >
      {formatShortId(full)}
    </button>
  );
}

/**
 * One design decision (#2323), open or closed as its parent says (`expanded`, changed
 * through `onToggle(id, expanded)`): the decision as plain text in the header of an
 * expandable section, with the reasoning, the table mapping and the query ids in the
 * body. The header is a Cloudscape button, so Enter and Space open and close it. The
 * mapping chips use the shared EngineBadge: the target tables in the tab engine's
 * catalog colour, the source tables in the neutral grey. It only formats the trade-off
 * (ADR-032); every field is untrusted text, rendered as text.
 */
function TradeOffDecision({ id, tradeOff, engine, onOpenQuery, expanded, onToggle, headingLevel, t }) {
  const sources = tradeOff.source_tables || [];
  const targets = tradeOff.target_tables || [];
  const ids = tradeOff.query_ids || [];
  // One keyed list: source tables, an arrow, then target tables.
  const mappingChips = [
    ...sources.map((table, i) => (
      <span key={`s${i}`} data-mapping="source"><EngineBadge>{table}</EngineBadge></span>
    )),
    ...(sources.length > 0 && targets.length > 0 ? [<span key="arrow" aria-hidden="true">→</span>] : []),
    ...targets.map((table, i) => (
      <span key={`t${i}`} data-mapping="target" data-engine={engine}>
        <EngineBadge engine={engine}>{table}</EngineBadge>
      </span>
    )),
  ];
  return (
    <div className="dbm-card dbm-card--info" data-trade-off-decision="">
      <Box padding={{ vertical: 'xs', horizontal: 's' }}>
        <ExpandableSection
          variant="inline"
          headingTagOverride={headingLevel}
          expanded={expanded}
          onChange={({ detail }) => onToggle(id, detail.expanded)}
          headerText={<ProseText text={tradeOff.description} />}
        >
          <SpaceBetween size="xs">
            {tradeOff.impact ? (
              <Box fontSize="body-s" color="text-body-secondary"><ProseText text={tradeOff.impact} /></Box>
            ) : null}
            {(sources.length > 0 || targets.length > 0) && (
              <div>
                <Box variant="awsui-key-label">{t('analysis-results.tradeoffs.mapping')}</Box>
                <span className="dbm-chip-row">{mappingChips}</span>
              </div>
            )}
            {ids.length > 0 && (
              <div>
                <Box variant="awsui-key-label">{t('analysis-results.tradeoffs.sql-ids')}</Box>
                <span className="dbm-chip-row">
                  {ids.map((qid, i) => <QueryIdChip key={i} id={qid} onOpen={onOpenQuery} t={t} />)}
                </span>
              </div>
            )}
          </SpaceBetween>
        </ExpandableSection>
      </Box>
    </div>
  );
}

export default memo(TradeOffDecision);
