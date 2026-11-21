// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * utils/theme.js owns the theme (#2490). It is the only file that names the saved
 * choice's key ('dbm-theme', or THEME_STORAGE_KEY) and the only one that calls
 * Cloudscape's applyMode. Every other file asks theme.js: `isDarkMode()` for the mode
 * the page applied, `onThemeChange` to hear about a change, `applyThemeChoice` to make
 * one. A component that read the stored string would draw dark on a light page while
 * the choice is System, since System saves nothing and the OS decides.
 *
 * The guard parses every non-test UI source file (@babel/parser, as
 * noHandFormatting.test.js does), so comments never count and any spelling of an access
 * does: a string or template literal holding the key, the THEME_STORAGE_KEY name, and
 * applyMode as a name, a property or a computed key. The self-tests below feed it known
 * bad and known good sources.
 */

const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');
const t = require('@babel/types');

const UI_SRC = path.resolve(__dirname, '..', '..');
const OWNER = 'utils/theme.js';

// The key as a whole word: 'dbm-theme-change' (the event) and 'dbm-theme-vars' (the
// token stylesheet's id) are other names.
const KEY = /(?<![\w-])dbm-theme(?![\w-])/;

function children(node) {
  const keys = t.VISITOR_KEYS[node.type] || [];
  const out = [];
  keys.forEach((k) => {
    const v = node[k];
    if (Array.isArray(v)) v.forEach((c) => c && out.push(c));
    else if (v) out.push(v);
  });
  return out;
}

function* walk(node) {
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    stack.push(...children(n));
  }
}

/** The theme-ownership rules `code` breaks, one entry per hit, as "rule at line". */
function themeOffences(code) {
  const ast = parse(code, { sourceType: 'unambiguous', plugins: ['jsx', 'optionalChaining', 'classProperties'] });
  const hits = [];
  const hit = (rule, node) => hits.push(`${rule} at ${node.loc.start.line}`);
  for (const node of walk(ast.program)) {
    if (t.isStringLiteral(node) || t.isDirectiveLiteral(node)) {
      if (KEY.test(node.value)) hit('saved-theme key', node);
      if (node.value === 'applyMode') hit('applyMode', node);
    } else if (t.isTemplateElement(node)) {
      if (KEY.test(node.value.cooked ?? node.value.raw)) hit('saved-theme key', node);
    } else if (t.isJSXText(node)) {
      if (KEY.test(node.value)) hit('saved-theme key', node);
    } else if (t.isIdentifier(node) || t.isJSXIdentifier(node)) {
      if (node.name === 'THEME_STORAGE_KEY') hit('THEME_STORAGE_KEY', node);
      if (node.name === 'applyMode') hit('applyMode', node);
    }
  }
  return hits;
}

function sources(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === '__tests__' ? [] : sources(full);
    return /\.jsx?$/.test(entry.name) ? [full] : [];
  });
}

const rel = (file) => path.relative(UI_SRC, file).split(path.sep).join('/');

test('only utils/theme.js names the saved theme or calls applyMode', () => {
  const offenders = {};
  sources(UI_SRC).forEach((file) => {
    if (rel(file) === OWNER) return;
    const hits = themeOffences(fs.readFileSync(file, 'utf8'));
    if (hits.length) offenders[rel(file)] = hits;
  });
  expect(offenders).toEqual({});
});

test('utils/theme.js is where the key and applyMode live, so the scan sees them there', () => {
  const hits = themeOffences(fs.readFileSync(path.join(UI_SRC, OWNER), 'utf8'));
  expect(hits.some((h) => h.startsWith('saved-theme key'))).toBe(true);
  expect(hits.some((h) => h.startsWith('applyMode'))).toBe(true);
});

describe('the rule flags each way of reaching the saved theme', () => {
  test.each([
    ['a getItem call', "localStorage.getItem('dbm-theme');", 'saved-theme key'],
    ['window.localStorage with the exported name', 'window.localStorage.getItem(THEME_STORAGE_KEY);', 'THEME_STORAGE_KEY'],
    ['an import of the exported name', "import { THEME_STORAGE_KEY } from '../utils/theme';", 'THEME_STORAGE_KEY'],
    ['a bracket read', "const v = localStorage['dbm-theme'];", 'saved-theme key'],
    ['an alias of localStorage', 'const s = window.localStorage; s.getItem("dbm-theme");', 'saved-theme key'],
    ['a backtick string', 'localStorage.setItem(`dbm-theme`, "dark");', 'saved-theme key'],
    ['a template with a value', 'const k = `dbm-theme`; localStorage.removeItem(`${k}`);', 'saved-theme key'],
    ['a key held in a constant', "const KEY = 'dbm-theme'; sessionStorage.getItem(KEY);", 'saved-theme key'],
    ['an applyMode import', "import { applyMode } from '@cloudscape-design/global-styles';", 'applyMode'],
    ['an applyMode on a namespace', "import * as gs from '@cloudscape-design/global-styles'; gs.applyMode('dark');", 'applyMode'],
    ['a computed applyMode', "gs['applyMode']('dark');", 'applyMode'],
  ])('%s', (_label, code, rule) => {
    expect(themeOffences(code).some((h) => h.startsWith(rule))).toBe(true);
  });
});

describe('the rule leaves other names and comments alone', () => {
  test.each([
    ['the theme change event', "window.addEventListener('dbm-theme-change', f);"],
    ['the token stylesheet id', "document.getElementById('dbm-theme-vars');"],
    ['a line comment', "// reads localStorage 'dbm-theme' and calls applyMode\nconst x = 1;"],
    ['a block comment', "/* THEME_STORAGE_KEY, applyMode, 'dbm-theme' */ const y = 2;"],
    ['the theme helpers', "import { isDarkMode, onThemeChange } from '../utils/theme'; isDarkMode();"],
  ])('%s', (_label, code) => {
    expect(themeOffences(code)).toEqual([]);
  });
});
