// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2310: Cloudscape components forward only id, className and data-* to the
 * DOM, so a `style` prop on them is dropped. The cards and code blocks that
 * need a border, a background or a scroll box live in wrapper elements with
 * class names, and the real Cloudscape Box renders inside them.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import Box from '@cloudscape-design/components/box';
import { CardBox, CodeBlock } from '../StyledBlocks';

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

test('real Cloudscape Box drops style but keeps className (the premise of the fix)', () => {
  act(() => root.render(<Box style={{ backgroundColor: '#fff' }} className="kept">x</Box>));
  const el = container.querySelector('.kept');
  expect(el).not.toBeNull();
  expect(el.getAttribute('style')).toBeNull();
});

test('CardBox renders a card element carrying its tone as a class and the content', () => {
  act(() => root.render(<CardBox tone="warning"><span data-testid="inner">hello</span></CardBox>));
  const card = container.querySelector('.dbm-card');
  expect(card).not.toBeNull();
  expect(card.classList.contains('dbm-card--warning')).toBe(true);
  expect(card.textContent).toBe('hello');
  expect(card.querySelector('[data-testid=inner]')).not.toBeNull();
});

test('CardBox defaults to the info tone', () => {
  act(() => root.render(<CardBox>text</CardBox>));
  expect(container.querySelector('.dbm-card').classList.contains('dbm-card--info')).toBe(true);
});

test.each(['error', 'warning', 'success', 'stopped', 'info', 'danger', 'note'])('CardBox tone %s sets the matching class', (tone) => {
  act(() => root.render(<CardBox tone={tone}>t</CardBox>));
  expect(container.querySelector('.dbm-card').classList.contains(`dbm-card--${tone}`)).toBe(true);
});

test('CodeBlock keeps the text verbatim in a monospace code block', () => {
  const sql = 'SELECT *\n  FROM t -- <b>not html</b>';
  act(() => root.render(<CodeBlock>{sql}</CodeBlock>));
  const block = container.querySelector('.dbm-code-block');
  expect(block).not.toBeNull();
  expect(block.textContent).toBe(sql);
  expect(block.querySelector('b')).toBeNull();
});

test('a capped CodeBlock is a labelled, focusable region; an uncapped one is not', () => {
  act(() => root.render(<CodeBlock capped label="Query text">x</CodeBlock>));
  const el = container.querySelector('.dbm-code-block');
  expect(el.getAttribute('tabindex')).toBe('0');
  expect(el.getAttribute('role')).toBe('region');
  expect(el.getAttribute('aria-label')).toBe('Query text');
  act(() => root.render(<CodeBlock>x</CodeBlock>));
  const plain = container.querySelector('.dbm-code-block');
  expect(plain.getAttribute('tabindex')).toBeNull();
  expect(plain.getAttribute('role')).toBeNull();
});

test('a capped CodeBlock without a label has no built-in English name and no unnamed region', () => {
  act(() => root.render(<CodeBlock capped>x</CodeBlock>));
  const el = container.querySelector('.dbm-code-block');
  expect(el.getAttribute('aria-label')).toBeNull();
  expect(el.getAttribute('role')).toBeNull();
  expect(el.getAttribute('tabindex')).toBeNull();
  expect(container.innerHTML).not.toContain('Query text');
});

test('CodeBlock can cap its height', () => {
  act(() => root.render(<CodeBlock capped>x</CodeBlock>));
  expect(container.querySelector('.dbm-code-block').classList.contains('dbm-code-block--capped')).toBe(true);
  act(() => root.render(<CodeBlock>x</CodeBlock>));
  expect(container.querySelector('.dbm-code-block').classList.contains('dbm-code-block--capped')).toBe(false);
});

describe('the stylesheet', () => {
  const css = require('fs').readFileSync(require('path').join(__dirname, '..', '..', 'styles', 'global.css'), 'utf8');

  test.each(['info', 'note', 'danger', 'error', 'warning', 'success', 'stopped'])('has a rule for the %s card', (tone) => {
    const rule = tone === 'info' ? '.dbm-card {' : `.dbm-card--${tone}`;
    expect(css).toContain(rule);
  });

  test('has the code block, capped and fill rules', () => {
    expect(css).toContain('.dbm-code-block {');
    expect(css).toContain('.dbm-code-block--capped {');
    expect(css).toContain('.dbm-fill {');
  });

  const block = (from) => css.slice(from, css.indexOf('}', from));
  const rootStart = css.indexOf(':root {', css.indexOf('Cards and code blocks'));
  const darkStart = css.indexOf('.awsui-dark-mode {', rootStart);
  const names = (b) => [...b.matchAll(/(--dbm-(?:card|code)-[a-z-]+):/g)].map((m) => m[1]);

  test('sets every card and code variable in the dark theme as well as the light one', () => {
    const light = names(block(rootStart));
    expect(light).toHaveLength(13);
    expect(names(block(darkStart)).sort()).toEqual(light.sort());
  });

  test('the severity borders keep 3:1 against the card background in both themes', () => {
    const { contrastRatio } = require('../../utils/engineColors');
    const val = (b, v) => {
      const line = b.split('\n').find((l) => l.trim().startsWith(`${v}:`));
      return /#[0-9a-f]{6}/i.exec(line)[0];
    };
    const light = block(rootStart);
    for (const tone of ['error', 'warning', 'success', 'stopped']) {
      expect(contrastRatio(val(light, `--dbm-card-${tone}-border`), val(light, '--dbm-card-neutral-bg'))).toBeGreaterThanOrEqual(3);
    }
    // The dark card background is a faint white over the page's #0f1b2a.
    const dark = block(darkStart);
    for (const tone of ['error', 'warning', 'success', 'stopped']) {
      expect(contrastRatio(val(dark, `--dbm-card-${tone}-border`), '#0f1b2a')).toBeGreaterThanOrEqual(3);
    }
  });
});
