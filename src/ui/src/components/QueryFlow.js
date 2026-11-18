// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import Button from '@cloudscape-design/components/button';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import ChartSankey from './ChartSankey';
import { displayEngine } from '../utils/engineNames';
import { formatCount } from '../utils/format';
import { queryFlowLabel, queryFlowRows } from '../utils/queryFlow';

/**
 * The query-flow chart with its text alternative (#2317): the drawing is one image with
 * an accessible name that states the split, and "Show as a table" lists the same numbers
 * (the same graph, so they never differ). With `onNodeClick`, each engine in the table is
 * a button that does what a click on its node does: the keyboard way to it.
 */
export default function QueryFlow({ data, total, height, onNodeClick, unavailableTitle }) {
  const { t } = useTranslation();
  const label = useMemo(() => queryFlowLabel(data, t, total), [data, t, total]);
  const items = useMemo(() => {
    const { owners, cache } = queryFlowRows(data);
    return [
      ...owners.map((row) => ({ ...row, isCacheLayer: false })),
      ...cache.map((row) => ({ ...row, isCacheLayer: true })),
    ];
  }, [data]);

  const engineCell = (item) => {
    const name = displayEngine(item.engine) + (item.isCacheLayer ? t('cache-layer.engine-badge-suffix') : '');
    if (!onNodeClick || item.isCacheLayer) return name;
    return (
      <Button
        variant="inline-link"
        ariaLabel={t('query-flow.view-engine-results', { engine: name })}
        onClick={() => onNodeClick(item.engine)}
      >
        {name}
      </Button>
    );
  };

  return (
    <SpaceBetween size="s">
      <ChartSankey height={height} data={data} onNodeClick={onNodeClick} unavailableTitle={unavailableTitle} ariaLabel={label} />
      <ExpandableSection headerText={t('query-flow.show-table')}>
        <Table
          variant="embedded"
          items={items}
          trackBy="engine"
          ariaLabels={{ tableLabel: t('query-flow.table-label') }}
          columnDefinitions={[
            { id: 'engine', header: t('query-flow.col-engine'), cell: engineCell },
            { id: 'queries', header: t('query-flow.col-queries'), cell: (item) => formatCount(item.queries) },
          ]}
        />
      </ExpandableSection>
    </SpaceBetween>
  );
}
