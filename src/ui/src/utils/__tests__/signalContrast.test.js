// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2312: the per-query review's signal chips fail WCAG AA contrast (the engine colour at
 * 12% opacity behind the same colour at 11 px read 3.96:1). A chip is now an EngineBadge,
 * a filled badge whose text colour is picked for the fill, so it does not depend on the
 * page theme. The ratios of the palette are computed here; the rendered chips are
 * tested in components/__tests__/SignalChips.test.js.
 * The page's layout (sentence-case headers, the list scrolling inside itself) is read
 * from its CSS and JSX; the browser checks are in tests/e2e/test_ui.py.
 */
const fs = require('fs');
const path = require('path');
const { ENGINE_HEX } = require('../engineCatalog.generated');
const { engineHex, engineBadgeStyle, contrastRatio } = require('../engineColors');

const AA_SMALL_TEXT = 4.5;
const PAGES = path.join(__dirname, '..', '..', 'pages');
const GATE_JS = fs.readFileSync(path.join(PAGES, 'AssignmentGate.js'), 'utf8');
const GATE_CSS = fs.readFileSync(path.join(PAGES, 'AssignmentGate.css'), 'utf8');

// These two guard the catalog palette (they pass on main, whose badges were already
// filled): every engine's fill keeps a 4.5:1 text colour. The rendered chip test is
// components/__tests__/SignalChips.test.js.
test.each([...Object.keys(ENGINE_HEX), 'not-in-the-catalog'])('palette: %s fill meets 4.5:1', (engine) => {
  const { background, color } = engineBadgeStyle(engine).root;
  expect(background).toBe(engineHex(engine));
  expect(contrastRatio(background, color)).toBeGreaterThanOrEqual(AA_SMALL_TEXT);
});

test('the collapsed row draws its signals with SignalChips', () => {
  expect(GATE_JS).toMatch(/<SignalChips signals=\{signals\} \/>/);
  expect(GATE_JS).not.toMatch(/className="signal-badge"/);
  expect(GATE_CSS).not.toMatch(/\.signal-badge\s*\{/);
});

test('column headers are sentence case, not forced to capitals', () => {
  const header = /\.query-list-header\s*\{([^}]*)\}/.exec(GATE_CSS)[1];
  expect(header).not.toMatch(/text-transform/);
});

test('the query list scrolls inside its own named region, never the page', () => {
  expect(/\.query-list-scroll\s*\{([^}]*)\}/.exec(GATE_CSS)[1]).toMatch(/overflow-x:\s*auto/);
  expect(/\.query-list\s*\{([^}]*)\}/.exec(GATE_CSS)[1]).toMatch(/min-width:\s*\d+px/);
  // A scrollable region must take focus (axe scrollable-region-focusable) and carry a name.
  expect(GATE_JS).toMatch(/className="query-list-scroll"\s+role="region"\s+tabIndex=\{0\}\s+aria-label=/);
});
