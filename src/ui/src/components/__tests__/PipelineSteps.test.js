// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2317: the phases of a running job are Cloudscape Steps (a list with a status icon and
 * a name per phase), not hand-built arrows that collide on a phone. Renders the real
 * Cloudscape component and the real English strings.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import '../../i18n';
import PipelineSteps from '../PipelineSteps';

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

const render = (props) => act(() => root.render(<PipelineSteps {...props} />));
const items = () => Array.from(container.querySelectorAll('li'));

test('lists the five phases by name, spelled "Analyze"', () => {
  render({ current: 'analyze' });
  expect(items().map((li) => li.textContent)).toEqual([
    expect.stringContaining('Collect'),
    expect.stringContaining('Analyze'),
    expect.stringContaining('Select'),
    expect.stringContaining('Design'),
    expect.stringContaining('Review'),
  ]);
  expect(container.textContent).not.toMatch(/Analyse/);
});

test('names the list and gives each phase a status a screen reader can read', () => {
  render({ current: 'select' });
  expect(container.querySelector('[aria-label="Assessment phases"]')).not.toBeNull();
  const labels = items().map((li) => li.querySelector('[role="img"]')?.getAttribute('aria-label'));
  expect(labels).toEqual(['Done', 'Done', 'In progress', 'Not started', 'Not started']);
});

test('says which phases wait for a person', () => {
  render({ current: 'collect' });
  const waiting = items().filter((li) => li.textContent.includes('Waits for your input'));
  expect(waiting.map((li) => li.textContent)).toEqual([
    expect.stringContaining('Select'),
    expect.stringContaining('Review'),
  ]);
});

test('a finished job shows every phase done', () => {
  render({ current: 'review', complete: true });
  expect(items().map((li) => li.querySelector('[role="img"]')?.getAttribute('aria-label')))
    .toEqual(Array(5).fill('Done'));
});
