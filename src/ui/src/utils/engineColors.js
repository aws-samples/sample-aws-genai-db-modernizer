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
