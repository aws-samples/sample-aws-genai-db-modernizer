// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useRef } from 'react';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import SplitPanel from '@cloudscape-design/components/split-panel';
import Tabs from '@cloudscape-design/components/tabs';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import CopyToClipboard from '@cloudscape-design/components/copy-to-clipboard';
import Header from '@cloudscape-design/components/header';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { CodeBlock } from './StyledBlocks';
import EngineBadge from './EngineBadge';
import QueryDecisionTrace from './QueryDecisionTrace';
import AccessPatternDetail from './AccessPatternDetail';
import RequestError from './RequestError';
import { displayTables } from '../utils/tableNames';
import { formatRate, formatShortId, NO_FIGURE } from '../utils/format';
import { journeyWorkloadFit } from '../utils/queryRouting';
import { targetDesignGap } from '../utils/accessPatterns';
import { displayEngine } from '../utils/engineNames';
import { WORKLOAD_FIT_LABEL } from '../utils/workloadFitLabels';
import HelpInfoLink from './HelpInfoLink';

/**
 * One query's detail in the page's split panel (#2319): the query table stays
 * visible above it. The title is the short id, the operation and the first
 * table. Every statement and name renders as text (React escapes it, nothing is
 * set as HTML). Figures go through the shared formatter (#2309); an engine reads
 * through its catalog name and colour. The raw journey JSON sits behind a
 * "Developer details" section. "Why this engine" is the model view and is the
 * same component the modal used.
 *
 * #2446: the "Target design" tab holds what the access pattern explorer listed for
 * the query: each schema design access pattern that serves it, then the cache
 * layer's key designs, with their keys and indexes (`patterns`, from the page's
 * designs). A query no design serves says so, and says why (#2480 review): its engine's
 * schema design was skipped (with the reason report.json stores), the design files
 * could not be read, or the design lists no pattern for it.
 *
 * `focusRequest` (#2480 review): a number the page raises when a query opens from
 * the source table dialog; once the query has loaded, focus moves to the panel's body,
 * named by the panel title, so a keyboard or screen reader user lands on the query.
 */

