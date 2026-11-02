// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { waveTags } from '../utils/graphExplorer';

const fmt = (value) => (value == null ? '-' : Number(value).toLocaleString());

/**
 * One step's explanation: the job's own text and figures (the wave plan,
 * facts.engines, the assignment's reason) in short fixed templates. Customer
 * and pipeline text is rendered as text.
 */
export function StepText({ t, step }) {
  if (step.kind === 'wave') {
    return (
      <SpaceBetween size="xxs">
        <Box variant="h4">{t('graph-explorer.walkthroughs.wave-title', { wave: step.wave, title: step.title || '' })}</Box>
        {step.rationale && <Box>{step.rationale}</Box>}
        {step.gate && <Box variant="small">{t('graph-explorer.walkthroughs.gate', { gate: step.gate })}</Box>}
      </SpaceBetween>
    );
  }
  if (step.kind === 'group') {
    const kind = step.group_kind === 'co_dependency' ? 'co-dependency' : 'independent';
    return (
      <SpaceBetween size="xxs">
        <Box variant="h4">
          {t(`graph-explorer.walkthroughs.group-${kind}`, {
            wave: step.wave,
            index: step.group_index,
            total: step.group_total,
            count: step.table_count,
          })}
        </Box>
        <Box>{t('graph-explorer.walkthroughs.group-queries', { count: step.query_count ?? 0, engines: step.engine_labels.join(', ') })}</Box>
        <Box variant="small">{step.tables.join(', ')}</Box>
      </SpaceBetween>
    );
  }
  if (step.kind === 'query' && step.query) {
    const q = step.query;
    return (
      <SpaceBetween size="xxs">
        <Box variant="h4">{t('graph-explorer.walkthroughs.query-title', { label: q.label, value: fmt(q.calls_per_second) })}</Box>
        <Box>{waveTags(q, t).join(' · ')}</Box>
        {step.assignment_reason && <Box>{t('graph-explorer.walkthroughs.why', { reason: step.assignment_reason })}</Box>}
        {q.sql_preview && <Box variant="code">{q.sql_preview}</Box>}
      </SpaceBetween>
    );
  }
  if (step.kind === 'engine') {
    return (
      <SpaceBetween size="xxs">
        <Box variant="h4">
          {t('graph-explorer.walkthroughs.engine-title', {
            engine: step.engine_label,
            role: step.role || '-',
            share: fmt(step.workload_percent),
            cost: fmt(step.monthly_cost_usd),
          })}
        </Box>
        {step.rationale && <Box>{step.rationale}</Box>}
        {step.busiest_tables.length > 0 && (
          <Box variant="small">
            {t('graph-explorer.walkthroughs.busiest-tables', {
              tables: step.busiest_tables
                .map((b) => t('graph-explorer.walkthroughs.table-traffic', { table: b.table, count: b.query_count, value: fmt(b.calls_per_second) }))
                .join('; '),
            })}
          </Box>
        )}
      </SpaceBetween>
    );
  }
  return null;
}

/** A step's short title, for the step bar and the step list. */
export function stepTitle(step, t) {
  if (step.kind === 'wave') return t('graph-explorer.walkthroughs.wave-title', { wave: step.wave, title: step.title || '' });
  if (step.kind === 'group') {
    return t('graph-explorer.walkthroughs.group-short', { wave: step.wave, index: step.group_index, total: step.group_total });
  }
  if (step.kind === 'query') return step.query ? step.query.label : step.start_id;
  if (step.kind === 'engine') return step.engine_label || step.engine;
  return '';
}

/**
 * The Walkthrough tab: the walkthroughs to start (those the API returns),
 * then, while one runs, its step's explanation and the list of its steps to
 * jump to. Previous and Next live in the step bar over the graph.
 */
