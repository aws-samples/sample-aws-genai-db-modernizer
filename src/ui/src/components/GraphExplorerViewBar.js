// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import FormField from '@cloudscape-design/components/form-field';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Select from '@cloudscape-design/components/select';
import Slider from '@cloudscape-design/components/slider';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Toggle from '@cloudscape-design/components/toggle';
import { COST_NODE_SIZE, EDGE_WIDTH, SLIDER_STEPS, logScale, sliderThreshold } from '../utils/graphExplorer';
import { NODE_TYPE_ICONS, engineIcon } from '../utils/nodeIcons';

function formatNumber(value) {
  if (value == null) return '-';
  return Number(value).toLocaleString(undefined, { maximumSignificantDigits: 3 });
}

// A cost is the report's figure, shown to the cent.
function formatCost(value) {
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** The node icons: tables, the "No table" placeholder and each engine drawn, always with their names. */
export function IconLegend({ t, engines = [], placeholder = false }) {
  const items = [
    { key: 'table', src: NODE_TYPE_ICONS.table.icon, label: t(NODE_TYPE_ICONS.table.labelKey) },
    ...(placeholder ? [{ key: 'placeholder', src: NODE_TYPE_ICONS.placeholder_table.icon, label: t(NODE_TYPE_ICONS.placeholder_table.labelKey) }] : []),
    ...engines.map((engine) => ({ key: engine, ...engineIcon(engine) })),
  ];
  return (
    <div data-testid="graph-explorer-icon-legend" style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
      {items.map((item) => (
        <span key={item.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <img src={item.src} alt="" width={16} height={16} style={{ borderRadius: 2, background: '#2a3644' }} />
          <Box variant="small">{item.label}</Box>
        </span>
      ))}
    </div>
  );
}

/**
 * How a link reads: a bundle is one straight link (click it to fan it out), a
 * fanned-out query is its own curved link. The round 4 legend line, restored.
 */
// A question's result on the graph (the Questions tab): the canvas's `node.result` colour.
export const RESULT_COLOUR = '#00a1c9';

export function LinkLegend({ t, wave = null, result = false }) {
  const stroke = '#8d99a8';
  return (
    <div data-testid="graph-explorer-link-legend" style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        <svg width="28" height="12" aria-hidden="true" focusable="false">
          <line x1="2" y1="6" x2="26" y2="6" stroke={stroke} strokeWidth="5" strokeLinecap="round" />
        </svg>
        <Box variant="small">{t('graph-explorer.legend.bundle')}</Box>
      </span>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        <svg width="28" height="12" aria-hidden="true" focusable="false">
          <path d="M2 10 Q14 0 26 10" fill="none" stroke={stroke} strokeWidth="2" />
          <path d="M2 10 Q14 4 26 10" fill="none" stroke={stroke} strokeWidth="1" />
        </svg>
        <Box variant="small">{t('graph-explorer.legend.query')}</Box>
      </span>
      <span data-testid="graph-explorer-pin-legend" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        <svg width="16" height="16" aria-hidden="true" focusable="false">
          <rect x="2" y="2" width="12" height="12" rx="2" fill="none" stroke={stroke} strokeWidth="1.5" strokeDasharray="1.5 2" />
        </svg>
        <Box variant="small">{t('graph-explorer.legend.pinned')}</Box>
      </span>
      {result && (
        <span data-testid="graph-explorer-result-legend" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <svg width="16" height="16" aria-hidden="true" focusable="false">
            <rect x="2" y="2" width="12" height="12" rx="2" fill="none" stroke={RESULT_COLOUR} strokeWidth="3" />
          </svg>
          <Box variant="small">{t('graph-explorer.legend.result')}</Box>
        </span>
      )}
      {wave != null && (
        <span data-testid="graph-explorer-cross-wave-legend" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <svg width="28" height="12" aria-hidden="true" focusable="false">
            <line x1="2" y1="6" x2="26" y2="6" stroke={stroke} strokeWidth="1" strokeDasharray="2 5" />
          </svg>
          <Box variant="small">{t('graph-explorer.legend.cross-wave')}</Box>
        </span>
      )}
    </div>
  );
}

/** A small legend: line or dot samples sized as the graph sizes them. */
export function Legend({ mode, traffic, cost, t }) {
  if (mode === 'who-writes') {
    return (
      <SpaceBetween direction="horizontal" size="m">
        <LineSample width={3} label={t('graph-explorer.legend.write')} />
        <LineSample width={3} dashed label={t('graph-explorer.legend.replica-read')} />
        <LineSample width={3} faint label={t('graph-explorer.legend.read')} />
        <LineSample width={3} dotted faint label={t('graph-explorer.legend.read-or-replica')} />
      </SpaceBetween>
    );
  }
  if (mode === 'moves-together') {
    return <LineSample width={3} dotted label={t('graph-explorer.legend.unit')} />;
  }
  if (mode === 'cost') {
    if (!cost) return null;
    return (
      <SpaceBetween direction="horizontal" size="m">
        {cost.samples.map((value) => (
          <span key={value} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span
              style={{
                display: 'inline-block',
                width: logScale(value, cost, COST_NODE_SIZE) / 2,
                height: logScale(value, cost, COST_NODE_SIZE) / 2,
                borderRadius: '50%',
                background: '#7d8998',
              }}
            />
            {t('graph-explorer.legend.cost', { value: formatCost(value) })}
          </span>
        ))}
      </SpaceBetween>
    );
  }
  if (!traffic) return null;
  // Thin, medium and thick: one query's calls per second.
  const names = ['thin', 'medium', 'thick'];
  return (
    <SpaceBetween direction="horizontal" size="m">
      {traffic.samples.map((value, i) => (
        <LineSample
          key={names[i] || value}
          width={logScale(value, traffic, EDGE_WIDTH)}
          label={t('graph-explorer.legend.traffic', { value: formatNumber(value) })}
        />
      ))}
    </SpaceBetween>
  );
}

function LineSample({ width, dashed = false, dotted = false, faint = false, label }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span
        style={{
          display: 'inline-block',
          width: 36,
          borderTop: `${width}px ${dotted ? 'dotted' : dashed ? 'dashed' : 'solid'} #8d99a8`,
          opacity: faint ? 0.45 : 1,
        }}
      />
      <Box variant="small">{label}</Box>
    </span>
  );
}

/**
 * The explorer's view switcher, its one filter row (engine, minimum traffic,
 * wave: what moves in the wave stays in full, the rest fades) and the legend. The hidden-query count is the API's (below-traffic),
 * never counted here.
 */
export default function GraphExplorerViewBar({
  t,
  views,
  mode,
  onMode,
  engines,
  engineFilter,
  onEngine,
  waves,
  wave,
  onWave,
  traffic,
  cost,
  slider,
  onSlider,
  threshold,
  below,
  hideWave = false,
  hideViews = false,
  hideLegend = false,
  showOtherWaves = true,
  onShowOtherWaves = null,
}) {
  const allEngines = { value: '', label: t('graph-explorer.filters.all-engines') };
  const engineOptions = [allEngines, ...(engines || []).map((e) => ({ value: e.id, label: e.label }))];
  const allWaves = { value: '', label: t('graph-explorer.filters.all-waves') };
  const waveOptions = (waves || []).map((w) => ({
    value: String(w.wave),
    label: t('graph-explorer.filters.wave-option', { wave: w.wave, engines: w.engine_labels.join(', ') }),
  }));
  return (
    <SpaceBetween size="s">
      {!hideViews && (
      <SegmentedControl
        label={t('graph-explorer.views.label')}
        selectedId={mode}
        onChange={({ detail }) => onMode(detail.selectedId)}
        options={views.map((v) => ({ id: v, text: t(`graph-explorer.views.${v}`) }))}
      />
      )}
      <SpaceBetween direction="horizontal" size="l">
        <FormField label={t('graph-explorer.filters.engine')}>
          <div style={{ minWidth: 200 }}>
            <Select
              selectedOption={engineOptions.find((o) => o.value === (engineFilter || '')) || allEngines}
              onChange={({ detail }) => onEngine(detail.selectedOption.value || null)}
              options={engineOptions}
            />
          </div>
        </FormField>
        <FormField
          label={t('graph-explorer.filters.min-traffic')}
          description={
            threshold > 0
              ? t('graph-explorer.filters.min-traffic-value', { value: formatNumber(threshold) })
              : t('graph-explorer.filters.min-traffic-off')
          }
          constraintText={
            below && below.min_calls_per_second > 0
              ? t('graph-explorer.filters.hidden', { count: below.hidden_count, value: formatNumber(below.min_calls_per_second) })
              : undefined
          }
        >
          <div style={{ minWidth: 260 }}>
            <Slider
              value={slider}
              min={0}
              max={SLIDER_STEPS}
              step={1}
              disabled={!traffic}
              ariaLabel={t('graph-explorer.filters.min-traffic')}
              // Each position shows the threshold it stands for (the ends: 0 and the busiest query).
              valueFormatter={(v) => formatNumber(sliderThreshold(v, traffic))}
              onChange={({ detail }) => onSlider(detail.value)}
            />
          </div>
        </FormField>
        {waveOptions.length > 0 && !hideWave && (
          <FormField label={t('graph-explorer.filters.wave')} description={wave != null ? t('graph-explorer.filters.wave-note') : undefined}>
            <div style={{ minWidth: 240 }}>
              <Select
                selectedOption={waveOptions.find((o) => o.value === String(wave)) || allWaves}
                onChange={({ detail }) => onWave(detail.selectedOption.value ? Number(detail.selectedOption.value) : null)}
                options={[allWaves, ...waveOptions]}
              />
            </div>
          </FormField>
        )}
        {waveOptions.length > 0 && onShowOtherWaves && (
          <FormField label={t('graph-explorer.filters.other-waves')} description={t('graph-explorer.filters.other-waves-note')}>
            <Toggle checked={showOtherWaves} onChange={({ detail }) => onShowOtherWaves(detail.checked)}>
              {t('graph-explorer.filters.other-waves-toggle')}
            </Toggle>
          </FormField>
        )}
      </SpaceBetween>
      {!hideLegend && <Legend mode={mode} traffic={traffic} cost={cost} t={t} />}
    </SpaceBetween>
  );
}
