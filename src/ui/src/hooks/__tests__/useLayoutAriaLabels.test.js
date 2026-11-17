// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2312: every page names the AppLayout's navigation and tools panels and their
 * toggle buttons from one shared hook. An AppLayout without `ariaLabels` renders
 * toggle buttons with no accessible name (axe `button-name`, critical). The first
 * test renders the real Cloudscape layout with the hook's labels; the second reads
 * every source file so a new page cannot skip them.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AppLayoutToolbar from '@cloudscape-design/components/app-layout';
import useLayoutAriaLabels from '../useLayoutAriaLabels';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key) => {
      const text = jest.requireActual('../../locales/en.json')[key];
      return text === undefined ? key : text;
    },
  }),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');

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

function Page({ withLabels }) {
  const labels = useLayoutAriaLabels();
  return (
    <AppLayoutToolbar
      ariaLabels={withLabels ? labels : undefined}
      navigation={<nav>links</nav>}
      tools={<div>on this page</div>}
      content={<p>content</p>}
    />
  );
}

const accessibleName = (button) => (button.getAttribute('aria-label') || button.textContent || '').trim();
const unnamedButtons = () => [...container.querySelectorAll('button')].filter((b) => accessibleName(b) === '');

test('the hook names the navigation and tools panels and each toggle', () => {
  act(() => root.render(<Page withLabels />));
  const names = [...container.querySelectorAll('button')].map(accessibleName);
  expect(names).toEqual(expect.arrayContaining(['Open side navigation', 'Open On this page']));
  expect(unnamedButtons()).toHaveLength(0);
});

test('without the labels the real layout has unnamed toggles (the premise of the fix)', () => {
  act(() => root.render(<Page withLabels={false} />));
  expect(unnamedButtons().length).toBeGreaterThan(0);
});

const SRC = path.join(__dirname, '..', '..');
const sourceFiles = () => fs.readdirSync(SRC, { recursive: true })
  .filter((rel) => /\.jsx?$/.test(rel) && !rel.split(path.sep).includes('__tests__'))
  .map((rel) => `${SRC}/${rel}`);

function layoutElements(code) {
  const ast = parse(code, { sourceType: 'module', plugins: ['jsx'] });
  const found = [];
  const names = new Set();
  const walk = (n) => {
    if (!n || typeof n.type !== 'string') return;
    if (n.type === 'ImportDeclaration' && n.source.value === '@cloudscape-design/components/app-layout') {
      n.specifiers.forEach((s) => names.add(s.local.name));
    }
    if (n.type === 'JSXOpeningElement' && n.name.type === 'JSXIdentifier') found.push(n);
    Object.keys(n).forEach((k) => {
      const v = n[k];
      if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v === 'object') walk(v);
    });
  };
  walk(ast.program);
  return found.filter((el) => names.has(el.name.name));
}

test('every AppLayout in the UI passes the shared labels', () => {
  const layouts = [];
  sourceFiles().forEach((file) => {
    layoutElements(fs.readFileSync(file, 'utf8')).forEach((el) => layouts.push({ file, el }));
  });
  expect(layouts.length).toBeGreaterThanOrEqual(8);
  layouts.forEach(({ file, el }) => {
    const attr = el.attributes.find((a) => a.type === 'JSXAttribute' && a.name.name === 'ariaLabels');
    expect({ file: path.relative(SRC, file), passes: !!attr }).toEqual({ file: path.relative(SRC, file), passes: true });
  });
});
