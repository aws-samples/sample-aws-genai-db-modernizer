// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2490: the graph explorer's canvas draws for the mode the page applied, not the
 * stored theme string. On System nothing is stored and the OS decides, so the old
 * read (`localStorage 'dbm-theme' || 'dark'`) drew dark labels on a light page. The
 * canvas also redraws when the theme changes. Cytoscape is replaced by a recording
 * stub; the canvas never renders in jsdom.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { GRAPH_CANVAS } from '../../styles/palette';

const instances = [];

jest.mock('cytoscape-cola', () => ({}));
jest.mock('cytoscape', () => {
  const empty = () => {
    const c = [];
    Object.assign(c, {
      addClass: () => c,
      removeClass: () => c,
      unselect: () => c,
      unlock: () => c,
      filter: () => empty(),
      empty: () => true,
      nonempty: () => false,
    });
    return c;
  };
  // Plain functions: react-scripts resets jest.fn implementations before each test.
  const cytoscape = (options) => {
    const scratch = {};
    const styles = [];
    const instance = {
      options,
      styles,
      style: (sheet) => styles.push(sheet),
      on: () => {},
      scratch: (key, value) => {
        if (value !== undefined) scratch[key] = value;
        return scratch[key];
      },
      container: () => options.container,
      batch: (fn) => fn(),
      elements: () => empty(),
      nodes: () => empty(),
      edges: () => empty(),
      collection: () => empty(),
      getElementById: () => empty(),
      resize: () => {},
      destroy: () => {},
    };
    instances.push(instance);
    return instance;
  };
  cytoscape.use = () => {};
  return cytoscape;
});

// Imported after the mocks.
const GraphExplorerCanvas = require('../GraphExplorerCanvas').default;
const { applyThemeChoice } = require('../../utils/theme');

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  instances.length = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  localStorage.clear();
  document.body.classList.remove('awsui-dark-mode');
});

const labelColour = (style) => style.find((rule) => rule.selector === 'node').style.color;

function mount() {
  act(() => {
    root.render(<GraphExplorerCanvas elements={[]} />);
  });
  expect(instances).toHaveLength(1);
  return instances[0];
}

test('on a light page with nothing saved, the canvas draws light-theme labels', () => {
  const instance = mount();
  expect(labelColour(instance.options.style)).toBe(GRAPH_CANVAS.light.text);
});

test('on a dark page, the canvas draws dark-theme labels', () => {
  document.body.classList.add('awsui-dark-mode');
  const instance = mount();
  expect(labelColour(instance.options.style)).toBe(GRAPH_CANVAS.dark.text);
});

test('a theme change redraws the canvas for the new mode, and stops once it unmounts', () => {
  const instance = mount();
  act(() => applyThemeChoice('dark'));
  expect(instance.styles).toHaveLength(1);
  expect(labelColour(instance.styles[0])).toBe(GRAPH_CANVAS.dark.text);
  act(() => applyThemeChoice('light'));
  expect(labelColour(instance.styles[1])).toBe(GRAPH_CANVAS.light.text);
  act(() => root.unmount());
  root = createRoot(container);
  applyThemeChoice('dark');
  expect(instance.styles).toHaveLength(2);
});
