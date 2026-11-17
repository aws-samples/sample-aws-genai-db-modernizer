// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { useTranslation } from 'react-i18next';
import Box from '@cloudscape-design/components/box';
import Popover from '@cloudscape-design/components/popover';
import { displayEngine } from '../utils/engineNames';
import { destinationKey } from '../utils/destinations';

// #2320: how many destination names the explorer's source-table cell shows before "+N more".
export const VISIBLE_DESTINATIONS = 3;

// Each destination once, in first-seen order. A destination is an engine and a name: the same
// name on two engines is two destinations. Blanks and the no-destination dash are dropped.
// This is presentation of a list the page already shows, not a figure from report.json facts.
export const uniqueDestinations = (destinations) => {
  const seen = new Set();
  const out = [];
  (destinations || []).forEach((d) => {
    const name = typeof d?.name === 'string' ? d.name.trim() : '';
    if (!name || name === '—') return;
    const key = destinationKey(d.engine, name);
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ engine: d.engine, name });
  });
  return out;
};

// The explorer's "Destination tables" cell for one source table. Names render as React text,
// never as markup. A name that two engines share carries its engine, so the two read as two.
export default function DestinationTables({ destinations }) {
  const { t } = useTranslation();
  const all = uniqueDestinations(destinations);
  if (all.length === 0) return '—';
  const enginesByName = {};
  all.forEach(({ engine, name }) => {
    (enginesByName[name] = enginesByName[name] || new Set()).add(engine);
  });
  const label = ({ engine, name }) => (enginesByName[name].size > 1 ? `${name} (${displayEngine(engine)})` : name);
  const head = all.slice(0, VISIBLE_DESTINATIONS);
  const rest = all.slice(VISIBLE_DESTINATIONS);
  return (
    <div style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
      <Box fontSize="body-s">
        {head.map((d, i) => (
          <span key={destinationKey(d.engine, d.name)}>
            {i > 0 ? ', ' : ''}
            <span data-testid="dest-name">{label(d)}</span>
          </span>
        ))}
        {rest.length > 0 && (
          <span data-testid="dest-more">
            {', '}
            <Popover
              triggerType="text"
              size="medium"
              dismissButton={false}
              triggerAriaLabel={t('analysis-results.explorer.destinations-more-aria', { count: rest.length })}
              content={
                <ul style={{ margin: 0, paddingInlineStart: '1.2em' }}>
                  {rest.map((d) => (
                    <li key={destinationKey(d.engine, d.name)} data-testid="dest-popover-name">{label(d)}</li>
                  ))}
                </ul>
              }
            >
              {t('analysis-results.explorer.destinations-more', { count: rest.length })}
            </Popover>
          </span>
        )}
      </Box>
    </div>
  );
}
