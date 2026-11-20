// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2323: the trade-offs tab lists each design decision as plain text in an expandable
 * section (it was a Link that did nothing), with a text filter, short query ids and
 * mapping chips in the engine catalog colours. These render the real Cloudscape
 * components, the real English strings and the real stylesheet.
 */
import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import TradeOffDecisions, { decisionMatches } from '../TradeOffDecisions';
import TradeOffDecision from '../TradeOffDecision';
import { formatShortId } from '../../utils/format';
import { ENGINE_HEX } from '../../utils/engineCatalog.generated';
import { DEFAULT_ENGINE_HEX, contrastRatio } from '../../utils/engineColors';

global.IS_REACT_ACT_ENVIRONMENT = true;

const ID_A = 'a'.repeat(8) + '0123456789abcdef'.repeat(3) + 'b'.repeat(8);
const ID_B = 'c0ffee00' + 'd'.repeat(56);
const DECISIONS = [
  { description: 'Orders live in one item collection.', impact: 'One read serves the page.', source_tables: ['orders'], target_tables: ['Orders'], query_ids: [ID_A] },
  { description: 'Reviews get their own table.', impact: 'Writes stay cheap.', source_tables: ['reviews'], target_tables: ['Reviews'], query_ids: [ID_B] },
  { description: 'Sessions expire by TTL.', impact: '', source_tables: [], target_tables: [], query_ids: [] },
];
const GROUPS = [
  { topic: 'keys', label: 'Key design', count: 2, tradeOffs: DECISIONS.slice(0, 2) },
  { topic: 'other', label: 'Other', count: 1, tradeOffs: DECISIONS.slice(2) },
];