// "calls_per_second" as "Calls per second": the label of a collector field this
// page has no translation for.
export function humanizeKey(key) {
  const words = String(key).split('_').filter(Boolean).join(' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// "Query 0452bed2 · UPDATE wp_woocommerce_api_keys"
export function queryDetailTitle(t, queryId, journey) {
  const source = (journey && journey.source) || {};
  const table = displayTables(source.tables_accessed)[0];
  const what = [source.query_type, table].filter(Boolean).join(' ');
  const base = t('analysis-results.query-detail.title', { id: formatShortId(queryId) });
  return what ? t('analysis-results.query-detail.title-with', { id: formatShortId(queryId), what }) : base;
}

function figureValue(value) {
  if (typeof value === 'number') return formatRate(value);
  if (typeof value === 'boolean') return null;
  if (Array.isArray(value)) return value.join(', ') || NO_FIGURE;
  if (value === null || value === undefined || value === '') return NO_FIGURE;
  return String(value);
}

function FieldList({ fields, emptyText, t }) {
  const entries = fields ? Object.entries(fields) : [];
  if (entries.length === 0) return <Box color="text-body-secondary">{emptyText}</Box>;
  return (
    <KeyValuePairs
      columns={3}
      items={entries.map(([key, value]) => ({
        label: t(`analysis-results.query-detail.field.${key}`, { defaultValue: humanizeKey(key) }),
        value: typeof value === 'boolean' ? t(value ? 'common.labels.yes' : 'common.labels.no') : figureValue(value),
      }))}
    />
  );
}

function EmptyTargetDesign({ engine, schemaDesigns, availability, t }) {
  const gap = targetDesignGap(engine, schemaDesigns, availability);
  if (gap.state === 'skipped') {
    return (
      <SpaceBetween size="xs">
        <Box color="text-body-secondary">{t('analysis-results.query-detail.design-skipped', { engine: displayEngine(gap.engine) })}</Box>
        {gap.reason && <Box color="text-body-secondary">{t('analysis-results.query-detail.design-skipped-reason', { reason: gap.reason })}</Box>}
      </SpaceBetween>
    );
  }
  const key = { unavailable: 'design-unavailable', partial: 'design-partial' }[gap.state] || 'no-design';
  return <Box color="text-body-secondary">{t(`analysis-results.query-detail.${key}`)}</Box>;
}

function TargetDesign({ patterns, engine, schemaDesigns, availability, t }) {
  if (!patterns || patterns.length === 0) {
    return <EmptyTargetDesign engine={engine} schemaDesigns={schemaDesigns} availability={availability} t={t} />;
  }
  return (
    <SpaceBetween size="m">
      {patterns.map(ap => <AccessPatternDetail key={`${ap.engine}:${ap.id}`} ap={ap} t={t} />)}
    </SpaceBetween>
  );
}

export function QueryDetailBody({ queryId, journey, loading, trace, patterns, schemaDesigns, availability, focusRequest, t }) {
  const focusRef = useRef(null);
  const focused = useRef(0);
  useEffect(() => {
    if (loading || !focusRequest || focusRequest === focused.current || !focusRef.current) return;
    focused.current = focusRequest;
    focusRef.current.focus();
  }, [loading, focusRequest]);
  if (loading) {
    return (
      <Box textAlign="center" padding="l">
        <StatusIndicator type="loading">{t('analysis-results.query-journey.loading')}</StatusIndicator>
      </Box>
    );
  }
  if (journey && journey.error) {
    return (
      <Box textAlign="center" padding="l">
        <RequestError inline failure={journey.error} lead={t('analysis-results.query-journey.error')} t={t} />
      </Box>
    );
  }
  const source = (journey && journey.source) || {};
  const assignment = (journey && journey.assignment) || {};
  const fit = journeyWorkloadFit(journey, t);
  const statement = source.query_text;
  return (
    <div ref={focusRef} tabIndex={-1} role="group" aria-label={queryDetailTitle(t, queryId, journey)} data-query-detail-focus="">
      <SpaceBetween size="m">
        <Tabs
          ariaLabel={t('analysis-results.query-detail.tabs-label')}
          // Five tabs scroll on a phone: the scroll buttons need names (axe: button-name).
          i18nStrings={{
            scrollLeftAriaLabel: t('analysis-results.tabs.scroll-left'),
            scrollRightAriaLabel: t('analysis-results.tabs.scroll-right'),
          }}
          tabs={[
            {
              id: 'general',
              label: t('analysis-results.query-detail.tab-general'),
              content: (
                <SpaceBetween size="m">
                  <KeyValuePairs
                    columns={3}
                    items={[
                      {
                        label: t('analysis-results.query-detail.query-id'),
                        value: (
                          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                            <Box fontFamily="monospace" fontSize="body-s">{queryId}</Box>
                            <CopyToClipboard
                              copyButtonAriaLabel={t('common.actions.copy-sql-id')}
                              copyErrorText={t('common.errors.copy-failed')}
                              copySuccessText={t('common.success.copied')}
                              textToCopy={queryId}
                              variant="icon"
                            />
                          </SpaceBetween>
                        ),
                      },
                      { label: t('analysis-results.query-routing.col-operation'), value: source.query_type || NO_FIGURE },
                      { label: t('analysis-results.query-routing.col-tables'), value: displayTables(source.tables_accessed).join(', ') || NO_FIGURE },
                      { label: t('analysis-results.query-detail.calls-per-hour'), value: formatRate(source.frequency_per_hour) },
                      { label: t('analysis-results.query-detail.calls-per-second'), value: formatRate(source.calls_per_second) },
                      {
                        label: t('analysis-results.query-routing.col-engine'),
                        value: assignment.assigned_engine ? <EngineBadge engine={assignment.assigned_engine} /> : NO_FIGURE,
                      },
                      {
                        label: WORKLOAD_FIT_LABEL,
                        // #2512: the definition (WORKLOAD_FIT_DEFINITION) is in the Query
                        // routing help panel; the Info link after the figure opens it.
                        value: fit ? (
                          <SpaceBetween direction="horizontal" size="xs">
                            <Box>{fit}</Box>
                            <HelpInfoLink topic="query-routing" t={t} />
                          </SpaceBetween>
                        ) : NO_FIGURE,
                      },
                      {
                        label: t('analysis-results.query-detail.in-scope'),
                        value: t(assignment.in_scope ? 'common.labels.yes' : 'common.labels.no'),
                      },
                    ]}
                  />
                  {statement && (
                    <SpaceBetween size="xs">
                      <Header variant="h3">{t('analysis-results.query-detail.statement-title')}</Header>
                      <CodeBlock capped label={t('analysis-results.query-detail.statement-label')}>{statement}</CodeBlock>
                    </SpaceBetween>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'design',
              label: t('analysis-results.query-detail.tab-design'),
              content: (
                <TargetDesign
                  patterns={patterns}
                  engine={assignment.assigned_engine}
                  schemaDesigns={schemaDesigns}
                  availability={availability}
                  t={t}
                />
              ),
            },
            {
              id: 'performance',
              label: t('analysis-results.query-detail.tab-performance'),
              content: <FieldList fields={source.performance} emptyText={t('analysis-results.query-detail.no-performance')} t={t} />,
            },
            {
              id: 'characteristics',
              label: t('analysis-results.query-detail.tab-characteristics'),
              content: <FieldList fields={source.characteristics} emptyText={t('analysis-results.query-detail.no-characteristics')} t={t} />,
            },
            {
              id: 'why-this-engine',
              label: t('analysis-results.query-trace.tab'),
              content: <QueryDecisionTrace trace={trace.data} error={trace.error} loading={false} t={t} />,
            },
          ]}
        />
        <ExpandableSection headerText={t('analysis-results.query-detail.developer-details')} variant="footer">
          <CodeBlock capped label={t('analysis-results.query-detail.developer-details-label')}>
            {JSON.stringify(journey, null, 2)}
          </CodeBlock>
        </ExpandableSection>
      </SpaceBetween>
    </div>
  );
}

export default function QueryDetailPanel({ queryId, journey, loading, trace, patterns, schemaDesigns, availability, focusRequest, t }) {
  return (
    <SplitPanel
      header={queryDetailTitle(t, queryId, loading ? null : journey)}
      closeBehavior="hide"
      i18nStrings={{
        closeButtonAriaLabel: t('analysis-results.query-detail.close'),
        openButtonAriaLabel: t('analysis-results.query-detail.open'),
        preferencesTitle: t('analysis-results.query-detail.preferences-title'),
        preferencesPositionLabel: t('analysis-results.query-detail.position-label'),
        preferencesPositionDescription: t('analysis-results.query-detail.position-description'),
        preferencesPositionSide: t('analysis-results.query-detail.position-side'),
        preferencesPositionBottom: t('analysis-results.query-detail.position-bottom'),
        preferencesConfirm: t('analysis-results.query-detail.preferences-confirm'),
        preferencesCancel: t('common.actions.cancel'),
        preferencesCloseAriaLabel: t('analysis-results.query-detail.preferences-close'),
        resizeHandleAriaLabel: t('analysis-results.query-detail.resize'),
      }}
    >
      <QueryDetailBody
        queryId={queryId}
        journey={journey}
        loading={loading}
        trace={trace}
        patterns={patterns}
        schemaDesigns={schemaDesigns}
        availability={availability}
        focusRequest={focusRequest}
        t={t}
      />
    </SplitPanel>
  );
}
