// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * For tests only (#2311): what a Cloudscape design token resolves to in the light and the
 * dark theme, read from the installed components' own theme stylesheet. A token is a
 * string like "var(--color-text-body-default-b2us04, #0f141a)"; the hashed name must be
 * one the installed @cloudscape-design/components defines, or the browser silently uses
 * the light fallback in both themes. `defined` says whether it is.
 */
const fs = require('fs');
const path = require('path');

// The package exports no path to its theme stylesheet, so it is found next to package.json.
const THEME_CSS = path.join(
  path.dirname(require.resolve('@cloudscape-design/components/package.json')),
  'internal', 'base-theme', 'styles.scoped.css',
);

const declarations = (block) => Object.fromEntries(
  [...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
);

const blockAfter = (css, opener) => {
  const at = css.indexOf(opener);
  if (at === -1) throw new Error(`no ${opener} block in ${THEME_CSS}`);
  return css.slice(at + opener.length, css.indexOf('}', at));
};

let cached;
const themes = () => {
  if (!cached) {
    const css = fs.readFileSync(THEME_CSS, 'utf8');
    const light = declarations(blockAfter(css, 'body {'));
    const dark = { ...light, ...declarations(blockAfter(css, '.awsui-dark-mode:not(#\\9) {')) };
    cached = { light, dark };
  }
  return cached;
};

const resolve = (value, table) => {
  let v = value;
  for (let i = 0; i < 10 && v && v.startsWith('var('); i += 1) {
    const name = /^var\((--[\w-]+)/.exec(v)[1];
    v = table[name];
  }
  return v;
};

const tokenName = (token) => {
  const m = /^var\((--[\w-]+),/.exec(token);
  if (!m) throw new Error(`not a design token: ${token}`);
  return m[1];
};

// { defined, light, dark } for one token string.
const tokenValues = (token) => {
  const name = tokenName(token);
  const { light, dark } = themes();
  return { defined: name in light, light: resolve(light[name], light), dark: resolve(dark[name], dark) };
};

module.exports = { tokenValues, tokenName };
