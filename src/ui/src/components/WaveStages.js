// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useId, useRef, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import {
  capitalized, metricLabel, metricValueText, pathTakesStep, stageKindText, stageUnits,
  stepMetric, stepMetricColumns, unitText, wavePhase, waveStages, waveSteps, waveStepsText,
} from "../utils/adoptionPaths";

/**
 * A wave card's steps and stages (#1132): "Steps 3 to 9: 17 stages (…)", collapsed
 * the way a gate's lists are (`gate_parts`, #1028); open, each stage states what it
 * moves, its units and queries and its gate, and its units expand the same way. The
 * stages are facts.slice_plan's (wave_phases and plan_steps[].step_stages, #2156), in
 * run order; nothing is counted here.
 *
 * Open, the list also states each step's why sentence and a table of its order figures,
 * named by facts.slice_plan.metric_labels (#1699); collapsed, the card reads as before.
 *
 * With an earlier path selected, a stage of a step the path does not take is greyed
 * out and marked "Left out of this path"; the full target (the default) greys nothing.
 */

const TOGGLE_STYLE = {
  background: 'none',
  border: 'none',
  padding: 0,
  margin: 0,
  font: 'inherit',
  color: 'inherit',
  cursor: 'pointer',
  textDecoration: 'underline dotted',
};
const HIDE_STYLE = { ...TOGGLE_STYLE, textDecoration: 'none', marginLeft: '0.2em' };
// A stage the selected path leaves out: its text muted with a compliant colour and a
// dashed rule, plus a full-contrast label; never faded (WCAG AA, review of #2249).
const LEFT_OUT_STYLE = { borderLeft: '3px dashed currentColor', paddingLeft: '8px' };
const LEFT_OUT_LABEL_STYLE = { fontWeight: 'bold' };

// "(…)" until clicked; open, the content follows as plain text and a small "×" hides it.
// Focus moves to whichever control is shown, as in the gate's lists.
function Disclosure({ count, showKey, hideKey, figure, t, children }) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  const showRef = useRef(null);
  const hideRef = useRef(null);
  const toggled = useRef(false);
  useEffect(() => {
    if (!toggled.current) return;
    const target = open ? hideRef.current : showRef.current;
    if (target) target.focus();
  }, [open]);
  const toggle = (next) => { toggled.current = true; setOpen(next); };
  if (!open) {
    // The controlled region is always in the page (empty and hidden while closed), so
    // aria-controls names a real element.
    return (
      <>
        <button
          ref={showRef}
          type="button"
          style={TOGGLE_STYLE}
          aria-expanded={false}
          aria-controls={contentId}
          aria-label={t(showKey, { count })}
          data-disclosure={figure}
          onClick={() => toggle(true)}
        >
          (…)
        </button>
        <div id={contentId} hidden />
      </>
    );
  }
  return (
    <>
      <button
        ref={hideRef}
        type="button"
        style={HIDE_STYLE}
        aria-expanded
        aria-controls={contentId}
        aria-label={t(hideKey, { count })}
        data-disclosure-hide={figure}
        onClick={() => toggle(false)}
      >
        ×
      </button>
      <div id={contentId} data-figure={figure}>{children}</div>
    </>
  );
}

function StageItem({ plan, entry, path, t }) {
  const { stage, step } = entry;
  const taken = pathTakesStep(path, step);
  const units = stageUnits(plan, stage);
  return (
    <li
      data-stage={stage.label}
      data-step={step}
      data-left-out={taken ? undefined : 'true'}
      style={taken ? undefined : LEFT_OUT_STYLE}
    >
      {!taken && (
        <div data-left-out-label="" style={LEFT_OUT_LABEL_STYLE}>
          <span data-figure="stage-left-out">{t('analysis-results.adoption.stage-left-out')}</span>
        </div>
      )}
      <Box color={taken ? undefined : 'text-body-secondary'}>
        <span data-figure="stage-line">
          {t('analysis-results.adoption.stage-line', {
            label: stage.label,
            kind: stageKindText(t, stage),
            units: t('analysis-results.adoption.unit-count', { count: stage.unit_count || 0 }),
            queries: t('analysis-results.adoption.query-count', { count: stage.query_count || 0 }),
          })}
        </span>
      {units.length > 0 && (
        <>
          {' '}
          <Disclosure
            count={stage.unit_count || 0}
            showKey="analysis-results.adoption.units-show"
            hideKey="analysis-results.adoption.units-hide"
            figure="stage-units"
            t={t}
          >
            <ul>
              {units.map(unit => (
                <li key={unit.key} data-figure="stage-unit">{unitText(t, unit, plan)}</li>
              ))}
            </ul>
          </Disclosure>
        </>
      )}
      </Box>
      {stage.gate && (
        <Box fontSize="body-s" color="text-body-secondary">
          <span data-figure="stage-gate">{t('analysis-results.adoption.stage-gate', { gate: stage.gate })}</span>
        </Box>
      )}
    </li>
  );
}

// The wave's steps' why sentences, as stored, and a table of each slice step's order
// figures headed by the facts' metric_labels (#1699). Nothing is counted here.
function StepWhy({ plan, wave, t }) {
  const steps = waveSteps(plan, wave);
  const told = steps.filter(s => s.why);
  if (told.length === 0) return null;
  const measured = steps.filter(s => s.metrics && typeof s.metrics === 'object');
  const columns = stepMetricColumns(plan, measured);
  return (
    <div data-figure="step-why-block">
      <Box variant="h4">{t('analysis-results.adoption.why-heading')}</Box>
      <ul>
        {told.map(s => (
          <li key={s.step_number} data-figure="step-why" data-step={s.step_number}>{String(s.why)}</li>
        ))}
      </ul>
      {columns.length > 0 && (
        <table data-figure="step-metrics">
          <thead>
            <tr>
              <th scope="col">{t('analysis-results.adoption.step-column')}</th>
              {columns.map(f => (
                <th key={f} scope="col" title={metricLabel(plan, f)}>{capitalized(metricLabel(plan, f))}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {measured.map(s => (
              <tr key={s.step_number} data-step={s.step_number}>
                <td>{s.step_number}</td>
                {columns.map(f => <td key={f}>{metricValueText(f, stepMetric(s, f))}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function WaveStages({ plan, wave, path, t }) {
  const phase = wavePhase(plan, wave);
  if (!phase) return null;
  const stages = waveStages(plan, wave);
  if (stages.length === 0) return null;
  const count = phase.stage_count || 0;
  return (
    <div data-figure="wave-stages">
      <span data-figure="wave-stages-line">
        {t('analysis-results.adoption.wave-stages', {
          steps: waveStepsText(t, phase),
          stages: t('analysis-results.adoption.stage-count', { count }),
        })}
      </span>
      {' '}
      <Disclosure
        count={count}
        showKey="analysis-results.adoption.stages-show"
        hideKey="analysis-results.adoption.stages-hide"
        figure="wave-stage-list"
        t={t}
      >
        <StepWhy plan={plan} wave={wave} t={t} />
        <ol style={{ listStyle: 'none', paddingLeft: 0 }}>
          {stages.map(entry => (
            <StageItem key={entry.stage.label} plan={plan} entry={entry} path={path} t={t} />
          ))}
        </ol>
      </Disclosure>
    </div>
  );
}
