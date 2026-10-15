/**
 * Display names for the engine keys the pipeline emits, shared by the
 * live React results page and the "Export to HTML" button's standalone
 * export (#225: badges used to show the raw engine key, e.g. "dynamodb",
 * instead of a display name like the rest of the report).
 *
 * Generated from the engine catalog (``src/catalog/engines/*.toml``, #492) into
 * ``engineCatalog.generated.js`` by ``scripts/render_catalog.py``, the same
 * files ``src/shared/engine_names.py``'s ``ENGINE_DISPLAY_NAMES`` reads.
 */
import { ENGINE_LABELS } from './engineCatalog.generated';

export { ENGINE_LABELS };

export const displayEngine = (engine) => ENGINE_LABELS[engine] || engine;
