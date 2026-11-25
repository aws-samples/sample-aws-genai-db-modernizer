// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import List from "@cloudscape-design/components/list";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import GateChecklist from "./GateChecklist";
import HelpInfoLink from "./HelpInfoLink";
import { displayEngine } from "../utils/engineNames";
import { stageGateConditions } from "../utils/migrationWaves";
import {
  capitalized, metricLabel, metricValueText, pathTakesStep, scoreBandWord, scoreNotes,
  stageKindText, stageUnits, stepMetric, stepMetricColumns, unitText, wavePhase, waveSteps,
  waveStepsText,
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
 * Above the steps, a table of the wave's steps (#2288): each step's order figures, named
 * by facts.slice_plan.metric_labels (#1699), and its why sentence in its own row. A 0-to-1
 * score with a band (shared by many parts, #2288's decision) reads as the band
 * facts.slice_plan states for it (score_bands, in the words of score_scales) then the
 * value, "Low (0.02)", and one without as its value, as the engineering report prints
 * them; an Info link by the heading opens how they read. StepFigures is the one place that draws them.
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

// Two stored figures in order, a missing one first; a comparison only, never arithmetic.
function compareFigures(x, y) {
  const a = x === null || x === undefined ? null : x;
  const b = y === null || y === undefined ? null : y;
  if (a === b) return 0;
  if (a === null) return -1;
  if (b === null) return 1;
  return a < b ? -1 : 1;
}

// #2288: a score as its band in the facts' words, then its stored value, "Low (0.37)":
// the text the engineering report prints in the same cell.
function ScoreText({ field, band, word, value }) {
  return (
    <span data-figure="step-score" data-metric={field} data-band={band} data-score={value}>
      <span data-figure="score-band-word">{capitalized(word)}</span>{` (${value})`}
    </span>
  );
}

function stepTitle(t, step) {
  return step.engine
    ? t('analysis-results.adoption.step-title', { step: step.step_number, engine: displayEngine(step.engine) })
    : t('analysis-results.adoption.wave-step', { step: step.step_number });
}

// The wave's steps as one table (#2288): the step, each order figure the facts name
// (#1699) and the step's why sentence, as stored. Every column sorts; a figure sorts by
// its stored value. Nothing is counted here.
export function StepFigures({ plan, wave, t }) {
  const [sorting, setSorting] = useState({ id: 'step', descending: false });
  const steps = waveSteps(plan, wave);
  const told = steps.filter(s => s.why);
  if (told.length === 0) return null;
  const measured = steps.filter(s => s.metrics && typeof s.metrics === 'object');
  const rows = steps.filter(s => s.why || (s.metrics && typeof s.metrics === 'object'));
  const columns = stepMetricColumns(plan, measured);
  const notes = scoreNotes(plan, columns);
  const columnDefinitions = [
    {
      id: 'step',
      header: t('analysis-results.adoption.step-column'),
      cell: s => <span data-figure="step-row" data-step={s.step_number}>{stepTitle(t, s)}</span>,
      sortingComparator: (a, b) => compareFigures(a.step_number, b.step_number),
      // Each row is named by its step for a screen reader.
      isRowHeader: true,
      minWidth: 120,
    },
    ...columns.map(f => {
      const label = metricLabel(plan, f);
      return {
        id: f,
        header: <span title={label} data-metric={f}>{capitalized(label)}</span>,
        cell: s => {
          const value = metricValueText(f, stepMetric(s, f));
          const word = stepMetric(s, f) === null ? '' : scoreBandWord(plan, s, f);
          return (
            <Box textAlign="right">
              {word
                ? <ScoreText field={f} band={s.score_bands[f]} word={word} value={value} />
                : <span data-figure="step-figure" data-metric={f}>{value}</span>}
            </Box>
          );
        },
        sortingComparator: (a, b) => compareFigures(stepMetric(a, f), stepMetric(b, f)),
      };
    }),
    {
      id: 'why',
      header: t('analysis-results.adoption.why-column'),
      cell: s => (s.why
        ? <span data-figure="step-why" data-step={s.step_number}>{String(s.why)}</span>
        : '-'),
      minWidth: 320,
    },
  ];
  const column = columnDefinitions.find(c => c.id === sorting.id) || columnDefinitions[0];
  const sign = sorting.descending ? -1 : 1;
  const items = [...rows].sort((a, b) => sign * column.sortingComparator(a, b));
  return (
    <div data-figure="step-why-block">
      <SpaceBetween size="xs">
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Box variant="h4">{t('analysis-results.adoption.why-heading')}</Box>
          {notes.length > 0 && (
            <HelpInfoLink topic="step-scores" t={t} ariaLabel={t('analysis-results.adoption.score-info-label')} />
          )}
        </SpaceBetween>
        <div data-figure="step-metrics">
          <Table
            variant="embedded"
            trackBy="step_number"
            wrapLines
            // Names the table, and the scroll region a narrow screen wraps it in (axe:
            // landmark-unique, #2458).
            ariaLabels={{ tableLabel: t('analysis-results.adoption.step-table-label', { wave }) }}
            items={items}
            columnDefinitions={columnDefinitions}
            sortingColumn={column}
            sortingDescending={sorting.descending}
            onSortingChange={({ detail }) => setSorting({
              id: detail.sortingColumn.id, descending: detail.isDescending,
            })}
          />
        </div>
      </SpaceBetween>
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
