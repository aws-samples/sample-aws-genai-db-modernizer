/**
 * The target databases the create-analysis form and the settings page offer, read from
 * the engine catalog's status (src/catalog/engines, ENGINE_STATUS in the generated
 * module, #724). An active engine is a routing target and can be ticked; a future one is
 * a named placeholder (ADR-031 Decision 9) shown disabled as "Coming soon"; a legacy id
 * (the generic "aurora" older reports carry) is not offered at all. A new catalog engine
 * reaches every form with no edit here.
 */
import { ENGINE_LABELS, ENGINE_STATUS } from './engineCatalog.generated';

const CATALOG_ORDER = Object.keys(ENGINE_LABELS);

export const SELECTABLE_TARGET_ENGINES = CATALOG_ORDER.filter((id) => ENGINE_STATUS[id] === 'active');

export const COMING_SOON_TARGET_ENGINES = CATALOG_ORDER.filter((id) => ENGINE_STATUS[id] === 'future');

// Every selectable engine set to `checked`: "Select all", "Deselect all" and the defaults.
export const targetEngineSelection = (checked) => Object.fromEntries(
  SELECTABLE_TARGET_ENGINES.map((id) => [id, checked]),
);

// The ticked engine ids, in catalog order. Only selectable engines count, so a stale
// key (a placeholder, a legacy id) in a saved selection never reaches the request.
export const selectedTargetEngines = (selection) => SELECTABLE_TARGET_ENGINES.filter(
  (id) => selection?.[id] === true,
);