let container;
let root;
let t;
let opened;

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
  opened = [];
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = (props = {}) => act(() => root.render(
  <TradeOffDecisions groups={null} decisions={DECISIONS} engine="dynamodb" onOpenQuery={(id) => opened.push(id)} t={t} {...props} />,
));
const decisionEls = () => [...container.querySelectorAll('[data-trade-off-decision]')];
const headerOf = (el) => el.querySelector('[role="button"][aria-expanded]');
const type = (text) => {
  const input = container.querySelector('input[type="search"], input');
  const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  act(() => {
    set.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};
const toHex = (rgb) => '#' + (/rgb\((\d+), (\d+), (\d+)\)/.exec(rgb) || []).slice(1).map((n) => Number(n).toString(16).padStart(2, '0')).join('');

describe('plain titles', () => {
  test('a title is text: no link, no anchor, nothing that takes focus except the expand button', () => {
    render();
    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('[class*="awsui_link_"]')).toBeNull();
    decisionEls().forEach((el) => {
      const focusable = el.querySelectorAll('a, button, [tabindex="0"], [role="link"]');
      focusable.forEach((f) => expect(f.getAttribute('role') === 'button' || f.tagName === 'BUTTON').toBe(true));
    });
    expect(container.textContent).toContain('Orders live in one item collection.');
  });

  test('Mapping and SQL IDs are key labels, not link-coloured', () => {
    render({ decisions: [DECISIONS[0]] });
    const labels = [...container.querySelectorAll('[class*="awsui_key-label"]')].map((n) => n.textContent);
    expect(labels).toEqual(['Mapping', 'SQL IDs']);
    expect(container.querySelector('[class*="awsui_link_"]')).toBeNull();
  });
});

describe('expand and collapse', () => {
  test('each decision starts closed and opens and closes with Enter and Space', () => {
    render();
    const first = decisionEls()[0];
    const header = headerOf(first);
    expect(header.getAttribute('aria-expanded')).toBe('false');
    act(() => { header.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', keyCode: 13, bubbles: true })); });
    expect(headerOf(first).getAttribute('aria-expanded')).toBe('true');
    expect(headerOf(decisionEls()[1]).getAttribute('aria-expanded')).toBe('false');
    act(() => { headerOf(first).dispatchEvent(new KeyboardEvent('keyup', { key: ' ', keyCode: 32, bubbles: true })); });
    expect(headerOf(first).getAttribute('aria-expanded')).toBe('false');
  });

  test('the header is reachable by keyboard', () => {
    render();
    expect(headerOf(decisionEls()[0]).getAttribute('tabindex')).toBe('0');
  });
});

describe('the filter', () => {
  test('narrows the list by title, impact, table and full query id, with a match count', () => {
    render();
    expect(decisionEls()).toHaveLength(3);
    type('review');
    expect(decisionEls().map((e) => e.textContent)).toEqual([expect.stringContaining('Reviews get their own table.')]);
    expect(container.textContent).toContain('1 match');
    type('writes stay');
    expect(decisionEls()).toHaveLength(1);
    type('ORDERS');
    expect(decisionEls()).toHaveLength(1);
    type(ID_A.slice(20, 40));
    expect(decisionEls()).toHaveLength(1);
    expect(container.textContent).toContain('Orders live');
    type('o');
    expect(container.textContent).toMatch(/\d+ matches/);
  });

  test('says so when nothing matches, and clearing the filter brings every decision back', () => {
    render();
    type('zzz-nothing');
    expect(decisionEls()).toHaveLength(0);
    expect(container.textContent).toContain('No design decision matches the filter.');
    type('');
    expect(decisionEls()).toHaveLength(3);
  });

  test('topic headings keep the facts count until a filter is typed, then show the label alone', () => {
    render({ groups: GROUPS });
    expect(container.textContent).toContain('Key design (2)');
    type('reviews');
    expect(container.textContent).toContain('Key design');
    expect(container.textContent).not.toContain('Key design (2)');
    expect(container.querySelector('[data-trade-off-topic="other"]')).toBeNull();
    expect(decisionEls()).toHaveLength(1);
  });

  test('decisionMatches ignores case and blank text', () => {
    expect(decisionMatches(DECISIONS[0], '  ')).toBe(true);
    expect(decisionMatches(DECISIONS[0], 'ITEM COLLECTION')).toBe(true);
    expect(decisionMatches(DECISIONS[2], 'orders')).toBe(false);
  });
});

describe('query id chips', () => {
  test('show the first 8 characters, keep the full id in the title, and open the query', () => {
    render({ decisions: [DECISIONS[0]] });
    const chip = container.querySelector('button.dbm-id-chip');
    expect(chip.textContent).toBe(ID_A.slice(0, 8));
    expect(chip.getAttribute('title')).toBe(ID_A);
    expect(chip.getAttribute('aria-label')).toContain(ID_A);
    expect(container.textContent).not.toContain(ID_A);
    act(() => chip.click());
    expect(opened).toEqual([ID_A]);
  });
});

describe('mapping chips', () => {
  test('are the shared Badge: targets in the tab engine colour, sources in the neutral grey', () => {
    render({ decisions: [DECISIONS[0]], engine: 'dynamodb' });
    const badge = (kind) => container.querySelector(`[data-mapping="${kind}"] [class*="awsui_badge_"]`);
    expect(toHex(badge('target').style.background)).toBe(ENGINE_HEX.dynamodb.toLowerCase());
    expect(toHex(badge('source').style.background)).toBe(DEFAULT_ENGINE_HEX.toLowerCase());
    expect(badge('target').textContent).toBe('Orders');
    expect(container.querySelector('[data-mapping][style]')).toBeNull();
  });

  test.each(Object.keys(ENGINE_HEX))('the %s tab colours its targets from the catalog', (engine) => {
    render({ decisions: [DECISIONS[0]], engine });
    const el = container.querySelector('[data-mapping="target"] [class*="awsui_badge_"]');
    expect(toHex(el.style.background)).toBe(ENGINE_HEX[engine].toLowerCase());
  });
});

describe('untrusted text', () => {
  test('a title, impact, table and id with markup render as text', () => {
    const evil = '<img src=x onerror=alert(1)><script>boom()</script>';
    render({
      decisions: [{ description: evil, impact: evil, source_tables: [evil], target_tables: [evil], query_ids: [evil] }],
    });
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain(evil);
    expect(container.querySelector('button.dbm-id-chip').getAttribute('title')).toBe(evil);
  });

  test('a backtick span in a title is inline code, not HTML', () => {
    render({ decisions: [{ description: 'Query `8c66eaee` leads.', impact: '', source_tables: [], target_tables: [], query_ids: [] }] });
    expect(container.querySelector('code').textContent).toBe('8c66eaee');
  });
});

describe('the id chip colours', () => {
  // #2311: the chip's colours are Cloudscape design tokens (styles/themeTokens.js), read
  // here in each theme from the installed components' theme stylesheet.
  const css = fs.readFileSync(path.join(__dirname, '..', '..', 'styles', 'global.css'), 'utf8');
  const { THEME_VARS } = require('../../styles/themeTokens');
  const { tokenValues } = require('../../testUtils/themeTokenValues');
  const chip = (theme) => Object.fromEntries(Object.entries(THEME_VARS)
    .filter(([name]) => name.startsWith('--dbm-id-chip-'))
    .map(([name, token]) => [name, tokenValues(token)[theme]]));

  test.each(['light', 'dark'])('in the %s theme the chip text reads 4.5:1 at rest and on hover', (theme) => {
    const v = chip(theme);
    expect(Object.keys(v).sort()).toEqual(['--dbm-id-chip-bg', '--dbm-id-chip-hover-bg', '--dbm-id-chip-hover-text', '--dbm-id-chip-text']);
    expect(contrastRatio(v['--dbm-id-chip-bg'], v['--dbm-id-chip-text'])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(v['--dbm-id-chip-hover-bg'], v['--dbm-id-chip-hover-text'])).toBeGreaterThanOrEqual(4.5);
  });

  test('the dark chip is not the light chip', () => {
    expect(chip('dark')['--dbm-id-chip-bg']).not.toBe(chip('light')['--dbm-id-chip-bg']);
  });

  test('the chip rule reads its colours from the theme variables, with no hard-coded fill', () => {
    const rule = css.slice(css.indexOf('.dbm-id-chip {'), css.indexOf('}', css.indexOf('.dbm-id-chip {')));
    expect(rule).toContain('var(--dbm-id-chip-bg)');
    expect(rule).toContain('var(--dbm-id-chip-text)');
  });
});

test('the page uses the component, with no inert Link left in the tab', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'pages', 'AnalysisResults.js'), 'utf8');
  expect(src).toContain('<TradeOffDecisions');
  expect(src).not.toMatch(/onFollow=\{\(\) => \{\}\}/);
  expect(src).not.toContain('QUERY_CHIP_BG');
});

