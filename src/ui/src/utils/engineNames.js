/**
 * Display names for the engine keys the pipeline emits, used by the live
 * React results page (#225: badges used to show the raw engine key, e.g.
 * "dynamodb", instead of a display name like the rest of the report).
 *
 * Generated from the engine catalog (``src/catalog/engines/*.toml``, #492) into
 * ``engineCatalog.generated.js`` by ``scripts/render_catalog.py``, the same
 * files ``src/shared/engine_names.py``'s ``ENGINE_DISPLAY_NAMES`` reads.
 */
import { ENGINE_LABELS } from './engineCatalog.generated';

export { ENGINE_LABELS };

// #1329: the writer of a table whose writes have no engine in the customer's scope;
// not an engine, only a source_of_truth value (src/shared/engine_names.py SOURCE_DATABASE).
const SOURCE_DATABASE_LABEL = { source_database: 'Source database' };

export const displayEngine = (engine) => ENGINE_LABELS[engine] || SOURCE_DATABASE_LABEL[engine] || engine;
