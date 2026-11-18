// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import EngineBadge from './EngineBadge';
import { ENGINE_LABELS } from '../utils/engineNames';
import { formatMoney } from '../utils/format';
import {
  costAssumptionsOf,
  costCaveatPartsOf,
  costScopeLabelOf,
  costSizingBasisText,
  costTableOf,
  formatFigure,
  growthScenarioLinesOf,
  priceListOf,
  rateWindowScenarioOf,
  rateWindowStatementOf,
  rowCountStatementOf,
} from '../utils/reportFacts';

/**
 * #2316: the cost section, total first. The total is the biggest number; the engines
 * follow in a table with a total row, both read from facts.engines and
 * facts.engine_cost_total_usd, as the engineering and executive reports print them (a
 * report written before them falls back to its TCO breakdown); every note sits in one
 * expandable, and the one pricing caveat links to the calculator. Every figure and
 * sentence is a report fact (ADR-032): nothing is added up or reworded here.
 */
function CostBreakdown({ synthesis, costBreakdown = [], projectedCost = null, cacheEngine, t }) {
  const table = costTableOf(synthesis);
  const rows = table
    ? table.rows.map((e) => ({ key: e.engine, engine: e.engine, role: e.role || '', cost: e.monthly_cost_usd, sizing: e.cost_sizing }))
    : costBreakdown.map((cb) => ({ key: cb.database, engine: cb.database, role: '', cost: cb.monthly_cost_usd, sizing: null }));
  const total = table ? table.total : projectedCost;
  const hasTotal = total !== null && total !== undefined;
  const items = hasTotal ? [...rows, { key: '__total', total: true, cost: total }] : rows;

  const price = priceListOf(synthesis);
  const covers = costAssumptionsOf(synthesis);
  const basisNotes = (synthesis?.facts?.cost_basis_notes || []);
  const growth = growthScenarioLinesOf(synthesis);
  const rowCounts = rowCountStatementOf(synthesis);
  const rates = rateWindowStatementOf(synthesis);
  const scenario = rateWindowScenarioOf(synthesis);
  const how = [
    price?.pricing_model && { label: t('analysis-results.cost-breakdown.how.pricing-model'), value: price.pricing_model },
    price?.region && { label: t('analysis-results.cost-breakdown.how.region'), value: price.region },
    price?.retrieved_on && { label: t('analysis-results.cost-breakdown.how.price-date'), value: price.retrieved_on },
    covers.length > 0 && {
      label: t('analysis-results.cost-breakdown.how.covers'),
      value: <ul data-figure="cost-assumptions">{covers.map((line) => <li key={line}>{line}</li>)}</ul>,
    },
    basisNotes.length > 0 && {
      label: t('analysis-results.cost-breakdown.how.read-basis'),
      value: <ul>{basisNotes.map((note) => <li key={note}><span data-figure="cost-basis">{note}</span></li>)}</ul>,
    },
    rowCounts && {
      label: t('analysis-results.cost-breakdown.row-counts'),
      value: <span data-figure="row-counts">{rowCounts}.</span>,
    },
    rates && {
      label: t('analysis-results.cost-breakdown.rate-windows'),
      value: <span data-figure="rate-windows">{rates}.</span>,
    },
    scenario && {
      label: t('analysis-results.cost-breakdown.how.scenario'),
      value: <span data-figure="rate-window-scenario">{scenario}.</span>,
    },
    growth.length > 0 && {
      label: t('analysis-results.cost-breakdown.how.growth'),
      value: <ul data-figure="growth-scenarios">{growth.map((line) => <li key={line}>{line}.</li>)}</ul>,
    },
  ].filter(Boolean);

  const caveat = costCaveatPartsOf(synthesis);
  const cell = (fn) => (item) => (item.total ? '' : fn(item));
  return (
    <Container
      id="cost-breakdown-section"
      header={<Header variant="h2" description={t('analysis-results.cost-breakdown.description')}>{t('analysis-results.cost-breakdown.title')}</Header>}
    >
      <SpaceBetween size="m">
        {hasTotal && (
          <div data-figure="cost-total">
            <Box variant="awsui-key-label">{t('analysis-results.cost-breakdown.total-label')}</Box>
            <Box fontSize="display-l" fontWeight="bold">
              <span data-figure="cost-total-value">{formatMoney(total)}</span>
              {' '}
              <Box variant="span" fontSize="body-m" color="text-body-secondary">{t('analysis-results.cost-breakdown.per-month')}</Box>
            </Box>
          </div>
        )}
        <Box fontSize="body-s" color="text-body-secondary">
          <span data-figure="cost-scope">{costScopeLabelOf(synthesis)}</span>
        </Box>
        <Table
          variant="embedded"
          trackBy="key"
          items={items}
          columnDefinitions={[
            {
              id: 'engine',
              header: t('analysis-results.cost-breakdown.column.engine'),
              cell: (item) => {
                if (item.total) return <Box fontWeight="bold">{t('analysis-results.cost-breakdown.total-row')}</Box>;
                // #358: the cache layer's row is labeled the same way as its "Target engines" badge.
                const isCacheLayer = item.engine === cacheEngine;
                return (
                  <EngineBadge engine={item.engine}>
                    {ENGINE_LABELS[item.engine] || item.engine}
                    {isCacheLayer ? t('cache-layer.engine-badge-suffix', { defaultValue: ' (cache layer)' }) : ''}
                  </EngineBadge>
                );
              },
            },
            { id: 'role', header: t('analysis-results.cost-breakdown.column.role'), cell: cell((item) => item.role) },
            {
              id: 'queries',
              header: t('analysis-results.cost-breakdown.column.queries'),
              cell: cell((item) => (item.sizing?.sized_query_count === undefined ? '' : formatFigure(item.sizing.sized_query_count))),
            },
            {
              id: 'calls',
              header: t('analysis-results.cost-breakdown.column.calls'),
              cell: cell((item) => (item.sizing?.calls_per_second === undefined ? '' : formatFigure(item.sizing.calls_per_second))),
            },
            {
              id: 'sized-for',
              header: t('analysis-results.cost-breakdown.column.sized-for'),
              cell: cell((item) => {
                const text = costSizingBasisText(t, item.sizing);
                return text ? <span data-figure="cost-sizing" data-engine={item.engine}>{text}</span> : '';
              }),
            },
            {
              id: 'per-month',
              header: t('analysis-results.cost-breakdown.column.per-month'),
              cell: (item) => (
                <Box fontWeight={item.total ? 'bold' : undefined}>{formatMoney(item.cost)}</Box>
              ),
            },
          ]}
        />
        <Box fontSize="body-s" color="text-body-secondary">
          <span data-figure="cost-caveat">
            {caveat.before}
            {caveat.href && (
              <Link external href={caveat.href} externalIconAriaLabel={t('analysis-results.cost-breakdown.calculator-new-tab')}>{caveat.label}</Link>
            )}
            {caveat.after}
          </span>
        </Box>
        {how.length > 0 && (
          <ExpandableSection headerText={t('analysis-results.cost-breakdown.how-header')} data-figure="cost-how">
            <KeyValuePairs columns={2} items={how} />
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default CostBreakdown;
