// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1132: the results page's adoption paths above the roadmap's wave cards, the waves'
 * stages and the one-step option. What the page must do, in the engineering report's
 * words (src/report/adoption.py):
 *
 * - open on the staged full target, so the default view is today's (nothing greyed);
 * - show one chip per path the capped view names (summary_path_ids, #2172), and every
 *   offered path only on "Show all"; nothing ranks the paths;
 * - show the selected path's four tiles from facts.adoption.paths[] as stored;
 * - print an earlier path's cost only when cost_basis is per_path_sizing, the full
 *   target's always; no difference from the relational move only when
 *   relational_monthly_cost_usd is None;
 * - never print a path's moving-table count (paths[].tables_migrate);
 * - grey out the waves and stages a path leaves out; choosing a path changes only what
 *   is shown and never writes anything;
 * - leave a report without facts.adoption exactly as it was.
 *
 * Cloudscape components are plain DOM stand-ins (as in MigrationWavePlan.test.js).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from '../../utils/__tests__/fixtures/waveHeadlines.json';
import MigrationWavePlan from '../MigrationWavePlan';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description, counter }) => (
    <header><h2>{children}{counter ? ` ${counter}` : ''}</h2><p className="mock-description">{description}</p></header>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/column-layout', () => ({
  __esModule: true,
  default: ({ children }) => <div className="mock-columns">{children}</div>,
}));
jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ children }) => <span className="mock-badge">{children}</span>,
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children }) => <details><summary>{headerText}</summary>{children}</details>,
}));
jest.mock('@cloudscape-design/components/toggle-button', () => ({
  __esModule: true,
  default: ({ children, pressed, onChange }) => (
    <button type="button" aria-pressed={pressed} onClick={() => onChange({ detail: { pressed: !pressed } })}>{children}</button>
  ),
}));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick }) => <button type="button" className="mock-link-button" onClick={onClick}>{children}</button>,
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, href, onFollow }) => <a href={href} onClick={onFollow}>{children}</a>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let t;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(synthesis, props = {}) {
  act(() => {
    root.render(<MigrationWavePlan synthesis={synthesis} t={t} {...props} />);
  });
}

function click(el) {
  act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
}

// A figure no real report holds, in every place a path's moving-table count lives.
const SENTINEL = 9137;

function path(id, stop, extra) {
  return {
    path_id: id,
    engines: ['aurora_mysql', 'elasticache', 'dynamodb'],
    stops_after_step: stop,
    step_count: stop,
    stage_count: stop,
    gate_count: stop,
    queries_moved: 10 * stop,
    queries_by_engine: {},
    tables_migrate: SENTINEL,
    monthly_cost_usd: 100 + stop,
    engine_costs: [],
    cost_delta_vs_relational_usd: stop - 2,
    cost_delta_vs_current_usd: null,
    cost_is_upper_bound: false,
    critical_high_risks: stop > 4 ? 2 : 1,
    replicated_tables: [],
    replicated_table_count: 0,
    temporary_stream_count: 0,
    first_gate: 'Full load complete. The cutover is your responsibility.',
    ...extra,
  };
}

function stage(label, kind, unitKeys, extra) {
  return {
    label,
    kind,
    unit_keys: unitKeys,
    unit_count: unitKeys.length,
    query_count: 3,
    owned_table_count: 1,
    call_share_percent: 1,
    holding_risk_ids: [],
    gate: `Gate of stage ${label}.`,
    ...extra,
  };
}