const toggle = (el) => act(() => { headerOf(el).dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', keyCode: 13, bubbles: true })); });
const SRC = (...parts) => fs.readFileSync(path.join(__dirname, '..', '..', ...parts), 'utf8');

describe('a decision keeps its own open state while the filter changes (review item 1)', () => {
  test('open one, filter to another: the other is closed; clear the filter: the first is still open and no other opened', () => {
    render();
    toggle(decisionEls()[0]);
    expect(headerOf(decisionEls()[0]).getAttribute('aria-expanded')).toBe('true');
    type('reviews');
    expect(decisionEls()).toHaveLength(1);
    expect(decisionEls()[0].textContent).toContain('Reviews get their own table.');
    expect(headerOf(decisionEls()[0]).getAttribute('aria-expanded')).toBe('false');
    type('');
    const states = decisionEls().map((e) => headerOf(e).getAttribute('aria-expanded'));
    expect(states).toEqual(['true', 'false', 'false']);
  });

  test('the same holds with topic groups', () => {
    render({ groups: GROUPS });
    toggle(decisionEls()[0]);
    type('reviews');
    expect(headerOf(decisionEls()[0]).getAttribute('aria-expanded')).toBe('false');
    type('');
    expect(decisionEls().map((e) => headerOf(e).getAttribute('aria-expanded'))).toEqual(['true', 'false', 'false']);
  });
});

describe('the header (review item 2)', () => {
  test('is the decision text in an h4 heading and the page does not use the deprecated header prop', () => {
    render({ groups: [{ topic: 'keys', label: 'Key design', count: 1, tradeOffs: [DECISIONS[0]] }] });
    expect(container.querySelector('h4').textContent).toBe('Orders live in one item collection.');
    expect(SRC('components', 'TradeOffDecision.js')).not.toMatch(/\n\s+header=/);
  });
});

describe('typing stays cheap (review item 3)', () => {
  test('a decision is memoised and the filter text is deferred', () => {
    expect(TradeOffDecision.$$typeof).toBe(Symbol.for('react.memo'));
    expect(SRC('components', 'TradeOffDecisions.js')).toContain('useDeferredValue(typed)');
  });
});

describe('the renderer-only guard and the short id (review items 4 and 5)', () => {
  test('both components are in GUARDED_UI_FILES', () => {
    const guard = SRC('utils', '__tests__', 'rendererOnlyFormat.test.js');
    expect(guard).toContain("'components/TradeOffDecisions.js'");
    expect(guard).toContain("'components/TradeOffDecision.js'");
  });

  test('the chip text is formatShortId and no new length is defined', () => {
    render({ decisions: [DECISIONS[0]] });
    expect(container.querySelector('button.dbm-id-chip').textContent).toBe(formatShortId(ID_A));
    expect(SRC('components', 'TradeOffDecision.js')).not.toContain('SHORT_ID_LENGTH');
  });
});

describe('a match inside the details shows the details (review item 6)', () => {
  const state = (i) => headerOf(decisionEls()[i]).getAttribute('aria-expanded');

  test.each([
    ['impact', 'writes stay'],
    ['query id', ID_B.slice(10, 30)],
    ['table', 'sessions_tbl'],
  ])('a match in the %s opens the decision', (_, text) => {
    render({ decisions: [...DECISIONS.slice(0, 2), { description: 'Reads are cached.', impact: '', source_tables: ['sessions_tbl'], target_tables: [], query_ids: [] }] });
    type(text);
    expect(decisionEls()).toHaveLength(1);
    expect(state(0)).toBe('true');
  });

  test('a match in the title alone leaves it closed, and the reader can still close an opened one', () => {
    render({ decisions: DECISIONS.slice(0, 2) });
    type('orders live');
    expect(state(0)).toBe('false');
    type('one read');
    expect(state(0)).toBe('true');
    toggle(decisionEls()[0]);
    expect(state(0)).toBe('false');
  });

  test('clearing the filter closes what the filter opened', () => {
    render({ decisions: DECISIONS.slice(0, 2) });
    type('writes stay');
    type('');
    expect(decisionEls().map((e) => headerOf(e).getAttribute('aria-expanded'))).toEqual(['false', 'false']);
  });
});

describe('the decision heading follows its context (axe heading-order)', () => {
  const topicHeadings = () => [...container.querySelectorAll('h3')].filter((h) => !h.closest('[data-trade-off-decision]'));
  const levels = () => [...container.querySelectorAll('[data-trade-off-decision] h1, [data-trade-off-decision] h2, [data-trade-off-decision] h3, [data-trade-off-decision] h4')].map((h) => h.tagName);

  test('under a topic heading (an h3) a decision is an h4', () => {
    render({ groups: GROUPS.slice(0, 1) });
    expect(topicHeadings().map((h) => h.textContent)).toEqual(['Key design (2)']);
    expect(levels()).toEqual(['H4', 'H4']);
  });

  test('with no topic heading (one "other" group, or a flat list) the section heading is the h2 above, so a decision is an h3', () => {
    render({ groups: [{ topic: 'other', label: 'Other', count: 3, tradeOffs: DECISIONS }] });
    expect(topicHeadings()).toHaveLength(0);
    expect(levels()).toEqual(['H3', 'H3', 'H3']);
    render({ groups: null });
    expect(levels()).toEqual(['H3', 'H3', 'H3']);
  });

  test('filtering a titled list down to one group keeps the topic heading and the h4', () => {
    render({ groups: GROUPS });
    type('sessions');
    expect(topicHeadings().map((h) => h.textContent)).toEqual(['Other']);
    expect(levels()).toEqual(['H4']);
  });
});
