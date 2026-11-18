// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import ProseText from './ProseText';
import EngineBadge from './EngineBadge';
import { CodeBlock } from './StyledBlocks';
import { displayEngine } from '../utils/engineNames';
import { formatCount, formatRate, formatShortId } from '../utils/format';

/**
 * One access pattern of a schema design (#2446), as the access pattern explorer's
 * pattern detail showed it: its engine, operation, destination, the index or GSI
 * that serves it with that GSI's keys, its key condition or query DSL, the cache key
 * design's TTL and data type, and why it is out of scope when it is. It renders in
 * the query detail panel, one per pattern that serves the query. Every value is the
 * design's own, formatted: nothing is counted. Customer and model text renders as
 * text (ProseText, CodeBlock), never as markup.
 */

function keyText(key) {
  const k = Array.isArray(key) ? key[0] : key;
  return k && k.attribute_name ? `${k.attribute_name} (${k.attribute_type})` : null;
}

function gsiItems(ap, t) {
  if (!ap.gsiName || !ap.destDef) return [];
  const gsi = (ap.destDef.gsis || ap.destDef.gsi_indexes || [])
    .find(g => (g.gsi_name || g.index_name) === ap.gsiName);
  if (!gsi) return [];
  const items = [{ label: t('analysis-results.pattern-detail.partition-key'), value: keyText(gsi.partition_key) || '—' }];
  const sk = keyText(gsi.sort_key);
  if (sk) items.push({ label: t('analysis-results.pattern-detail.sort-key'), value: sk });
  items.push({ label: t('analysis-results.pattern-detail.projection'), value: gsi.projection || t('common.labels.all') });
  return items;
}

export function accessPatternTitle(ap, t) {
  const engine = `${displayEngine(ap.engine)}${ap.isCacheLayer ? t('cache-layer.engine-badge-suffix') : ''}`;
  return t('analysis-results.pattern-detail.heading', { id: formatShortId(ap.id), engine });
}

export default function AccessPatternDetail({ ap, t }) {
  const items = [
    {
      label: t('analysis-results.pattern-detail.engine'),
      value: (
        <EngineBadge engine={ap.engine}>
          {displayEngine(ap.engine)}{ap.isCacheLayer ? t('cache-layer.engine-badge-suffix') : ''}
        </EngineBadge>
      ),
    },
    { label: t('analysis-results.pattern-detail.operation'), value: ap.operation || '—' },
    { label: t('analysis-results.pattern-detail.destination-table'), value: ap.destTable || '—' },
  ];
  if (ap.gsiName) items.push({ label: t('analysis-results.pattern-detail.gsi'), value: ap.gsiName });
  if (ap.indexUsed) items.push({ label: t('analysis-results.pattern-detail.index-used'), value: ap.indexUsed });
  if (ap.sourceTables && ap.sourceTables.length > 0) {
    items.push({ label: t('analysis-results.pattern-detail.source-tables'), value: ap.sourceTables.join(', ') });
  }
  if (ap.designRps != null) items.push({ label: t('analysis-results.pattern-detail.design-rps'), value: formatRate(ap.designRps) });
  if (ap.itemSize != null) {
    items.push({ label: t('analysis-results.pattern-detail.item-size'), value: t('analysis-results.pattern-detail.item-size-value', { size: formatCount(ap.itemSize) }) });
  }
  if (ap.avgItems != null) items.push({ label: t('analysis-results.pattern-detail.avg-items-returned'), value: formatRate(ap.avgItems) });
  if (ap.patternGroup) items.push({ label: t('analysis-results.pattern-detail.pattern-group'), value: ap.patternGroup });
  // #429 part 2: the cache layer's key design.
  if (ap.ttlSeconds != null) {
    items.push({ label: t('analysis-results.pattern-detail.ttl'), value: t('analysis-results.explorer.ttl-suffix', { seconds: ap.ttlSeconds }) });
  }
  if (ap.keyDataType) items.push({ label: t('analysis-results.pattern-detail.key-data-type'), value: ap.keyDataType });

  const dsl = ap.opensearchDsl
    ? (typeof ap.opensearchDsl === 'string' ? ap.opensearchDsl : JSON.stringify(ap.opensearchDsl, null, 2))
    : null;
  const gsi = gsiItems(ap, t);
  const title = accessPatternTitle(ap, t);

  return (
    <Container header={<Header variant="h3">{title}</Header>}>
      <SpaceBetween size="m">
        {ap.description && (
          <SpaceBetween size="xxs">
            <Box><ProseText text={ap.description} /></Box>
            {ap.patternDescription && ap.patternDescription !== ap.description && (
              <Box fontSize="body-s" color="text-body-secondary"><ProseText text={ap.patternDescription} /></Box>
            )}
          </SpaceBetween>
        )}
        <KeyValuePairs columns={3} items={items} />
        {(ap.keyCondition || dsl) && (
          <SpaceBetween size="xs">
            <Box variant="awsui-key-label">{t('analysis-results.pattern-detail.target-access-pattern')}</Box>
            <CodeBlock capped label={t('analysis-results.pattern-detail.target-access-pattern-label', { title })}>
              {ap.keyCondition || dsl}
            </CodeBlock>
          </SpaceBetween>
        )}
        {gsi.length > 0 && (
          <SpaceBetween size="xs">
            <Box variant="awsui-key-label">{t('analysis-results.source-table.gsi-prefix')}{ap.gsiName}</Box>
            <KeyValuePairs columns={3} items={gsi} />
          </SpaceBetween>
        )}
        {!ap.inScope && ap.outOfScopeReason && (
          <Box color="text-status-error">
            <strong>{t('analysis-results.pattern-detail.out-of-scope-reason')}:</strong> <ProseText text={ap.outOfScopeReason} />
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}
