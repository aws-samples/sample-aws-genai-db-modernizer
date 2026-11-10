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
 * are plain DOM stand-ins (see SourcesOfTruthCallout.test.js); the component
 * and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import RoutineMigrationSection from '../RoutineMigrationSection';
import routines from './fixtures/routineFacts.json';

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
  default: ({ children, ...props }) => (
    <div {...Object.fromEntries(Object.entries(props).filter(([k]) => k.startsWith('data-')))}>{children}</div>
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
