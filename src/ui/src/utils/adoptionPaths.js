// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #1132 (ADR-032): the results page's adoption paths, the waves' stages and the
// one-step option, read from report.json's facts.adoption and facts.slice_plan and
// formatted. Nothing here counts, totals or ranks: every figure is a stored field, the
// staged full target stays the default, and choosing a path only changes what the
// page shows (it never writes or changes the assignment).
//
// The words are the engineering report's (src/report/adoption.py), copied into the
// locale and pinned by tests/unit/report/test_adoption_paths_js_sync.py. Three of its
// rules hold here too:
//
// - no path's moving-table count (paths[].tables_migrate) is printed;
// - an earlier path's cost is printed only when each path was priced for itself
//   (cost_basis "per_path_sizing"); the full target's cost always is;
// - no difference from the relational move only is printed when
//   relational_monthly_cost_usd is None (no relational step).

import { displayEngine } from './engineNames';

export const PER_PATH_SIZING = 'per_path_sizing';
export const FULL_TARGET = 'full_target';
export const RELATIONAL_ONLY = 'relational_only';

// _fmt_usd from src/report/formatting.py: "$1,178.33".
export function formatUsd(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// fmt_num for a count: thousands separators.
export function formatCount(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-US') : '0';
}

// facts.adoption when it offers a path, else null: a report written before it (or
// rebuilt without the assignment) shows the page exactly as before (#1132).
export function adoptionOf(synthesis) {
  const block = synthesis && synthesis.facts && synthesis.facts.adoption;
  if (!block || typeof block !== 'object' || !Array.isArray(block.paths)) return null;
  const paths = block.paths.filter(p => p && typeof p === 'object' && p.path_id);
  return paths.length > 0 ? block : null;
}

export function slicePlanOf(synthesis) {
  const block = synthesis && synthesis.facts && synthesis.facts.slice_plan;
  return block && typeof block === 'object' ? block : null;
}

function offeredPaths(adoption) {
  return (adoption.paths || []).filter(p => p && typeof p === 'object' && p.path_id);
}

// The path the page opens on: the facts' default (the staged full target).
export function defaultPathId(adoption) {
  return (adoption && adoption.default_path_id) || FULL_TARGET;
}

// The paths the selector shows. Capped, they are the ones summary_path_ids names
// (#2172), so the page chooses none of them; "Show all" lists every offered path.
export function selectorPaths(adoption, showAll) {
  const paths = offeredPaths(adoption);
  if (showAll) return paths;
  const shown = new Set((adoption.summary_path_ids || []).map(String));
  return paths.filter(p => shown.has(String(p.path_id)));
}

// Whether the capped selector leaves a path out, so "Show all" is offered.
export function selectorIsCapped(adoption) {
  const shown = new Set((adoption.summary_path_ids || []).map(String));
  return offeredPaths(adoption).some(p => !shown.has(String(p.path_id)));
}

export function findPath(adoption, pathId) {
  const paths = offeredPaths(adoption);
  return paths.find(p => p.path_id === pathId)
    || paths.find(p => p.path_id === defaultPathId(adoption))
    || null;
}

// "Relational move only", "Through step 5", "Full target, the default": never the raw
// path_id. The step is the path's own stops_after_step.
export function pathLabel(t, path, defaultId) {
  const id = path.path_id;
  let label;
  if (id === RELATIONAL_ONLY) label = t('analysis-results.adoption.path.relational_only');
  else if (id === FULL_TARGET) label = t('analysis-results.adoption.path.full_target');
  else label = t('analysis-results.adoption.path.through-step', { step: path.stops_after_step });
  return id === defaultId ? t('analysis-results.adoption.path.default', { label }) : label;
}

// The relational step's engine, or '' when the plan has none.
export function relationalEngine(plan) {
  const step = ((plan && plan.plan_steps) || []).find(s => s && s.kind === 'relational_step');
  return (step && step.engine) || '';
}

export function planStep(plan, stepNumber) {
  return ((plan && plan.plan_steps) || []).find(s => s && s.step_number === stepNumber) || null;
}

// Every offered path's cost is printed only when each was priced for itself; the full
// target's (the headline projected cost) always is (ADR-033 Amendment 2).
export function costStated(adoption, path) {
  return adoption.cost_basis === PER_PATH_SIZING || path.path_id === FULL_TARGET;
}

// A difference from the relational move only is printed only when the relational move
// has a cost (#2214), and never for the relational move only itself.
export function differenceStated(adoption, path) {
  if (adoption.relational_monthly_cost_usd === null || adoption.relational_monthly_cost_usd === undefined) return false;
  if (path.path_id === RELATIONAL_ONLY || !costStated(adoption, path)) return false;
  const delta = path.cost_delta_vs_relational_usd;
  return typeof delta === 'number' && Number.isFinite(delta);
}

// "$15.33 less than the relational move only", "the same as the relational move only":
// the stored difference, its sign choosing the word.
export function differenceText(t, path) {
  const delta = path.cost_delta_vs_relational_usd;
  if (delta === 0) return t('analysis-results.adoption.difference-same');
  const key = delta < 0 ? 'analysis-results.adoption.difference-less' : 'analysis-results.adoption.difference-more';
  return t(key, { amount: formatUsd(Math.abs(delta)) });
}

// The note on what every path's cost rests on: the facts' own sentence when each path
// was priced for itself, else the report's "only the full target's cost" line.
export function costNote(t, adoption) {
  if (adoption.cost_basis === PER_PATH_SIZING) return adoption.cost_basis_note || '';
  return t('analysis-results.adoption.full-target-cost-only');
}

export function introText(t, plan) {
  const engine = relationalEngine(plan);
  return engine
    ? t('analysis-results.adoption.intro-relational', { engine: displayEngine(engine) })
    : t('analysis-results.adoption.intro');
}

// "Steps 1 to 5, the last on DynamoDB in wave 3; 10 stages, 10 gates", as the
// engineering report's path block says it.
export function stepsText(t, path, plan) {
  const stop = path.stops_after_step;
  const span = path.step_count === 1
    ? t('analysis-results.adoption.steps-one')
    : t('analysis-results.adoption.steps-span', { stop });
  const last = planStep(plan, stop);
  let where = '';
  if (last && last.engine) {
    where = last.phase_wave
      ? t('analysis-results.adoption.steps-last-in-wave', { engine: displayEngine(last.engine), wave: last.phase_wave })
      : t('analysis-results.adoption.steps-last', { engine: displayEngine(last.engine) });
  }
  return t('analysis-results.adoption.steps', {
    span,
    where,
    stages: t('analysis-results.adoption.stage-count', { count: path.stage_count || 0 }),
    gates: t('analysis-results.adoption.gate-count', { count: path.gate_count || 0 }),
  });
}

// The not-offered lines (#1130), in the report's words.
export function notOfferedLines(t, adoption) {
  const defaultId = defaultPathId(adoption);
  return (adoption.not_offered || []).filter(e => e && typeof e === 'object').map(entry => {
    const label = pathLabel(t, { path_id: entry.path_id, stops_after_step: stepOfPathId(entry.path_id) }, defaultId);
    let text;
    if (entry.reason === 'no_relational_engine') {
      text = t('analysis-results.adoption.not-offered-no-relational', { label });
    } else {
      const engines = (entry.serving_engines || []).map(displayEngine);
      text = t('analysis-results.adoption.not-offered-gates', {
        label,
        count: entry.failing_query_count || 0,
        serving: engines.length
          ? t(engines.length === 1
            ? 'analysis-results.adoption.not-offered-serving-one'
            : 'analysis-results.adoption.not-offered-serving-many', { engines: engines.join(', ') })
          : '',
      });
    }
    if (entry.offered_instead) {
      const instead = pathLabel(t, { path_id: entry.offered_instead, stops_after_step: stepOfPathId(entry.offered_instead) }, '');
      text = `${text} ${t('analysis-results.adoption.offered-instead', { label: instead })}`;
    }
    return { pathId: entry.path_id, text };
  });
}

// The step a through_step_<k> id names (only for a path not offered, which has no
// stops_after_step of its own).
function stepOfPathId(pathId) {
  const match = /^through_step_(\d+)$/.exec(String(pathId || ''));
  return match ? match[1] : '';
}

// ---- The waves' stages (facts.slice_plan, #2156) ----

// The wave's phase: its steps, in order, from wave_phases.
export function wavePhase(plan, wave) {
  return ((plan && plan.wave_phases) || []).find(p => p && p.phase_wave === wave) || null;
}

// The wave's stages, each with the step it belongs to, in run order: the phase's steps
// and each step's own stages.
export function waveStages(plan, wave) {
  const phase = wavePhase(plan, wave);
  if (!phase) return [];
  const out = [];
  (phase.step_numbers || []).forEach(n => {
    const step = planStep(plan, n);
    ((step && step.step_stages) || []).forEach(stage => {
      if (stage && typeof stage === 'object') out.push({ step: n, stage });
    });
  });
  return out;
}

// "Step 1", "Steps 3 to 9": the phase's first and last step numbers.
export function waveStepsText(t, phase) {
  const numbers = (phase.step_numbers || []).filter(n => typeof n === 'number');
  if (numbers.length === 0) return t('analysis-results.adoption.stages-label');
  const first = numbers[0];
  const [last] = numbers.slice(-1);
  return first === last
    ? t('analysis-results.adoption.wave-step', { step: first })
    : t('analysis-results.adoption.wave-steps', { first, last });
}

// Whether the selected path takes a step: it takes steps 1 to stops_after_step.
export function pathTakesStep(path, stepNumber) {
  return !path || stepNumber <= path.stops_after_step;
}

// Whether the selected path leaves the whole wave out (none of its steps taken).
export function pathLeavesWaveOut(path, phase) {
  if (!path || !phase) return false;
  const numbers = phase.step_numbers || [];
  return numbers.length > 0 && numbers.every(n => !pathTakesStep(path, n));
}

// Whether the selected path stops inside the wave: some of its steps are taken, some not.
export function pathStopsInWave(path, phase) {
  if (!path || !phase) return false;
  const numbers = phase.step_numbers || [];
  return numbers.some(n => pathTakesStep(path, n)) && numbers.some(n => !pathTakesStep(path, n));
}

// What a stage moves, in the report's words, with the risk that holds it. A kind this
// page does not know reads "-", as in the report, never the raw locale key.
export function stageKindText(t, stage) {
  const kind = stage.kind ? t(`analysis-results.adoption.stage-kind.${stage.kind}`, { defaultValue: '-' }) : '';
  const held = (stage.holding_risk_ids || []).map(String);
  if (held.length === 0) return kind;
  return kind ? `${kind} (${held.join(', ')})` : held.join(', ');
}

// The units of a stage the page lists: by key from the plan's move_units, the
// whole-database stage and a unit with no table either way left out, as the report
// leaves them out (the stage's own counts state them).
export function stageUnits(plan, stage) {
  if (stage.kind === 'whole_database') return [];
  const byKey = {};
  ((plan && plan.move_units) || []).forEach(u => { if (u && u.key) byKey[u.key] = u; });
  return (stage.unit_keys || [])
    .map(k => byKey[k])
    .filter(u => u && (
      (u.owned_tables || []).length > 0
      || (u.replicated_in || []).length > 0
      || (u.reverse_replicas || []).length > 0
    ));
}

// One unit as a line: the pilot named, its queries, the tables it owns, the copies it
// reads and where a CDC copy of its tables goes back to. Never its key (an engine id).
export function unitText(t, unit) {
  const parts = [t('analysis-results.adoption.unit-queries', { count: unit.query_count || 0 })];
  const owned = (unit.owned_tables || []).map(String);
  if (owned.length) parts.push(t('analysis-results.adoption.unit-owns', { tables: owned.join(', ') }));
  const reads = (unit.replicated_in || []).filter(r => r && r.table).map(r => String(r.table));
  if (reads.length) parts.push(t('analysis-results.adoption.unit-reads', { tables: reads.join(', ') }));
  const back = (unit.reverse_replicas || []).filter(r => r && r.engine).map(r => t(
    'analysis-results.adoption.unit-back-engine',
    { engine: displayEngine(r.engine), count: r.read_count || 0 },
  ));
  if (back.length) parts.push(t('analysis-results.adoption.unit-back', { engines: back.join(', ') }));
  const text = parts.join('; ');
  return unit.pilot ? t('analysis-results.adoption.unit-pilot', { text }) : text;
}

// ---- The one-step option (#473) ----

// The section of the results page the one-step option links to: the purpose-built
// designs' explorer, else the table design, else none (the option then points at the
// executive summary's target architecture).
export function schemaSectionIdOf(hasExplorer, hasTableDesign) {
  if (hasExplorer) return 'access-patterns-section';
  if (hasTableDesign) return 'table-design-section';
  return null;
}

export function oneStepOf(adoption) {
  const one = adoption && adoption.one_step;
  return one && typeof one === 'object' ? one : null;
}

// A one-step rule in the report's words; one this page does not know reads as its
// words ("No new thing here"), never the raw locale key or the snake_case id.
export function oneStepRuleText(t, rule) {
  const words = String(rule || '').replace(/_/g, ' ').trim();
  const fallback = words ? words.charAt(0).toUpperCase() + words.slice(1) : '-';
  return t(`analysis-results.adoption.one-step.rule.${rule}`, { defaultValue: fallback });
}

// What the one-step option points at, decided as the engineering report decides it
// (src/report/roadmap.py one_step_pointer): the target schemas when a schema design
// completed, else the target architecture in the Target engines tile. Read from
// report.json's schema_designs, never from which sections the page happens to show.
export function oneStepPointerOf(synthesis) {
  const designs = (synthesis && synthesis.schema_designs) || {};
  const completed = Object.values(designs).some(d => d && typeof d === 'object' && d.status === 'completed');
  return completed ? 'schemas' : 'architecture';
}

export const TARGET_ENGINES_ID = 'target-engines';

export function oneStepChecks(one) {
  return (one.one_step_checks || []).filter(c => c && typeof c === 'object');
}
