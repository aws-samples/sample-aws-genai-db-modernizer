// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useState } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import { assumptionValueText } from '../utils/reportFacts';

/**
 * #2316: the assumptions as a two-column table (assumption, value) with where each value
 * comes from as a badge, and an Info link for the sentence that explains it. The results
 * page and the assignment page both show it. `items` are the report's assumption items
 * (key, setting, origin, line); the value text only formats `setting`, and the sentence
 * is the item's own line. `renderEdit(item)` may add a control under a value.
 */
const AssumptionsTable = memo(({ items, t, renderEdit }) => {
  const [open, setOpen] = useState({});
  const toggle = (key) => setOpen((prev) => ({ ...prev, [key]: !prev[key] }));
  const name = (item) => t(`analysis-results.assumptions-panel.name.${item.key}`, { defaultValue: item.key });
  return (
    <div data-figure="assumptions-table">
      <Table
        variant="embedded"
        trackBy="key"
        // Names the table, and the scroll region a narrow screen wraps it in (axe:
        // landmark-unique, #2458).
        ariaLabels={{ tableLabel: t('analysis-results.assumptions-panel.title') }}
        items={items}
        columnDefinitions={[
          {
            id: 'assumption',
            header: t('analysis-results.assumptions-panel.column.assumption'),
            cell: (item) => (
              <SpaceBetween size="xxs">
                <SpaceBetween direction="horizontal" size="xs">
                  <span>{name(item)}</span>
                  <Link
                    variant="info"
                    ariaLabel={t('analysis-results.assumptions-panel.info-label', { name: name(item) })}
                    nativeAttributes={{ 'aria-expanded': open[item.key] ? 'true' : 'false', 'aria-controls': `assumption-line-${item.key}` }}
                    onFollow={() => toggle(item.key)}
                  >
                    {t('analysis-results.assumptions-panel.info')}
                  </Link>
                </SpaceBetween>
                <div id={`assumption-line-${item.key}`} hidden={!open[item.key]}>
                  {open[item.key] && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      <span data-figure="assumption-line">{item.line}.</span>
                    </Box>
                  )}
                </div>
              </SpaceBetween>
            ),
          },
          {
            id: 'value',
            header: t('analysis-results.assumptions-panel.column.value'),
            cell: (item) => (
              <SpaceBetween size="xs">
                <span data-figure="assumption-value" data-assumption={item.key}>{assumptionValueText(t, item)}</span>
                {renderEdit ? renderEdit(item) : null}
              </SpaceBetween>
            ),
          },
          {
            id: 'origin',
            header: t('analysis-results.assumptions-panel.column.origin'),
            cell: (item) => (
              <Badge color={item.origin === 'default' || item.origin === 'collected' ? 'grey' : 'blue'}>
                {t(`analysis-results.assumptions-panel.origin.${item.origin}`, { defaultValue: item.origin })}
              </Badge>
            ),
          },
        ]}
      />
    </div>
  );
});

export default AssumptionsTable;