// Step 1 moves the whole database to Aurora MySQL (wave 1), step 2 fronts reads with
// ElastiCache (wave 2), steps 3 to 8 move to DynamoDB (wave 3). Seven paths: the
// relational move only (after step 2), through steps 3 to 7, and the full target.
function synthesis({ costBasis = 'per_path_sizing', relational = true } = {}) {
  const steps = [
    { step_number: 1, kind: 'relational_step', engine: 'aurora_mysql', phase_wave: 1, step_stages: [stage('1', 'whole_database', ['aurora_mysql:whole-database'])] },
    { step_number: 2, kind: 'cache_step', engine: 'elasticache', phase_wave: 2, step_stages: [stage('2a', 'cache_reads', ['elasticache:cache'])] },
    ...[3, 4, 5, 6, 7, 8].map(n => ({
      step_number: n,
      kind: 'slice',
      engine: 'dynamodb',
      phase_wave: 3,
      step_stages: [stage(`${n}a`, n === 3 ? 'replica_reads' : 'self_contained', n === 3 ? ['dynamodb:app.carts', 'dynamodb:app.empty'] : [`dynamodb:app.t${n}`])],
    })),
  ];
  const units = [
    { key: 'aurora_mysql:whole-database', engine: 'aurora_mysql', query_count: 30, owned_tables: ['app.carts'] },
    { key: 'elasticache:cache', engine: 'elasticache', query_count: 2, owned_tables: [] },
    {
      key: 'dynamodb:app.carts', engine: 'dynamodb', query_count: 4, pilot: true,
      owned_tables: ['app.carts'], replicated_in: [{ table: 'app.users', source_of_truth: 'aurora_mysql' }],
      reverse_replicas: [{ engine: 'aurora_mysql', read_count: 2 }],
    },
    { key: 'dynamodb:app.empty', engine: 'dynamodb', query_count: 1, owned_tables: [] },
    ...[4, 5, 6, 7, 8].map(n => ({ key: `dynamodb:app.t${n}`, engine: 'dynamodb', query_count: 2, owned_tables: [`app.t${n}`] })),
  ];
  const ids = [['relational_only', 2], ['through_step_3', 3], ['through_step_4', 4], ['through_step_5', 5],
    ['through_step_6', 6], ['through_step_7', 7], ['full_target', 8]];
  const paths = ids.map(([id, stop]) => path(id, stop, id === 'relational_only'
    ? { engines: ['aurora_mysql', 'elasticache'], cost_delta_vs_relational_usd: 0 } : {}));
  paths[1].cost_delta_vs_relational_usd = -15.33;
  return {
    migration_waves: [
      { wave: 1, title: 'Move to Aurora MySQL', engines: ['aurora_mysql'], query_count: 30, workload_share_percent: 100, table_count: 9 },
      { wave: 2, title: 'Cache hot reads', engines: ['elasticache'], query_count: 2, workload_share_percent: 5, table_count: 0 },
      { wave: 3, title: 'Move to DynamoDB', engines: ['dynamodb'], query_count: 15, workload_share_percent: 50, table_count: 6 },
    ],
    facts: {
      migration_wave_count: 3,
      waves: [],
      tables_migrate: SENTINEL,
      slice_plan: {
        plan_steps: steps,
        step_count: 8,
        move_units: units,
        wave_phases: [
          { phase_wave: 1, step_numbers: [1], engines: ['aurora_mysql'], stage_labels: ['1'], stage_kinds: ['whole_database'], stage_count: 1, query_count: 30 },
          { phase_wave: 2, step_numbers: [2], engines: ['elasticache'], stage_labels: ['2a'], stage_kinds: ['cache_reads'], stage_count: 1, query_count: 2 },
          { phase_wave: 3, step_numbers: [3, 4, 5, 6, 7, 8], engines: Array(6).fill('dynamodb'), stage_labels: ['3a', '4a', '5a', '6a', '7a', '8a'], stage_kinds: ['replica_reads', 'self_contained'], stage_count: 6, query_count: 15 },
        ],
      },
      adoption: {
        default_path_id: 'full_target',
        paths,
        path_count: 7,
        summary_path_ids: ['relational_only', 'through_step_3', 'through_step_5', 'through_step_6', 'full_target'],
        cost_basis: costBasis,
        cost_basis_note: costBasis === 'per_path_sizing'
          ? "Each path's cost adds up its engines' estimates for the queries that path places on each of them, so the relational engine is estimated for every query the path leaves on it."
          : "Each path's cost adds up its engines' estimates for the full target.",
        relational_monthly_cost_usd: relational && costBasis === 'per_path_sizing' ? 102 : null,
        not_offered: [],
        one_step: {
          step_count: 1,
          staged_step_count: 8,
          staged_stage_count: 8,
          gate_count: 8,
          temporary_stream_count: 0,
          staged_temporary_stream_count: 3,
          suits_one_step: false,
          one_step_checks: [
            { rule: 'no_open_critical_high_risk', observed: 2, limit: 0, passed: false },
            { rule: 'at_most_one_purpose_built_target', observed: 1, limit: 1, passed: true },
            { rule: 'no_reverse_replica_stage', observed: 1, limit: 0, passed: false },
          ],
        },
      },
    },
  };
}

