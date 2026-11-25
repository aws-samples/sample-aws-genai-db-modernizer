// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import List from "@cloudscape-design/components/list";
import SpaceBetween from "@cloudscape-design/components/space-between";
import GateChecklist from "./GateChecklist";
import { displayEngine } from "../utils/engineNames";
import { stageGateConditions } from "../utils/migrationWaves";
import {
  capitalized, metricLabel, metricValueText, pathTakesStep, stageKindText, stageUnits,
  stepMetric, stepMetricColumns, unitText, wavePhase, waveSteps, waveStepsText,
} from "../utils/adoptionPaths";

/**
 * A wave's steps and stages (#1132, #2286): the line "Steps 3 to 7: 11 stages", then
 * one collapsed section per step ("Step 3: DynamoDB", its stage and query counts in
 * the header) and, inside it, one collapsed section per stage ("3a: Reads on a CDC
 * copy", its unit and query counts in the header). Open, a stage states what it moves,
 * its units, queries and gate as key-value pairs (the gate a checklist of its conditions,
 * GateChecklist, #2287), then its units as a list. The steps
 * and stages are facts.slice_plan's (wave_phases and plan_steps[].step_stages, #2156),
 * in run order; nothing is counted here.
 *
 * Above the steps, each step's why sentence and a table of the steps' order figures,
 * named by facts.slice_plan.metric_labels (#1699). StepFigures is the one place that
 * draws them, so a later change to that table (#2288) touches only it.
 *
 * With an earlier path selected, a step the path does not take is marked "Left out of
 * this path" once, in its header; it and each of its stages carry a dashed rule. The full
 * target (the default) marks nothing.
 */

// A step or stage the selected path leaves out: a dashed rule and a full-contrast badge,
// never faded (WCAG AA, review of #2249).
const LEFT_OUT_STYLE = { borderLeft: '3px dashed currentColor', paddingLeft: '8px' };

function LeftOutBadge({ figure, t }) {
  return (
    <span data-left-out-label="" data-figure={figure}>
      <Badge color="grey">{t('analysis-results.adoption.stage-left-out')}</Badge>
    </span>
  );
}

function StageSection({ plan, stage, step, taken, t }) {
  const units = stageUnits(plan, stage);
  const kind = stageKindText(t, stage);
  const unitCount = t('analysis-results.adoption.unit-count', { count: stage.unit_count || 0 });
  const queryCount = t('analysis-results.adoption.query-count', { count: stage.query_count || 0 });
  const items = [
    { label: t('analysis-results.adoption.stage-field.kind'), value: <span data-figure="stage-kind">{kind || '-'}</span> },
    { label: t('analysis-results.adoption.stage-field.units'), value: <span data-figure="stage-unit-count">{unitCount}</span> },
    { label: t('analysis-results.adoption.stage-field.queries'), value: <span data-figure="stage-query-count">{queryCount}</span> },
  ];
  const conditions = stageGateConditions(stage);
  if (conditions.length > 0) {
    // #2287: the gate as a checklist of the facts' conditions, each table list behind
    // "Show N tables".
    items.push({
      label: t('analysis-results.adoption.stage-field.gate'),
      value: (
        <GateChecklist
          conditions={conditions}
          t={t}
          label={t('analysis-results.adoption.stage-gate-label', { label: stage.label })}
          figure="stage-gate"
        />
      ),
    });
  }
  return (
    <div
      data-stage={stage.label}
      data-step={step}
      data-left-out={taken ? undefined : 'true'}
      style={taken ? undefined : LEFT_OUT_STYLE}
    >
      <ExpandableSection
        variant="default"
        headingTagOverride="h6"
        // A kind the page does not know reads "-" in the fields, and the header is the label alone.
        headerText={kind && kind !== '-'
          ? t('analysis-results.adoption.stage-title', { label: stage.label, kind: capitalized(kind) })
          : String(stage.label)}
        headerDescription={t('analysis-results.adoption.stage-summary', { units: unitCount, queries: queryCount })}
      >
        <SpaceBetween size="s">
          <KeyValuePairs columns={2} items={items} />
          {units.length > 0 && (
            <div data-figure="stage-units">
              <List
                ariaLabel={t('analysis-results.adoption.stage-unit-list', { label: stage.label })}
                items={units}
                disablePaddings
                renderItem={unit => ({
                  id: unit.key,
                  content: <span data-figure="stage-unit">{unitText(t, unit, plan)}</span>,
                })}
              />
            </div>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
}

function StepSection({ plan, step, path, t }) {
  const n = step.step_number;
  const taken = pathTakesStep(path, n);
  const stages = (step.step_stages || []).filter(s => s && typeof s === 'object');
  const title = step.engine
    ? t('analysis-results.adoption.step-title', { step: n, engine: displayEngine(step.engine) })
    : t('analysis-results.adoption.wave-step', { step: n });
  return (
    <div
      data-figure="plan-step"
      data-plan-step={n}
      data-left-out={taken ? undefined : 'true'}
      style={taken ? undefined : LEFT_OUT_STYLE}
    >
      <ExpandableSection
        variant="default"
        headingTagOverride="h5"
        headerText={title}
        headerDescription={t('analysis-results.adoption.step-summary', {
          stages: t('analysis-results.adoption.stage-count', { count: step.stage_count || 0 }),
          queries: t('analysis-results.adoption.query-count', { count: step.query_count || 0 }),
        })}
        headerSecondaryContent={taken ? undefined : <LeftOutBadge figure="step-left-out" t={t} />}
      >
        <SpaceBetween size="xs">
          {stages.map(stage => (
            <StageSection key={stage.label} plan={plan} stage={stage} step={n} taken={taken} t={t} />
          ))}
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
}

// The wave's steps' why sentences, as stored, and a table of each slice step's order
// figures headed by the facts' metric_labels (#1699). Nothing is counted here.
export function StepFigures({ plan, wave, t }) {
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
  const steps = waveSteps(plan, wave).filter(s => (s.step_stages || []).length > 0);
  if (steps.length === 0) return null;
  const count = phase.stage_count || 0;
  return (
    <div data-figure="wave-stages">
      <SpaceBetween size="s">
        <Box variant="h4">
          <span data-figure="wave-stages-line">
            {t('analysis-results.adoption.wave-stages', {
              steps: waveStepsText(t, phase),
              stages: t('analysis-results.adoption.stage-count', { count }),
            })}
          </span>
        </Box>
        <StepFigures plan={plan} wave={wave} t={t} />
        <div data-figure="wave-step-list">
          <SpaceBetween size="xs">
            {steps.map(step => (
              <StepSection key={step.step_number} plan={plan} step={step} path={path} t={t} />
            ))}
          </SpaceBetween>
        </div>
      </SpaceBetween>
    </div>
  );
}