export function WalkthroughPanel({ t, walkthroughs, active, onStart, onStep, onClose }) {
  if (!walkthroughs || walkthroughs.length === 0) {
    return <Box color="text-body-secondary">{t('graph-explorer.walkthroughs.none')}</Box>;
  }
  const current = active ? walkthroughs.find((w) => w.id === active.id) : null;
  return (
    <div data-testid="graph-explorer-walkthroughs">
      <SpaceBetween size="m">
        <SpaceBetween size="xs">
          <Box fontWeight="bold">{t('graph-explorer.walkthroughs.start')}</Box>
          {walkthroughs.map((w) => (
            <Button key={w.id} variant={current && current.id === w.id ? 'primary' : 'normal'} onClick={() => onStart(w.id)}>
              {t(`graph-explorer.walkthroughs.${w.id}.title`)}
            </Button>
          ))}
        </SpaceBetween>
        {current && (
          <SpaceBetween size="s">
            <StepText t={t} step={current.steps[active.index]} />
            <Box variant="small" color="text-body-secondary">{t('graph-explorer.walkthroughs.explore-freely')}</Box>
            <Box fontWeight="bold">{t('graph-explorer.walkthroughs.steps')}</Box>
            {/* Number and title side by side, so a long title wraps under itself, not under the number. */}
            <ol data-testid="graph-explorer-step-list" style={{ margin: 0, padding: 0, listStyle: 'none' }}>
              {current.steps.map((step, i) => (
                // eslint-disable-next-line react/no-array-index-key
                <li key={i} style={{ display: 'flex', gap: 6, alignItems: 'flex-start', marginBottom: 4, lineHeight: '20px' }}>
                  <span aria-hidden="true" style={{ flex: '0 0 auto', minWidth: 20, textAlign: 'right', lineHeight: '20px' }}>
                    {t('graph-explorer.walkthroughs.step-number', { index: i + 1 })}
                  </span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <Button variant="inline-link" onClick={() => onStep(i)} ariaCurrent={i === active.index ? 'step' : undefined}>
                      {i === active.index ? <strong>{stepTitle(step, t)}</strong> : stepTitle(step, t)}
                    </Button>
                  </span>
                </li>
              ))}
            </ol>
            <Button variant="link" onClick={onClose}>
              {t('graph-explorer.walkthroughs.close')}
            </Button>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </div>
  );
}

/**
 * The floating step bar at the bottom of the graph: Previous, "step N of M ·
 * title", Next and Close. Shown only while a walkthrough runs.
 */
export function StepBar({ t, walkthroughs, active, onStep, onClose, loading }) {
  const current = active && walkthroughs ? walkthroughs.find((w) => w.id === active.id) : null;
  if (!current) return null;
  const index = active.index;
  const last = current.steps.length - 1;
  return (
    <div
      data-testid="graph-explorer-step-bar"
      role="toolbar"
      aria-label={t(`graph-explorer.walkthroughs.${current.id}.title`)}
      style={{
        position: 'absolute',
        // Above the legend card in the corner.
        bottom: 72,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 4,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        maxWidth: '90%',
        padding: '6px 10px',
        borderRadius: 999,
        border: '1px solid #ff9900',
        background: 'rgba(15, 20, 26, 0.94)',
        color: '#e9ebed',
        boxShadow: '0 2px 10px rgba(0,0,0,0.4)',
      }}
    >
      <Button variant="icon" iconName="angle-left" disabled={index === 0 || loading} ariaLabel={t('graph-explorer.walkthroughs.previous')} onClick={() => onStep(index - 1)} />
      <span data-testid="graph-explorer-walkthrough-counter" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontSize: 13 }}>
        {t('graph-explorer.walkthroughs.bar', { index: index + 1, total: current.steps.length, title: stepTitle(current.steps[index], t) })}
      </span>
      <Button variant="icon" iconName="angle-right" disabled={index === last || loading} loading={loading} ariaLabel={t('graph-explorer.walkthroughs.next')} onClick={() => onStep(index + 1)} />
      <Button variant="icon" iconName="close" ariaLabel={t('graph-explorer.walkthroughs.close')} onClick={onClose} />
    </div>
  );
}