const chips = () => [...container.querySelectorAll('[data-path] > button')];
const chip = (id) => container.querySelector(`[data-path="${id}"] > button`);
const figure = (name, scope = container) => scope.querySelector(`[data-figure="${name}"]`);
const tiles = () => ({
  engines: figure('path-engines').textContent,
  queries: figure('path-queries-moved').textContent,
  cost: figure('path-cost').firstChild.textContent,
  difference: figure('path-cost-difference') && figure('path-cost-difference').textContent,
  risks: figure('path-risks').textContent,
});

describe('the path selector', () => {
  test('opens on the full target, the default, with its four tiles', () => {
    render(synthesis());
    expect(figure('selected-path').getAttribute('data-path')).toBe('full_target');
    expect(chip('full_target').getAttribute('aria-pressed')).toBe('true');
    expect(chip('full_target').textContent).toBe('Full target, the default');
    expect(tiles()).toEqual({
      engines: 'Aurora MySQL, ElastiCache, DynamoDB',
      queries: '80',
      cost: '$108.00',
      difference: '$6.00 more than the relational move only',
      risks: '2',
    });
    expect(container.textContent).toContain('Queries off Aurora MySQL');
    expect(figure('path-steps').textContent).toBe('Steps 1 to 8, the last on DynamoDB in wave 3; 8 stages, 8 gates');
  });

  // passes on main: guards behaviour main already has (nothing greyed); review of #2249.
  test('the default view greys nothing: today\'s roadmap', () => {
    render(synthesis());
    expect(container.querySelectorAll('[data-left-out]')).toHaveLength(0);
    expect(figure('wave-left-out')).toBeNull();
    expect(figure('wave-path-stops')).toBeNull();
  });

  test('the capped selector shows only the paths summary_path_ids names, in path order', () => {
    render(synthesis());
    expect(chips().map(c => c.parentElement.getAttribute('data-path'))).toEqual(
      ['relational_only', 'through_step_3', 'through_step_5', 'through_step_6', 'full_target'],
    );
    expect(chips().map(c => c.textContent)).toEqual(
      ['Relational move only', 'Through step 3', 'Through step 5', 'Through step 6', 'Full target, the default'],
    );
    const showAll = figure('adoption-show-all').querySelector('button');
    expect(showAll.textContent).toBe('Show all 7 paths');
    click(showAll);
    expect(chips()).toHaveLength(7);
    expect(chip('through_step_4')).not.toBeNull();
    expect(figure('adoption-show-all').querySelector('button').textContent).toBe('Show fewer paths');
  });

  test('after "Show fewer", a picked path that is no longer shown falls back to the default', () => {
    render(synthesis());
    click(figure('adoption-show-all').querySelector('button'));
    click(chip('through_step_4'));
    expect(figure('selected-path').getAttribute('data-path')).toBe('through_step_4');
    click(figure('adoption-show-all').querySelector('button'));
    expect(chip('through_step_4')).toBeNull();
    expect(figure('selected-path').getAttribute('data-path')).toBe('full_target');
    expect(chip('full_target').getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelectorAll('[data-left-out]')).toHaveLength(0);
  });

  test('after "Show fewer", a picked path that is still shown stays picked', () => {
    render(synthesis());
    click(figure('adoption-show-all').querySelector('button'));
    click(chip('through_step_3'));
    click(figure('adoption-show-all').querySelector('button'));
    expect(figure('selected-path').getAttribute('data-path')).toBe('through_step_3');
    expect(chip('through_step_3').getAttribute('aria-pressed')).toBe('true');
  });

  test('every path fits: no "Show all"', () => {
    const s = synthesis();
    s.facts.adoption.summary_path_ids = s.facts.adoption.paths.map(p => p.path_id);
    render(s);
    expect(chips()).toHaveLength(7);
    expect(figure('adoption-show-all')).toBeNull();
  });

  test('choosing an earlier path shows its tiles as stored', () => {
    render(synthesis());
    click(chip('through_step_3'));
    expect(figure('selected-path').getAttribute('data-path')).toBe('through_step_3');
    expect(chip('through_step_3').getAttribute('aria-pressed')).toBe('true');
    expect(chip('full_target').getAttribute('aria-pressed')).toBe('false');
    expect(tiles()).toEqual({
      engines: 'Aurora MySQL, ElastiCache, DynamoDB',
      queries: '30',
      cost: '$103.00',
      difference: '$15.33 less than the relational move only',
      risks: '1',
    });
  });

  test('the relational move only states no difference from itself', () => {
    render(synthesis());
    click(chip('relational_only'));
    expect(tiles().difference).toBeNull();
    expect(tiles().cost).toBe('$102.00');
    expect(tiles().engines).toBe('Aurora MySQL, ElastiCache');
  });

  test('choosing a path never writes anything and leaves report.json as it was', () => {
    const s = synthesis();
    const before = JSON.stringify(s);
    const fetchSpy = jest.fn();
    const saved = global.fetch;
    global.fetch = fetchSpy;
    try {
      render(s);
      click(chip('through_step_3'));
      click(chip('relational_only'));
      click(chip('full_target'));
    } finally {
      global.fetch = saved;
    }
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(JSON.stringify(s)).toBe(before);
    expect(container.textContent).toContain('Choosing a path changes only what this page shows; the assignment stays as it is.');
  });

  test('no path\'s moving-table count is printed, on any path', () => {
    render(synthesis());
    ['full_target', 'through_step_3', 'relational_only'].forEach((id) => {
      click(chip(id));
      expect(container.textContent).not.toContain(String(SENTINEL));
      expect(container.textContent).not.toContain('9,137');
    });
  });

  test('no raw path id or engine id reaches the page', () => {
    render(synthesis());
    click(figure('adoption-show-all').querySelector('button'));
    const text = container.textContent;
    ['relational_only', 'through_step', 'full_target', 'aurora_mysql', 'elasticache:', 'dynamodb:'].forEach((raw) => {
      expect(text).not.toContain(raw);
    });
  });
});

