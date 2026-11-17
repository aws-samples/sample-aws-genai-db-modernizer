// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2320: the explorer's "By source table" Destination tables cell lists each destination
 * once, in first-seen order. It shows the first three and puts the rest behind a "+N more"
 * Popover, so the cell never grows into one very wide line. Names are data: HTML-like text
 * renders as text. Runs against the real Cloudscape Box and Popover.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import DestinationTables, { uniqueDestinations, VISIBLE_DESTINATIONS } from '../DestinationTables';
import { displayEngine } from '../../utils/engineNames';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => (opts && opts.count !== undefined ? `${key}|${opts.count}` : key),
  }),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const on = (engine, names) => names.map((name) => ({ engine, name }));
const render = (names, engine = 'dynamodb') => {
  const entries = names.map((n) => (typeof n === 'string' ? { engine, name: n } : n));
  act(() => root.render(<DestinationTables destinations={entries} />));
};
const shown = () => [...container.querySelectorAll('[data-testid="dest-name"]')].map((n) => n.textContent);
const moreTrigger = () => container.querySelector('[data-testid="dest-more"]');
const popoverNames = () =>
  [...document.querySelectorAll('[data-testid="dest-popover-name"]')].map((n) => n.textContent);

describe('uniqueDestinations', () => {
  test('keeps first-seen order and drops repeats', () => {
    expect(uniqueDestinations(on('dynamodb', ['topics', 'posts', 'topics', 'users', 'posts'])).map((d) => d.name)).toEqual([
      'topics', 'posts', 'users',
    ]);
  });
  test('drops blanks and the no-destination dash', () => {
    expect(uniqueDestinations(on('dynamodb', ['', '—', null, undefined, 'a'])).map((d) => d.name)).toEqual(['a']);
  });
  test('trims before comparing', () => {
    expect(uniqueDestinations(on('dynamodb', [' a', 'a ', 'a'])).map((d) => d.name)).toEqual(['a']);
  });
  test('a missing list is empty', () => {
    expect(uniqueDestinations(undefined)).toEqual([]);
  });
  test('the same name on two engines is two destinations', () => {
    expect(uniqueDestinations([
      { engine: 'dynamodb', name: 'topics' },
      { engine: 'aurora_postgresql', name: 'topics' },
      { engine: 'dynamodb', name: 'topics' },
    ])).toEqual([
      { engine: 'dynamodb', name: 'topics' },
      { engine: 'aurora_postgresql', name: 'topics' },
    ]);
  });
});

describe('DestinationTables cell', () => {
  test('shows the first three', () => {
    expect(VISIBLE_DESTINATIONS).toBe(3);
  });

  test('zero destinations show a dash and no more link', () => {
    render([]);
    expect(container.textContent).toBe('—');
    expect(moreTrigger()).toBeNull();
  });

  test('one destination shows it alone', () => {
    render(['topics']);
    expect(shown()).toEqual(['topics']);
    expect(moreTrigger()).toBeNull();
  });

  test('three destinations show all three and no more link', () => {
    render(['a', 'b', 'c']);
    expect(shown()).toEqual(['a', 'b', 'c']);
    expect(moreTrigger()).toBeNull();
  });

  test('four destinations show three and a +1 more popover holding the fourth', () => {
    render(['a', 'b', 'c', 'd']);
    expect(shown()).toEqual(['a', 'b', 'c']);
    const trigger = moreTrigger();
    expect(trigger.textContent).toContain('analysis-results.explorer.destinations-more|1');
    act(() => trigger.querySelector('button').click());
    expect(popoverNames()).toEqual(['d']);
  });

  test('many destinations list the rest, in order, in the popover', () => {
    const names = Array.from({ length: 40 }, (_, i) => `t${i}`);
    render(names);
    expect(shown()).toEqual(['t0', 't1', 't2']);
    expect(moreTrigger().textContent).toContain('|37');
    act(() => moreTrigger().querySelector('button').click());
    expect(popoverNames()).toEqual(names.slice(3));
  });

  test('duplicates appear once and count once', () => {
    render(['topics', 'posts', 'topics', 'users', 'posts', 'tags', 'topics']);
    expect(shown()).toEqual(['topics', 'posts', 'users']);
    expect(moreTrigger().textContent).toContain('|1');
  });

  test('only duplicates of one name show one name', () => {
    render(['topics', 'topics', 'topics']);
    expect(shown()).toEqual(['topics']);
    expect(moreTrigger()).toBeNull();
  });

  test('the cell wraps instead of staying on one line', () => {
    render(['a', 'b']);
    const cell = container.firstElementChild;
    expect(window.getComputedStyle(cell).whiteSpace).toBe('normal');
    expect(cell.querySelectorAll('[data-testid="dest-name"]').length).toBe(2);
  });

  test('the same name on two engines lists both, each labelled with its engine', () => {
    render([
      { engine: 'dynamodb', name: 'topics' },
      { engine: 'aurora_postgresql', name: 'topics' },
      { engine: 'dynamodb', name: 'posts' },
    ]);
    expect(shown()).toEqual([
      `topics (${displayEngine('dynamodb')})`,
      `topics (${displayEngine('aurora_postgresql')})`,
      'posts',
    ]);
  });

  test('a name on one engine only carries no engine label', () => {
    render(['topics', 'posts']);
    expect(shown()).toEqual(['topics', 'posts']);
  });

  test('the +N more trigger names what it opens', () => {
    render(Array.from({ length: 48 }, (_, i) => `t${i}`));
    expect(moreTrigger().querySelector('button').getAttribute('aria-label')).toBe(
      'analysis-results.explorer.destinations-more-aria|45'
    );
  });

  test('HTML-like names render as text', () => {
    render(['<img src=x onerror=alert(1)>', '<b>bold</b>', 'a&amp;b', 'x', '<script>1</script>']);
    expect(shown()).toEqual(['<img src=x onerror=alert(1)>', '<b>bold</b>', 'a&amp;b']);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    act(() => moreTrigger().querySelector('button').click());
    expect(popoverNames()).toEqual(['x', '<script>1</script>']);
    expect(document.querySelector('script')).toBeNull();
  });
});
