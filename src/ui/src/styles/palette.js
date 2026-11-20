// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The UI's few hard-coded colours, in one place (#2311). Everything else is a Cloudscape
 * design token (styles/themeTokens.js), so it follows the light and dark themes. A colour
 * lives here only when no token can do its job, and each says why.
 * src/ui/src/__tests__/NoHardCodedColours.test.js fails on a hex or rgb colour anywhere
 * else in src/ui/src; the engine colours themselves are catalog data
 * (src/catalog/engines/*.toml, rendered into utils/engineCatalog.generated.js).
 */

// The neutral grey for an id the engine catalog does not know, and the Sankey's source
// node. It sits beside the catalog's engine fills, which are fixed colours in both
// themes, and the badge text on it is picked by contrast maths, which needs a real
// colour (a token is a CSS variable). The executive report uses the same grey.
export const NEUTRAL_ENGINE_HEX = '#5f6b7a';

// The two text colours an engine badge chooses from, by WCAG contrast on its fill
// (utils/engineColors.js badgeTextColor). The fills do not change with the theme, so
// neither does the text on them, and the contrast maths needs real colours.
export const BADGE_TEXT_DARK = '#000716';
export const BADGE_TEXT_LIGHT = '#ffffff';

// The graph explorer's canvas (components/GraphExplorerCanvas.js). Cytoscape paints on
// a <canvas>, which cannot read CSS variables, so the canvas gets real colours, one set
// per theme where the theme changes them. The legend (GraphExplorerViewBar.js) draws its
// samples from the same values, so a legend line matches the line it explains.
export const GRAPH_CANVAS = {
  dark: { text: '#e9ebed', textBackground: '#161d26', edge: '#8d99a8' },
  light: { text: '#000716', textBackground: '#ffffff', edge: '#7d8998' },
  // The node tile behind every AWS service and table icon. The icons are published
  // white-on-colour artwork, so the tile stays dark in both themes.
  nodeFill: '#2a3644',
  nodeBorder: '#5f6b7a',
  // Placeholders, other waves' links and nodes, and cross-wave links.
  muted: '#7d8998',
  mutedText: '#8d99a8',
  // A question's result (the Questions tab), under the selection's orange.
  result: '#00a1c9',
  // The selection and hover highlight, and the active walkthrough's outline.
  selected: '#ff9900',
};