describe('the cost-basis rule', () => {
  test('per_path_sizing: each path states its cost and the facts\' basis sentence', () => {
    render(synthesis());
    click(chip('through_step_5'));
    expect(tiles().cost).toBe('$105.00');
    expect(figure('path-cost-note').textContent).toBe(
      "Each path's cost adds up its engines' estimates for the queries that path places on each of them, so the relational engine is estimated for every query the path leaves on it.",
    );
  });

  test('full_target_estimates: an earlier path states no cost or difference, the full target its cost', () => {
    render(synthesis({ costBasis: 'full_target_estimates' }));
    expect(tiles().cost).toBe('$108.00');
    expect(tiles().difference).toBeNull();
    click(chip('through_step_3'));
    expect(tiles().cost).toBe('Stated for the full target only');
    expect(tiles().difference).toBeNull();
    expect(container.textContent).not.toContain('$103.00');
    expect(figure('path-cost-note').textContent).toBe(
      "Only the full target's cost is stated: this report was written before each path was estimated for itself.",
    );
  });
});

describe('a plan with no relational step', () => {
  function noRelational() {
    const s = synthesis({ relational: false });
    const plan = s.facts.slice_plan;
    plan.plan_steps[0].kind = 'slice';
    plan.plan_steps[0].engine = 'dynamodb';
    // relational_only is not offered; every path still holds a difference number.
    s.facts.adoption.paths = s.facts.adoption.paths.slice(1);
    s.facts.adoption.paths.forEach((p) => { p.cost_delta_vs_relational_usd = 42; });
    s.facts.adoption.path_count = 6;
    s.facts.adoption.summary_path_ids = ['through_step_3', 'through_step_4', 'through_step_5', 'through_step_6', 'full_target'];
    s.facts.adoption.not_offered = [{ path_id: 'relational_only', reason: 'no_relational_engine', offered_instead: 'full_target' }];
    return s;
  }

  test('prints no difference from the relational move only, on any path', () => {
    render(noRelational());
    expect(tiles().difference).toBeNull();
    click(chip('through_step_3'));
    expect(tiles().difference).toBeNull();
    expect(tiles().cost).toBe('$103.00');
    expect(container.textContent).not.toContain('relational move only');
    expect(container.textContent).not.toContain('$42.00');
  });

  test('counts queries moved, not queries off an engine, and says why the relational move is not offered', () => {
    render(noRelational());
    expect(container.textContent).toContain('Queries moved');
    expect(container.textContent).not.toContain('Queries off');
    expect(figure('path-not-offered').textContent).toBe(
      'Relational move only is not offered: no step of this plan keeps the whole database on one relational engine. Full target is offered in its place.',
    );
  });
});

