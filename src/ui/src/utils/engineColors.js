/**
 * Colour, badge colour and label per engine for the assignment gate, built from the
 * engine catalog's generated tables (src/catalog/engines, scripts/render_catalog.py,
 * #492). The key order is the gate's engine-picker order, each engine's
 * ui_picker_order in the catalog, so a new catalog engine appears here with no edit.
 */
import {
  ENGINE_BADGE_COLORS, ENGINE_HEX, ENGINE_LABELS, ENGINE_PICKER_ORDER,
} from './engineCatalog.generated';

export const ENGINE_STYLES = Object.fromEntries(ENGINE_PICKER_ORDER.map((engine) => [
  engine,
  { bg: ENGINE_HEX[engine], badge: ENGINE_BADGE_COLORS[engine], label: ENGINE_LABELS[engine] },
]));

// The default blue the pages used before the catalog (ChartSankey.js's old
// NODE_COLORS fallback): an engine the palette does not know keeps a readable
// colour instead of the literal "undefined" a missing private-table key produced.
export const DEFAULT_ENGINE_HEX = '#0972D3';

// The live UI colour for an engine id -- one lookup for every page, so a new
// catalog engine reaches all of them with no edit (#482).
export const engineHex = (engineId) => ENGINE_HEX[engineId] || DEFAULT_ENGINE_HEX;
