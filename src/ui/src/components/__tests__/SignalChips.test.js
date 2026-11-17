// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2312: the signal chips of the per-query review, rendered with the real Cloudscape Badge.
 * Main drew them as a span with the engine colour at 12% opacity behind the same colour at
 * 11 px (3.96:1). This reads the colours each rendered chip carries and checks the pair
 * meets WCAG AA for small text (4.5:1). A filled badge does not depend on the page theme,
 * so the pair is read in both mode classes.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import SignalChips from '../SignalChips';
import EngineBadge from '../EngineBadge';
import { ENGINE_HEX } from '../../utils/engineCatalog.generated';
import { DEFAULT_ENGINE_HEX, contrastRatio } from '../../utils/engineColors';

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

const toHex = (rgb) => '#' + (/rgb\((\d+), (\d+), (\d+)\)/.exec(rgb) || []).slice(1).map((n) => Number(n).toString(16).padStart(2, '0')).join('');
// Each rendered chip with the fill and text colour its own style carries.
const chipsOf = (node) => [...node.querySelectorAll('[class*="awsui_badge_"]')].map((chip) => ({
  chip,
  fill: toHex(chip.style.background),
  text: toHex(chip.style.color),
}));

const SIGNALS = [...Object.keys(ENGINE_HEX).map((engine) => ({ engine, signal: 'point_lookup' })), { signal: 'no_engine' }];

test('every signal is one Badge, named in words, not underscores', () => {
  act(() => root.render(<SignalChips signals={SIGNALS} />));
  expect(chipsOf(container)).toHaveLength(SIGNALS.length);
  expect(container.textContent).toContain('point lookup');
  expect(container.textContent).not.toContain('_');
});

// The pair a chip carries is its own inline colours, so the page theme cannot change it;
// each catalog engine and the no-engine grey are read in both modes to show that.
test.each(['awsui-light-mode', 'awsui-dark-mode'])('in %s each chip reads 4.5:1 on its engine fill', (modeClass) => {
  container.className = modeClass;
  act(() => root.render(<SignalChips signals={SIGNALS} />));
  const chips = chipsOf(container);
  expect(chips).toHaveLength(SIGNALS.length);
  chips.forEach(({ fill, text }, i) => {
    expect(fill).toBe((SIGNALS[i].engine ? ENGINE_HEX[SIGNALS[i].engine] : DEFAULT_ENGINE_HEX).toLowerCase());
    expect({ fill, text, ok: contrastRatio(fill, text) >= 4.5 }).toEqual({ fill, text, ok: true });
  });
});

test('the chip is not the old translucent span', () => {
  act(() => root.render(<SignalChips signals={[{ engine: 'dynamodb', signal: 'hot_key' }]} />));
  expect(container.querySelector('.signal-badge')).toBeNull();
  expect(container.querySelector('[data-engine-badge]')).not.toBeNull();
});

// Every place that names an engine (cost cards, roadmap, the gate's patterns and modal)
// uses a bare EngineBadge with the engine's display name.
test.each(Object.keys(ENGINE_HEX))('EngineBadge for %s reads 4.5:1', (engine) => {
  act(() => root.render(<EngineBadge engine={engine} />));
  const [{ fill, text }] = chipsOf(container);
  expect(fill).toBe(ENGINE_HEX[engine].toLowerCase());
  expect(contrastRatio(fill, text)).toBeGreaterThanOrEqual(4.5);
});