describe('the waves and stages a path leaves out', () => {
  test('a path that stops inside a wave greys the stages of the steps it does not take', () => {
    render(synthesis());
    click(chip('through_step_3'));
    const wave3 = container.querySelector('[data-wave="3"]');
    expect(wave3.getAttribute('data-left-out')).toBeNull();
    expect(figure('wave-path-stops', wave3).textContent).toBe(
      'Through step 3 leaves out the stages after step 3.',
    );
    click(wave3.querySelector('[data-disclosure="wave-stage-list"]'));
    const stages = [...wave3.querySelectorAll('[data-stage]')];
    expect(stages.map(s => [s.getAttribute('data-stage'), s.getAttribute('data-left-out')])).toEqual([
      ['3a', null], ['4a', 'true'], ['5a', 'true'], ['6a', 'true'], ['7a', 'true'], ['8a', 'true'],
    ]);
    expect(stages[1].textContent).toContain('Left out of this path');
    // Review of #2249 (WCAG AA): nothing is faded with opacity; the stage carries a
    // full-contrast label as its non-colour cue.
    stages.forEach((st) => expect(st.style.opacity).toBe(''));
    expect(figure('stage-left-out', stages[1]).closest('[data-left-out-label]')).not.toBeNull();
    expect(figure('stage-left-out', stages[0])).toBeNull();
    expect(container.querySelector('[data-wave="1"]').getAttribute('data-left-out')).toBeNull();
  });

  test('a path that takes none of a wave\'s steps greys the whole wave', () => {
    render(synthesis());
    click(chip('relational_only'));
    const wave3 = container.querySelector('[data-wave="3"]');
    expect(wave3.getAttribute('data-left-out')).toBe('true');
    expect(figure('wave-left-out', wave3).textContent).toBe(
      "Left out of Relational move only: this path takes none of this wave's steps.",
    );
    expect(container.querySelector('[data-wave="2"]').getAttribute('data-left-out')).toBeNull();
    // Review of #2249 (WCAG AA): the card is not faded; a "Left out of this path" badge
    // marks it, and the reason line stays at full contrast.
    expect(wave3.style.opacity).toBe('');
    container.querySelectorAll('[data-wave] *').forEach(el => expect(el.style.opacity).toBe(''));
    expect(figure('wave-left-out-badge', wave3).textContent).toBe('Left out of this path');
    expect(figure('wave-left-out-badge', container.querySelector('[data-wave="2"]'))).toBeNull();
  });

  test('going back to the full target greys nothing again', () => {
    render(synthesis());
    click(chip('relational_only'));
    click(chip('full_target'));
    expect(container.querySelectorAll('[data-left-out]')).toHaveLength(0);
  });
});

