/**
 * One colour per engine for every UI view, from the engine catalog (colors.ui in
 * src/catalog/engines, rendered into engineCatalog.generated.js by
 * scripts/render_catalog.py, #492). The executive report draws its engine accents from
 * the same field (#1246), so a reader learns "colour = engine" once: badges, the Sankey,
 * the roadmap, the assignment gate, the pattern analysis page and the PDF agree.
 *
 * ENGINE_STYLES is keyed in the gate's engine-picker order (each engine's
 * ui_picker_order), so a new catalog engine appears here with no edit.
 */
import { ENGINE_HEX, ENGINE_LABELS, ENGINE_PICKER_ORDER } from './engineCatalog.generated';

export const ENGINE_STYLES = Object.fromEntries(ENGINE_PICKER_ORDER.map((engine) => [
  engine,
  { bg: ENGINE_HEX[engine], label: ENGINE_LABELS[engine] },
]));

// A neutral grey for an id the catalog does not know (#1246): never another engine's
// colour (the old default blue read as DynamoDB's), and never the literal "undefined"
// a missing private-table key produced (#482).
export const DEFAULT_ENGINE_HEX = '#5f6b7a';

// The live UI colour for an engine id -- one lookup for every page, so a new
// catalog engine reaches all of them with no edit (#482).
export const engineHex = (engineId) => ENGINE_HEX[engineId] || DEFAULT_ENGINE_HEX;

const DARK_TEXT = '#000716';
const LIGHT_TEXT = '#ffffff';

// WCAG relative luminance of a #rrggbb colour. The executive report computes the same
// for its share-bar labels (contrast_ratio in src/report/executive_report.py); keep the
// two in step.
const luminance = (hex) => {
  const channel = (i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
};

export const contrastRatio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

// The badge text colour with the higher contrast on `background`.
export const badgeTextColor = (background) => (
  contrastRatio(background, LIGHT_TEXT) >= contrastRatio(background, DARK_TEXT) ? LIGHT_TEXT : DARK_TEXT
);

// The Cloudscape Badge `style` for an engine (#1246, building on #1105's shared
// engineBadgeColor lookup for #1088): the badge is filled with the engine's catalog
// colour, so it matches the Sankey node and the PDF, instead of the four named badge
// colours, which forced Aurora into DocumentDB's green or a grey.
export const engineBadgeStyle = (engineId) => {
  const background = engineHex(engineId);
  return { root: { background, borderColor: background, color: badgeTextColor(background) } };
};
