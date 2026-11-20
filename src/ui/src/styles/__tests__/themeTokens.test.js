// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2311: the app's --dbm-* colours are Cloudscape design tokens, so the light and dark
 * themes both apply. The token values are read from the installed components' theme
 * stylesheet (testUtils/themeTokenValues.js), so these ratios are the ones a reader sees.
 * The browser checks (axe, both themes, 1440 and 390 px) are in tests/e2e/test_ui.py.
 */
import fs from 'fs';
import path from 'path';
import { THEME_VARS, installThemeVars, themeVarsCss } from '../themeTokens';
import { contrastRatio } from '../../utils/engineColors';

const { tokenValues } = require('../../testUtils/themeTokenValues');

const SRC = path.resolve(__dirname, '../..');
const THEMES = ['light', 'dark'];
const value = (name, theme) => tokenValues(THEME_VARS[name])[theme];
const ratio = (fg, bg, theme) => contrastRatio(value(fg, theme), value(bg, theme));

const sourceFiles = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
  // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
  const full = path.join(dir, entry.name);
  if (entry.isDirectory()) return entry.name === '__tests__' ? [] : sourceFiles(full);
  return /\.(js|css)$/.test(entry.name) ? [full] : [];
});

test('every property is a token the installed Cloudscape components define', () => {
  // A design-tokens release out of step with the components package renames the hashed
  // variables; the browser then shows the light fallback in the dark theme too.
  Object.entries(THEME_VARS).forEach(([name, token]) => {
    expect([name, token.startsWith('var(--'), tokenValues(token).defined]).toEqual([name, true, true]);
  });
});

test('every --dbm-* property the CSS and the components read is declared', () => {
  const used = new Set(sourceFiles(SRC).flatMap((file) => [
    ...fs.readFileSync(file, 'utf8').matchAll(/var\((--dbm-[a-z0-9-]+)\)/g),
  ].map((m) => m[1])));
  expect(used.size).toBeGreaterThan(20);
  expect([...used].filter((name) => !(name in THEME_VARS))).toEqual([]);
});

test('no stylesheet sets a --dbm-* property of its own', () => {
  sourceFiles(SRC).filter((file) => file.endsWith('.css')).forEach((file) => {
    const text = fs.readFileSync(file, 'utf8');
    expect([path.relative(SRC, file), /--dbm-[a-z0-9-]+\s*:/.test(text)]).toEqual([path.relative(SRC, file), false]);
  });
});

test('the properties are declared on body, where the theme class is', () => {
  const css = themeVarsCss();
  expect(css.startsWith('body {\n')).toBe(true);
  expect(css).toContain(`  --dbm-text-primary: ${THEME_VARS['--dbm-text-primary']};`);
  expect(css.trim().endsWith('}')).toBe(true);
});

test('installThemeVars writes one style element, however often it runs', () => {
  installThemeVars(document);
  installThemeVars(document);
  const styles = document.head.querySelectorAll('style#dbm-theme-vars');
  expect(styles).toHaveLength(1);
  expect(styles[0].textContent).toBe(themeVarsCss());
});

test('the entry point installs the properties before it renders', () => {
  const entry = fs.readFileSync(path.join(SRC, 'index.js'), 'utf8');
  expect(entry).toMatch(/installThemeVars\(\);[\s\S]*render\(/);
});

describe.each(THEMES)('in the %s theme', (theme) => {
  test('body, secondary and muted text read 4.5:1 on the page and on a surface', () => {
    ['--dbm-text-primary', '--dbm-text-secondary', '--dbm-text-muted', '--dbm-text-link'].forEach((fg) => {
      ['--dbm-page-bg', '--dbm-bg-surface', '--dbm-card-neutral-bg'].forEach((bg) => {
        expect([fg, bg, ratio(fg, bg, theme) >= 4.5]).toEqual([fg, bg, true]);
      });
    });
  });

  test('the moved badge (AssignmentGate.css .diff-badge) reads 4.5:1', () => {
    // #2498 gave the dark theme its own orange; the light theme's orange on its tint
    // read 2.7:1. Both themes now use the warning token pair.
    expect(ratio('--dbm-moved-text', '--dbm-moved-bg', theme)).toBeGreaterThanOrEqual(4.5);
    // The badge sits on a row, an expanded (selected) row, or the page.
    ['--dbm-page-bg', '--dbm-bg-highlight', '--dbm-bg-surface'].forEach((bg) => {
      expect([bg, ratio('--dbm-moved-text', bg, theme) >= 4.5]).toEqual([bg, true]);
    });
  });

  test('the query id chip reads 4.5:1 at rest and on hover', () => {
    expect(ratio('--dbm-id-chip-text', '--dbm-id-chip-bg', theme)).toBeGreaterThanOrEqual(4.5);
    expect(ratio('--dbm-id-chip-hover-text', '--dbm-id-chip-hover-bg', theme)).toBeGreaterThanOrEqual(4.5);
  });

  test('code reads 4.5:1 on its block', () => {
    expect(ratio('--dbm-code-text', '--dbm-code-bg', theme)).toBeGreaterThanOrEqual(4.5);
    expect(ratio('--dbm-text-code', '--dbm-bg-code', theme)).toBeGreaterThanOrEqual(4.5);
  });

  test('card edges keep 3:1 against their card', () => {
    ['error', 'warning', 'success', 'stopped'].forEach((tone) => {
      const edge = `--dbm-card-${tone}-border`;
      expect([edge, ratio(edge, '--dbm-card-neutral-bg', theme) >= 3]).toEqual([edge, true]);
    });
    [['info', 'info'], ['note', 'note'], ['danger', 'danger']].forEach(([edge, bg]) => {
      expect([edge, ratio(`--dbm-card-${edge}-border`, `--dbm-card-${bg}-bg`, theme) >= 3]).toEqual([edge, true]);
    });
  });
});

test('the dark theme is not the light theme', () => {
  ['--dbm-page-bg', '--dbm-text-primary', '--dbm-moved-bg', '--dbm-card-info-bg', '--dbm-code-bg'].forEach((name) => {
    expect([name, value(name, 'dark') !== value(name, 'light')]).toEqual([name, true]);
  });
});