describe('a wave card\'s stages', () => {
  test('are collapsed to "N stages (…)" and expand in place, in run order, with their gates', () => {
    render(synthesis());
    const wave3 = container.querySelector('[data-wave="3"]');
    expect(figure('wave-stages-line', wave3).textContent).toBe('Steps 3 to 8: 6 stages');
    expect(wave3.querySelectorAll('[data-stage]')).toHaveLength(0);
    const show = wave3.querySelector('[data-disclosure="wave-stage-list"]');
    expect(show.textContent).toBe('(…)');
    expect(show.getAttribute('aria-label')).toBe('Show the 6 stages');
    click(show);
    const first = wave3.querySelector('[data-stage="3a"]');
    expect(figure('stage-line', first).textContent).toBe('Stage 3a: reads on a CDC copy; 2 units, 3 queries');
    expect(figure('stage-gate', first).textContent).toBe('Gate: Gate of stage 3a.');
    expect(figure('wave-stages-line', container.querySelector('[data-wave="1"]')).textContent).toBe('Step 1: 1 stage');
  });

  test('a stage\'s units expand the same way, the pilot named, never by key', () => {
    render(synthesis());
    const wave3 = container.querySelector('[data-wave="3"]');
    click(wave3.querySelector('[data-disclosure="wave-stage-list"]'));
    const first = wave3.querySelector('[data-stage="3a"]');
    const show = first.querySelector('[data-disclosure="stage-units"]');
    expect(show.getAttribute('aria-label')).toBe('Show the 2 units');
    click(show);
    // The unit with no table either way is left out, as the report leaves it out.
    expect([...first.querySelectorAll('[data-figure="stage-unit"]')].map(u => u.textContent)).toEqual([
      'Pilot: 4 queries; owns app.carts; reads a copy of app.users; CDC copy back to Aurora MySQL (2 reads)',
    ]);
    expect(first.textContent).not.toContain('dynamodb:');
  });

  test('the whole-database and cache stages list no units of their own', () => {
    render(synthesis());
    ['1', '2'].forEach((n) => {
      const wave = container.querySelector(`[data-wave="${n}"]`);
      click(wave.querySelector('[data-disclosure="wave-stage-list"]'));
      expect(wave.querySelector('[data-disclosure="stage-units"]')).toBeNull();
    });
  });

  test('a stage an open risk holds names the risk', () => {
    const s = synthesis();
    s.facts.slice_plan.plan_steps[3].step_stages[0] = stage('4a', 'risk_held', ['dynamodb:app.t4'], { holding_risk_ids: ['RISK-002'] });
    render(s);
    const wave3 = container.querySelector('[data-wave="3"]');
    click(wave3.querySelector('[data-disclosure="wave-stage-list"]'));
    expect(figure('stage-line', wave3.querySelector('[data-stage="4a"]')).textContent).toBe(
      'Stage 4a: held by an open risk (RISK-002); 1 unit, 3 queries',
    );
  });
});

describe('an unknown stage kind or one-step rule', () => {
  test('reads as a dash or the rule in words, never a locale key', () => {
    const s = synthesis();
    s.facts.slice_plan.plan_steps[3].step_stages[0] = stage('4a', 'some_new_kind', ['dynamodb:app.t4']);
    s.facts.adoption.one_step.one_step_checks.push({ rule: 'no_new_thing_here', observed: 0, limit: 0, passed: true });
    render(s);
    const wave3 = container.querySelector('[data-wave="3"]');
    click(wave3.querySelector('[data-disclosure="wave-stage-list"]'));
    expect(figure('stage-line', wave3.querySelector('[data-stage="4a"]')).textContent).toBe(
      'Stage 4a: -; 1 unit, 3 queries',
    );
    const row = container.querySelector('[data-rule="no_new_thing_here"]');
    expect(row.children[0].textContent).toBe('No new thing here');
    expect(container.textContent).not.toContain('analysis-results.');
    expect(container.textContent).not.toContain('some_new_kind');
  });
});

