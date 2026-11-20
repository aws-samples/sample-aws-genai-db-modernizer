// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2311: no hex or rgb colour in the UI's code or stylesheets. A colour is a Cloudscape
 * design token (styles/themeTokens.js for the CSS files, an import from
 * @cloudscape-design/design-tokens in a component), so it follows the light and dark
 * themes. The only literals are in the files below, each for a reason given there.
 * Comments are not scanned, so a note may still quote a colour or an issue number; in
 * code, a #123 reads as a colour. A colour after ' // ' inside a string is still found,
 * and so are the CSS named colours (white, black, red, ...) as the value of a colour-like
 * property (color: red; backgroundColor: 'white'). A bare 'white' or
 * <Badge color="blue"> is not. A source scan, like NoStyleOnCloudscape.test.js.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '..');

const ALLOWED = [
  // The colours no token can do (the graph canvas, badge text by contrast, the neutral grey).
  'styles/palette.js',
  // The engine colours: catalog data from src/catalog/engines/*.toml (scripts/render_catalog.py).
  'utils/engineCatalog.generated.js',
];

// A colour literal: #rgb, #rgba, #rrggbb or #rrggbbaa (not an HTML entity like &#123;),
// or an rgb(), rgba(), hsl() or hsla() call.
const COLOUR = /(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])|\b(?:rgba?|hsla?)\(/g;

// A CSS named colour as the value of a colour-like property: in a stylesheet or a style
// object (color: red; backgroundColor: 'white'). A JSX prop such as <Badge color="blue">
// is a Cloudscape name, not a CSS colour, and prose in a string or comment is not matched.
// "transparent", "inherit" and "currentColor" are not colours of their own.
const NAMED = 'white|black|red|green|blue|orange|yellow|purple|pink|gray|grey|silver|navy|teal|cyan|magenta|maroon|olive|lime|aqua|fuchsia|brown|gold|coral|crimson|indigo|violet|tan|beige|ivory|khaki|salmon|tomato';
const NAMED_COLOUR = new RegExp(`(?<=\\b[\\w-]*(?:colou?r|background|border|outline|fill|stroke|shadow)[\\w-]*['"]?\\s*:\\s*['"\`]?(?:[^;'"\`]*\\s)?)(?:${NAMED})(?=['"\`;,}\\s]|$)`, 'gi');

// Comments out, line numbers kept: block comments (JSX {/* */} too), then line comments.
// A line comment starts a line or follows a space, and the text before it must not leave
// a quote open, so a ' // ' inside a string stays (and so does "https://").
const stripLineComment = (line) => {
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      if (ch === '\\') i += 1;
      else if (ch === quote) quote = null;
    } else if (ch === "'" || ch === '"' || ch === '`') quote = ch;
    else if (ch === '/' && line[i + 1] === '/' && (i === 0 || /\s/.test(line[i - 1]))) return line.slice(0, i);
  }
  return line;
};
const withoutComments = (text) => text
  .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
  .split('\n').map(stripLineComment).join('\n');

const files = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
  // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
  const full = path.join(dir, entry.name);
  if (entry.isDirectory()) return ['__tests__', 'assets'].includes(entry.name) ? [] : files(full);
  return /\.(jsx?|css)$/.test(entry.name) ? [full] : [];
});

const colourHits = (text) => withoutComments(text).split('\n').flatMap((line, i) => (
  [...line.matchAll(COLOUR), ...line.matchAll(NAMED_COLOUR)].map((m) => `${i + 1}: ${m[0]}`)
));

test('the scan reads the pages, components, utilities and stylesheets', () => {
  const names = files(SRC).map((f) => path.relative(SRC, f));
  ['pages/AnalysisResults.js', 'pages/JobMonitoringSummary.js', 'pages/AssignmentGate.css',
    'components/ChartSankey.js', 'components/GraphExplorerCanvas.js', 'styles/global.css',
    'utils/engineColors.js', ...ALLOWED].forEach((name) => expect(names).toContain(name));
});

test('no hex or rgb colour outside the palette module and the engine catalog', () => {
  const found = files(SRC)
    .map((f) => [path.relative(SRC, f), colourHits(fs.readFileSync(f, 'utf8'))])
    .filter(([name, hits]) => hits.length > 0 && !ALLOWED.includes(name));
  expect(found).toEqual([]);
});

describe('the scan', () => {
  test.each([
    ["const a = '#ec7211';", ['1: #ec7211']],
    ['  color: #FFF;', ['1: #FFF']],
    ["const black = '#000';", ['1: #000']],
    ["background: `${x}` + '#0f141a80'", ['1: #0f141a80']],
    ['  border: 1px solid rgba(0, 0, 0, 0.1);', ['1: rgba(']],
    ['x = hsl(10, 50%, 50%)', ['1: hsl(']],
    ["const s = 'a // b'; const c = '#ec7211';", ['1: #ec7211']],
    ['const s = "see // x #fff";', ['1: #fff']],
    ["const s = { color: 'white' };", ['1: white']],
    ['const s = { backgroundColor: "Black" };', ['1: Black']],
    ["const s = { border: '1px solid red' };", ['1: red']],
    ['  color: red;', ['1: red']],
    ['  background: orange !important;', ['1: orange']],
  ])('finds %s', (text, hits) => {
    expect(colourHits(text)).toEqual(hits);
  });

  test.each([
    '// #2311: the old #ec7211 orange',
    '/* #ff9900 */ const a = 1;',
    "{/* #2446: the tab's one list */}",
    "href: '#query-routing-section'",
    'const entity = "&#123;";',
    "const url = 'https://example.com/a'; // #fff",
    "const s = 'a // b'; // #fff",
    "const label = 'white label';",
    "const c = 'white';",
    '<Badge color="blue">{x}</Badge>',
    'const t = "Whitelist is not a colour";',
    '  color: var(--dbm-text-primary);',
    '  color: transparent;',
    "const x = { red: 1 };",
  ])('ignores %s', (text) => {
    expect(colourHits(text)).toEqual([]);
  });

  test('a block comment keeps the line numbers after it', () => {
    expect(colourHits("/* one\n two */\nconst a = '#abcdef';")).toEqual(['3: #abcdef']);
  });
});
