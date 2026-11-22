// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1939: the results page's "Routines" section. It shows report.json's
 * facts.routines (#1938) as stored -- the same facts the engineering report's
 * Routines section prints (fixtures/routineFacts.json is checked against the
 * Python facts by tests/unit/shared/test_routine_fact_words.py): the headline,
 * the to-do with the routines to migrate yourself and its wave, and each
 * routine behind a routine risk by id, kind and status with why in plain
 * words. A report without facts.routines shows nothing. Cloudscape components
 * are plain DOM stand-ins (see the jest.mock calls below); the component
 * and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import RoutineMigrationSection from '../RoutineMigrationSection';
import routines from './fixtures/routineFacts.json';
import withInterventions from './fixtures/routineInterventions.json';
import withAfterRepair from './fixtures/routineAfterRepair.json';
import withRepairHeld from './fixtures/routineRepairHeld.json';

jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children, variant, defaultExpanded }) => (
    <section data-variant={variant} data-default-expanded={String(defaultExpanded)}>
      <h3>{headerText}</h3>
      {children}
    </section>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children, variant, ...props }) => (
    <div
      data-box-variant={variant}
      {...Object.fromEntries(Object.entries(props).filter(([k]) => k.startsWith('data-')))}
    >
      {children}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ items, columnDefinitions, trackBy }) => (
    <table>
      <thead>
        <tr>{columnDefinitions.map(c => <th key={c.id}>{c.header}</th>)}</tr>
      </thead>
      <tbody>
        {items.map(item => (
          <tr key={trackBy(item)} data-routine={item.routine_id}>
            {columnDefinitions.map(c => <td key={c.id}>{c.cell(item)}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  ),
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

function render(synthesis) {
  act(() => root.render(<RoutineMigrationSection synthesis={synthesis} t={t} />));
}

const TO_DO = 'We could not translate 5 routines automatically; migrate them yourself before wave 1.';

test('shows the headline and the to-do up front, the detail collapsed', () => {
  render({ facts: { routines } });
  const headline = container.querySelector('[data-figure="routines-headline"]');
  expect(headline.closest('section')).toBeNull();
  expect(container.querySelector('[data-figure="routines-line"]').textContent).toBe(routines.line);
  expect(container.querySelector('[data-figure="routines-to-do-line"]').textContent).toBe(TO_DO);
  const section = container.querySelector('section');
  expect(section.getAttribute('data-default-expanded')).toBe('false');
  expect(container.querySelector('h3').textContent).toBe('Routines (10 routines)');
});

test('lists the routines to migrate yourself from the facts and the step with its wave', () => {
  render({ facts: { routines } });
  const toDo = container.querySelector('[data-figure="routines-to-do"]');
  const items = [...toDo.querySelectorAll('li')].map(li => li.textContent);
  expect(items).toEqual(routines.to_do_routines.map(r => `${r.kind} ${r.routine_id}`));
  expect(items).toContain('trigger public.orders.tg_audit');
  expect(toDo.textContent).toContain('Migrate these routines yourself before wave 1, then check them again.');
});

test('lists only the listed routines, with status, why and the routines they name', () => {
  render({ facts: { routines } });
  const table = container.querySelector('[data-figure="routines-table"]');
  const rows = [...table.querySelectorAll('tbody tr')];
  const listed = routines.routines.filter(r => r.listed);
  expect(rows.map(r => r.getAttribute('data-routine'))).toEqual(listed.map(r => r.routine_id));
  expect(table.textContent).not.toContain('public.plain_unknown()');
  listed.forEach((r, i) => {
    const cells = [...rows[i].querySelectorAll('td')].map(td => td.textContent);
    expect(cells.slice(0, 4)).toEqual([r.routine_id, r.kind, r.status_text, r.why.join(' ')]);
  });
  const fa = rows.find(r => r.getAttribute('data-routine') === 'public.f_a()');
  expect([...fa.querySelectorAll('td')][4].textContent).toBe('function public.f_b()');
  expect(table.textContent).not.toMatch(/needs_attention|dependency_cycle|source_text_not/);
  expect(container.textContent.toLowerCase()).not.toContain('proven');
});

test('rolls the rewrites and the checks that could not run up once per note', () => {
  render({ facts: { routines } });
  const notes = [...container.querySelectorAll('[data-figure="routines-notes"] li')].map(li => li.textContent);
  expect(notes).toEqual(routines.check_notes.map(n => (
    `${n.label} (${n.note_count}): ${n.routines.map(r => r.routine_id).join(', ')}.${n.text ? ` ${n.text}` : ''}`
  )));
  expect(container.querySelector('[data-figure="routines-others"]').textContent).toBe(routines.others_line);
});

test('a long note says how many more', () => {
  const note = { ...routines.check_notes[1], note_count: 13, more_count: 3 };
  render({ facts: { routines: { ...routines, check_notes: [note] } } });
  expect(container.querySelector('[data-figure="routines-notes"] li').textContent).toContain(', … and 3 more.');
});

test('a function and a procedure of one name are two rows with their own keys', () => {
  const errors = jest.spyOn(console, 'error').mockImplementation(() => {});
  const base = routines.routines.find(r => r.routine_id === 'public.py_fn()');
  const twins = [
    { ...base, kind: 'function', routine_id: 'shop.place_order' },
    { ...base, kind: 'procedure', routine_id: 'shop.place_order' },
  ];
  const refs = twins.map(r => ({ kind: r.kind, routine_id: r.routine_id }));
  render({ facts: { routines: { ...routines, routines: twins, to_do_routines: refs } } });
  expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  expect([...container.querySelectorAll('[data-figure="routines-to-do"] li')].map(li => li.textContent))
    .toEqual(['function shop.place_order', 'procedure shop.place_order']);
  expect(errors.mock.calls.filter(c => String(c[0]).includes('same key'))).toEqual([]);
  errors.mockRestore();
});

test('no to-do without a routine to migrate yourself', () => {
  const none = {
    ...routines,
    migrate_yourself_line: null,
    migrate_yourself_step: null,
    to_do_routines: [],
    routines: routines.routines.filter(r => !r.risk_classes.includes('migrate_yourself')),
  };
  render({ facts: { routines: none } });
  expect(container.querySelector('[data-figure="routines-to-do"]')).toBeNull();
  expect(container.querySelector('[data-figure="routines-to-do-line"]')).toBeNull();
  expect(container.querySelector('[data-figure="routines-table"]')).not.toBeNull();
});

test('with nothing listed, states the others line and no table', () => {
  const clean = {
    ...routines,
    routines: [],
    check_notes: [],
    to_do_routines: [],
    migrate_yourself_line: null,
    others_line: 'Each of the 2 routines is carried and every check on it passed.',
  };
  render({ facts: { routines: clean } });
  expect(container.querySelector('[data-figure="routines-table"]')).toBeNull();
  expect(container.querySelector('[data-figure="routines-others"]').textContent).toBe(clean.others_line);
});

test('a report without routine facts shows nothing', () => {
  render({ facts: {} });
  expect(container.innerHTML).toBe('');
  render({ facts: { routines: null } });
  expect(container.innerHTML).toBe('');
  render({});
  expect(container.innerHTML).toBe('');
});


// The page's heading order (axe heading-order on the results page, a regression from #2108):
// the section sits in wave 1's card under its h3 title (#2318), so each sub-heading is an h4.
test('the sub-headings follow the page heading order: h4 under the wave\'s h3 title', () => {
  render({ facts: { routines } });
  const section = container.querySelector('#routines-section');
  const headings = [...section.querySelectorAll('[data-box-variant^="h"]')].map(
    (el) => el.getAttribute('data-box-variant'),
  );
  expect(headings.length).toBeGreaterThan(0);
  expect(headings.every((v) => v === 'h4')).toBe(true);
  expect(section.querySelector('[data-figure="routines-to-do"] [data-box-variant="h4"]')).not.toBeNull();
});

// #1941: what came of each model repair, in the facts' own words
// (fixtures/routineInterventions.json is checked against the Python facts by
// tests/unit/report/test_routine_interventions_deliverables.py).
test('lists each intervention with its line, and every attempt of one that needs attention', () => {
  render({ facts: { routines: withInterventions } });
  const box = container.querySelector('[data-figure="routines-interventions"]');
  expect(box.querySelector('[data-box-variant="h4"]').textContent).toBe('Interventions');
  const items = [...box.querySelectorAll(':scope > ul > li')];
  expect(items.map(li => li.getAttribute('data-intervention'))).toEqual(
    withInterventions.interventions.map(i => i.state),
  );
  withInterventions.interventions.forEach((item, n) => {
    expect(items[n].textContent).toContain(`${item.kind} ${item.routine_id}: ${item.line}`);
    const attempts = [...items[n].querySelectorAll('ul li')].map(li => li.textContent);
    expect(attempts).toEqual(item.attempt_lines);
  });
  const repaired = withInterventions.interventions.find(i => i.state === 'model_repaired');
  expect(box.textContent).toContain(`What changed: ${repaired.change_note}`);
  // #2385: a model repair reads as compared on a local engine, not tested on Aurora.
  expect(box.textContent).toContain('a local engine of the source');
  expect(box.textContent).toContain('it is checked, not tested yet on a test Aurora');
  const pending = withInterventions.interventions.find(i => i.state === 'repaired_pending_proof');
  expect(box.textContent).not.toContain(pending.change_note);
});

// #2428: each routine carried after a repair, with the routines it follows, in the
// facts' own words (fixtures/routineAfterRepair.json is checked against the Python facts
// by tests/unit/tools/routines/test_carried_after_repair.py).
test('lists each routine carried after a repair as checked, not tested', () => {
  render({ facts: { routines: withAfterRepair } });
  const box = container.querySelector('[data-figure="routines-after-repair"]');
  expect(box.querySelector('[data-box-variant="h4"]').textContent).toBe('Carried after a repair');
  const items = [...box.querySelectorAll(':scope > ul > li')];
  expect(items).toHaveLength(withAfterRepair.carried_after_repair.length);
  withAfterRepair.carried_after_repair.forEach((item, n) => {
    const after = item.after.map(a => `${a.kind} ${a.routine_id}`).join(', ');
    expect(items[n].textContent).toBe(`${item.kind} ${item.routine_id}, after ${after}: ${item.line}`);
    expect(items[n].getAttribute('data-after-repair')).toBe(item.deploy_gate);
  });
  expect(box.textContent).toContain('checked, not tested yet');
});

// #2566: a repaired routine whose callee is not carried is held by it: its row names the
// routine it waits for and its intervention line says it is not carried, in the facts' own
// words (fixtures/routineRepairHeld.json is checked against the Python facts by
// tests/unit/tools/routines/test_repaired_routine_held_by_callee.py).
test('a repaired routine held by its callee reads as not carried, waiting for it', () => {
  render({ facts: { routines: withRepairHeld } });
  const id = 'public.own_bad(integer)';
  const row = container.querySelector(`[data-routine="${id}"]`);
  expect(row.textContent).toContain('Not carried: needs repair');
  expect(row.textContent).toContain('public.wal_pos(integer)');
  const box = container.querySelector('[data-figure="routines-interventions"]');
  const item = withRepairHeld.interventions.find(i => i.routine_id === id);
  expect(item.deploy_gate).toBe('depends_on_needs_attention');
  expect(box.textContent).toContain(`${item.kind} ${id}: ${item.line}`);
  expect(box.textContent).toContain('It is not carried');
  expect(container.querySelector('[data-figure="routines-after-repair"]')).toBeNull();
});

test('no carried-after-a-repair section without one', () => {
  render({ facts: { routines: withInterventions } });
  expect(container.querySelector('[data-figure="routines-after-repair"]')).toBeNull();
});

test('no interventions section without one', () => {
  render({ facts: { routines } });
  expect(container.querySelector('[data-figure="routines-interventions"]')).toBeNull();
});
