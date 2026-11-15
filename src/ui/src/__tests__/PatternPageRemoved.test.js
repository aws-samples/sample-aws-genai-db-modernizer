// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2300: the old pattern-analysis page is gone: no module, no route to it, no
 * locale keys, and no code in the UI links to its URL. (The redirect from the
 * old URL is tested in AppRoutes.test.js.)
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..');

function sourceFiles(dir, out = []) {
  fs.readdirSync(dir, { withFileTypes: true }).forEach((entry) => {
    // Names come from reading this repo's own src/ tree, not from user input.
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== '__tests__') sourceFiles(full, out);
    } else if (/\.js$/.test(entry.name)) {
      out.push(full);
    }
  });
  return out;
}

test('the page module is gone', () => {
  expect(fs.existsSync(path.join(SRC, 'pages', 'PatternAnalysis.js'))).toBe(false);
});

test('no UI source imports it or names its pattern-analysis locale keys', () => {
  const offenders = sourceFiles(SRC).filter((file) => /PatternAnalysis|EngineFitFigure|['"`]pattern-analysis\./.test(fs.readFileSync(file, 'utf8')));
  expect(offenders.map((f) => path.relative(SRC, f))).toEqual([]);
});

test('no locale file keeps a pattern-analysis key', () => {
  const dir = path.join(SRC, 'locales');
  fs.readdirSync(dir).filter((f) => f.endsWith('.json')).forEach((file) => {
    // `file` is a name listed from src/locales, not user input.
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const json = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
    expect(Object.keys(json).filter((k) => k.startsWith('pattern-analysis'))).toEqual([]);
  });
});

test('the only mention of the old URL in a UI source is the redirect in the route table', () => {
  const mentions = sourceFiles(SRC).filter((file) => fs.readFileSync(file, 'utf8').includes('/analysis/patterns'));
  expect(mentions.map((f) => path.relative(SRC, f))).toEqual(['AppRoutes.js']);
});
