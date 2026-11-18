// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2322: each open risk is one expandable section. The header carries the severity in
 * sentence case, the engine, the id and a one-line title; the body carries the
 * description, mitigation, affected tables and design notes. These render the real
 * Cloudscape components and the real English strings.
 */
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import RiskCard from '../RiskCard';

global.IS_REACT_ACT_ENVIRONMENT = true;

const LONG = 'Writes to the orders table fan out to many items. '.repeat(1);
const RISK = {
  risk_id: 'R-7',
  severity: 'HIGH',
  description: `[dynamodb] ${LONG}A second sentence with more detail.`,
  mitigation: 'Batch the writes.',
  affected_tables: ['shop.orders', 'shop.order_items'],
  details: ['See the design guide.'],
};

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

function Harness({ risks, headingLevel = 'h3' }) {
  const [open, setOpen] = useState({});
  return (
    <div>
      {risks.map((r, i) => {
        const key = r.risk_id ? `risk-${r.risk_id}` : `risk-${r.severity}-${i}`;
        return (
          <RiskCard
            key={key}
            id={key}
            risk={r}
            severity={r.severity}
            expanded={open[key] ?? false}
            onToggle={(id, expanded) => setOpen((o) => ({ ...o, [id]: expanded }))}
            headingLevel={headingLevel}
            engineLabel="DynamoDB"
            mitigationRef={null}
            copySync={r.copySync || null}
            t={t}
          />
        );
      })}
    </div>
  );
}
const render = (props) => act(() => root.render(<Harness risks={[RISK]} {...props} />));
const header = () => container.querySelector('[role="button"][aria-expanded]');
const press = (el, key, keyCode) => act(() => {
  el.dispatchEvent(new KeyboardEvent('keyup', { key, keyCode, bubbles: true }));
});

test('the header reads severity in sentence case, engine, id and a one-line title', () => {
  render();
  const text = header().textContent;
  expect(text).toContain('High');
  expect(text).not.toContain('HIGH');
  expect(text).toContain('DynamoDB');
  expect(text).toContain('R-7');
  expect(text).toContain('Writes to the orders table fan out to many items.');
  expect(text).not.toContain('A second sentence');
});

test('no CSS text-transform is used for the severity', () => {
  render();
  const label = [...container.querySelectorAll('*')].find((n) => n.textContent === 'High' && n.children.length === 0);
  expect(label).toBeTruthy();
  expect(getComputedStyle(label).textTransform).not.toBe('uppercase');
});

test('the body is closed at first and opens and closes with Enter and Space', () => {
  render();
  expect(header().getAttribute('aria-expanded')).toBe('false');
  expect(header().getAttribute('tabindex')).toBe('0');
  press(header(), 'Enter', 13);
  expect(header().getAttribute('aria-expanded')).toBe('true');
  expect(container.textContent).toContain('A second sentence with more detail.');
  expect(container.textContent).toContain('Batch the writes.');
  press(header(), ' ', 32);
  expect(header().getAttribute('aria-expanded')).toBe('false');
});

test('the heading level is the one the page passes', () => {
  render({ headingLevel: 'h3' });
  expect(container.querySelector('h3 [role="button"], h3')).toBeTruthy();
  expect(container.querySelector('h2')).toBeNull();
  act(() => root.render(<Harness risks={[RISK]} headingLevel="h4" />));
  expect(container.querySelector('h4')).toBeTruthy();
});

test('mitigation and affected tables are key-value pairs, the tables behind a popover with no count', () => {
  render();
  press(header(), 'Enter', 13);
  const labels = [...container.querySelectorAll('[class*="awsui_key-value-pairs"] dt, [class*="awsui_key-value-pairs"] [class*="detail-label"], dt')].map((n) => n.textContent);
  expect(labels).toEqual(expect.arrayContaining(['Mitigation', 'Affects']));
  const trigger = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Show the affected tables');
  expect(trigger).toBeTruthy();
  expect(container.textContent).not.toContain('shop.orders');
  expect(container.textContent).not.toMatch(/\b2 tables\b/);
  act(() => trigger.click());
  expect(document.body.textContent).toContain('shop.orders');
  expect(document.body.textContent).toContain('shop.order_items');
});

test('design notes sit in a nested expandable, one level below the card heading', () => {
  render();
  press(header(), 'Enter', 13);
  const buttons = container.querySelectorAll('[role="button"][aria-expanded]');
  expect(buttons).toHaveLength(2);
  expect(buttons[1].textContent).toBe('Design notes');
  expect(container.querySelector('h4')).toBeTruthy();
  expect(buttons[1].getAttribute('aria-expanded')).toBe('false');
  press(buttons[1], 'Enter', 13);
  expect(buttons[1].getAttribute('aria-expanded')).toBe('true');
  expect(container.textContent).toContain('See the design guide.');
});

test('the copy-sync lines and basis join the design notes', () => {
  const risk = { ...RISK, details: [], copySync: { lines: ['CDC keeps the copy current.'], basis: 'capture window' } };
  render({ risks: [risk] });
  press(header(), 'Enter', 13);
  press(container.querySelectorAll('[role="button"][aria-expanded]')[1], 'Enter', 13);
  expect(container.textContent).toContain('CDC keeps the copy current.');
  expect(container.textContent).toContain('Basis: capture window');
});

test('a risk with no mitigation, tables or notes has only its description in the body', () => {
  const risk = { risk_id: 'R-9', severity: 'LOW', description: 'A short risk. It has a second sentence.' };
  render({ risks: [risk] });
  press(header(), 'Enter', 13);
  expect(container.querySelectorAll('[role="button"][aria-expanded]')).toHaveLength(1);
  expect(container.textContent).toContain('It has a second sentence.');
  expect(container.textContent).not.toContain('Mitigation');
});

test('each card keeps its own state under its risk id, and an unrated severity reads Unrated', () => {
  const other = { risk_id: 'R-8', severity: 'WEIRD', description: 'Another risk.' };
  render({ risks: [RISK, other] });
  const buttons = () => [...container.querySelectorAll('[data-risk-card]')].map((c) => c.querySelector('[role="button"][aria-expanded]'));
  press(buttons()[1], 'Enter', 13);
  expect(buttons()[0].getAttribute('aria-expanded')).toBe('false');
  expect(buttons()[1].getAttribute('aria-expanded')).toBe('true');
  expect(buttons()[1].textContent).toContain('Unrated');
  act(() => root.render(<Harness risks={[other, RISK]} />));
  expect(buttons()[0].getAttribute('aria-expanded')).toBe('true');
  expect(buttons()[1].getAttribute('aria-expanded')).toBe('false');
});