describe('the one-step option', () => {
  test('is collapsed behind "Show the one-step option" and states the one-step facts', () => {
    render(synthesis());
    const card = figure('one-step-option');
    expect(card.querySelector('summary').textContent).toBe('Show the one-step option');
    expect(figure('one-step-in-place', card).textContent).toBe('The same end state in one step, in place of 8 steps and 8 stages.');
    expect(figure('one-step-gate', card).textContent).toBe('Its one gate holds all 8 stage gates.');
    expect(figure('one-step-streams', card).textContent).toBe(
      'It needs none of the 3 CDC copies back to an earlier engine that the staged form carries.',
    );
    expect(figure('one-step-verdict', card).textContent).toBe(
      'These checks describe the shape of this workload. Not every check holds, so it does not suit a one-step move.',
    );
    expect([...card.querySelectorAll('[data-figure="one-step-check"]')].map(r => [...r.children].map(c => c.textContent))).toEqual([
      ['No open critical or high risk', '2', '0', 'no'],
      ['At most one purpose-built target', '1', '1', 'yes'],
      ['No stage that needs a CDC copy back to an earlier engine', '1', '0', 'no'],
    ]);
  });

  test('every check holding reads as suiting a one-step move', () => {
    const s = synthesis();
    const one = s.facts.adoption.one_step;
    one.suits_one_step = true;
    one.one_step_checks = one.one_step_checks.map(c => ({ ...c, observed: 0, passed: true }));
    render(s);
    expect(figure('one-step-verdict').textContent).toBe(
      'These checks describe the shape of this workload. Every check holds, so it suits a one-step move.',
    );
  });

  // Review of #2249: the pointer follows the engineering report's own condition
  // (one_step_pointer: a schema design that completed), never whether the page has a
  // design section. With none completed it is the report's words and the Target engines tile.
  test('with no completed schema design it points at the Target engines, as the report does', () => {
    const s = synthesis();
    s.schema_designs = { aurora_mysql: { status: 'skipped' }, dynamodb: { status: 'skipped' } };
    // The page has a table design section, but no completed design: the pointer stays.
    render(s, { schemaSectionId: 'table-design-section' });
    const link = figure('one-step-adopt').querySelector('a');
    expect(link.getAttribute('href')).toBe('#target-engines');
    expect(figure('one-step-adopt').textContent).toBe(
      'To take the full target in one step, adopt the target architecture in the Target engines table above directly.',
    );
  });

  test('with a completed schema design it points at the target schemas', () => {
    const s = synthesis();
    s.schema_designs = { aurora_mysql: { status: 'skipped' }, dynamodb: { status: 'completed' } };
    render(s, { schemaSectionId: 'access-patterns-section' });
    const link = figure('one-step-adopt').querySelector('a');
    expect(link.getAttribute('href')).toBe('#access-patterns-section');
    expect(figure('one-step-adopt').textContent).toBe(
      'To take the full target in one step, adopt the target schemas below directly.',
    );
  });

  test('a report with no schema designs at all points at the Target engines', () => {
    render(synthesis(), { schemaSectionId: 'access-patterns-section' });
    expect(figure('one-step-adopt').querySelector('a').getAttribute('href')).toBe('#target-engines');
  });

  test('says nothing about the cutover; the plan says it once', () => {
    render(synthesis());
    expect(figure('one-step-option').textContent.toLowerCase()).not.toContain('cutover');
    expect(container.querySelectorAll('[data-figure="cutover-responsibility"]')).toHaveLength(1);
  });

  test('the roadmap heading points at the one-step option', () => {
    render(synthesis());
    expect(container.querySelector('.mock-description').textContent).toBe(
      'This is one suggested adoption path. To modernize in one step instead, open the one-step option below.',
    );
  });
});

describe('a report without adoption facts', () => {
  // passes on main: guards behaviour main already has (an old report's roadmap); review of #2249.
  test('renders the roadmap as before: no selector, no tiles, no stages, nothing greyed', () => {
    render(fixture.synthesis);
    expect(figure('adoption-paths')).toBeNull();
    expect(figure('one-step-option')).toBeNull();
    expect(figure('wave-stages')).toBeNull();
    expect(container.querySelectorAll('[data-left-out]')).toHaveLength(0);
    expect(container.textContent).not.toContain('Adoption paths');
    expect(container.querySelector('.mock-description').textContent).toBe(
      'This is one suggested adoption path. To modernize in one step instead, '
      + 'adopt the target architecture in Executive summary above directly.',
    );
  });

  // passes on main: guards behaviour main already has (no path, nothing new); review of #2249.
  test('an adoption block that offers no path shows nothing new either', () => {
    const s = synthesis();
    s.facts.adoption = { default_path_id: null, paths: [], path_count: 0, summary_path_ids: [], not_offered: [], one_step: null };
    delete s.facts.slice_plan;
    render(s);
    expect(figure('adoption-paths')).toBeNull();
    expect(figure('wave-stages')).toBeNull();
    expect(container.querySelectorAll('[data-wave]')).toHaveLength(3);
  });
});
