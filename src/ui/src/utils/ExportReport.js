/**
 * ExportReport.js
 * Utility module for generating interactive HTML reports with Chart.js
 *
 * NOTE: This file is EXEMPT from i18n requirements because it generates
 * self-contained, standalone HTML files that are viewed outside the application.
 * These exported files:
 * - Do not use React or i18next
 * - Are meant to be portable and shareable
 * - Are viewed in any browser without the application context
 * - All text is in English by design for maximum portability
 *
 * SECURITY: every report.json value is untrusted (LLM- or customer-derived). The
 * shell escapes with escapeHtml() from ./escapeHtml.js; the embedded client script
 * escapes text and attribute values with its own escapeHtml() and passes values to
 * inline on* handlers only through jsArg(). DATA is embedded with jsonForScript()
 * so a "</script>" inside a string cannot end the script element (#242).
 */

// HTML escaping for the shell below; the embedded client script carries its own copy
// (the exported file is standalone). Both escape & < > " ' -- see ./escapeHtml.js.
import { escapeHtml, jsonForScript } from './escapeHtml';
import {
  getCacheOverlay, formatCacheLayerLine, targetEngineEntries, resolveCostBreakdown,
  ownerSchemaDesigns, cacheAccessPatternCount,
} from './cacheLayer';

// #358: this file is English-only (i18n-exempt, see header), so this is the
// one literal copy of the suffix cacheLayer.js's formatCacheLayerLine uses by
// default -- shared between the shell's "Target Engines" badges and cost
// cards (built directly in generateHTMLReport, below) and the embedded
// client script's own cost cards (buildCostBreakdown, generated further
// down): all three label the cache engine "<Name> (cache layer)" instead of
// a plain owner badge. The embedded script can't reference this constant
// directly (scripts/sync_report_template.py requires every `script += '...'`
// line to be its own single-quoted literal, so the suffix is hardcoded there
// too) -- exported so ExportReport.cacheLayer.test.js can assert the
// generated script's literal still matches this one.
export const CACHE_LAYER_SUFFIX = ' (cache layer)';

// #358: the embedded client script runs standalone in the exported HTML --
// no bundler, so it can't `import` resolveCostBreakdown/isKeptCostEngine from
// cacheLayer.js -- so buildCostBreakdown (generated below) mirrors this exact
// expression inline instead, as a hardcoded literal (same reason as
// CACHE_LAYER_SUFFIX above). Kept as a string constant with the same text, so
// ExportReport.cacheLayer.test.js can both `new Function(...)` it to assert
// it behaves like cacheLayer.js's isKeptCostEngine, and assert the generated
// script's literal still matches it -- rather than letting the two copies
// silently drift.
export const KEEP_COST_CARD_EXPR = 'afterDist[cb.database] != null || cb.database === cacheEngine';

// #405: the embedded client script's extractPatterns() (generated below) mirrors
// ownerSchemaDesigns's cache-engine filter -- the cache layer gets its own schema
// design (and its own access_patterns) too, but those were never queries it owns,
// so counting them here would double-count ElastiCache as if it were an owner
// engine in the Access Pattern Explorer's total, pie and rows, the same gap #361
// fixed on the Results page. Only skips when the report carries a cache_overlay
// (hasCacheOverlay, set from DATA.results.synthesis.cache_overlay just above):
// a legacy report with no overlay at all still has ElastiCache as a real owner,
// matching src/report/analysis_report.py (which only strips a design when
// cache_overlay.engine names it) and ownerSchemaDesigns's own hasOverlay guard.
// No bundler at runtime, so extractPatterns can't `import` isCacheEngine from
// cacheLayer.js -- it hardcodes this exact expression as a literal instead (same
// reason as CACHE_LAYER_SUFFIX/KEEP_COST_CARD_EXPR above). Kept as a string
// constant with the same text, so ExportReport.cachePatterns.test.js can both
// `new Function(...)` it to assert it agrees with cacheLayer.js's isCacheEngine,
// and assert the generated script's literal still matches it.
export const CACHE_ENGINE_SKIP_EXPR = "hasCacheOverlay && design.target_type === 'elasticache'";

// Engine, operation and chart colours are NOT declared here. The palette lives in
// exactly one place -- the :root block of REPORT_CSS below -- and both the badges
// (via [data-engine] rules) and the charts (via paletteColor() at runtime) read it
// from there, so re-theming the report is a single edit.
// Display names for every engine key the pipeline emits. Kept in sync with
// ENGINE_LABELS in src/report/analysis_report.py (itself backed by
// src/shared/engine_names.py) by
// tests/unit/report/test_report_template_sync.py.
const ENGINE_LABELS = {
  dynamodb: 'DynamoDB', documentdb: 'DocumentDB', opensearch: 'OpenSearch',
  elasticache: 'ElastiCache', aurora_postgresql: 'Aurora PostgreSQL',
  aurora_mysql: 'Aurora MySQL', neptune: 'Neptune', keyspaces: 'Keyspaces',
  aurora: 'Aurora',
};


// CSS styles as a regular string (not a template literal) to avoid Semgrep false positives
// Using single quotes to avoid any template literal syntax
// NOTE: every line of this concatenation must stay a single-quoted string
// literal — scripts/sync_report_template.py lifts it verbatim into the ATX
// report template and rejects anything else (including comments) inside it.
//
// .grid-auto exists because a card count driven by the recommended-engine count
// outgrows the fixed .grid-N classes: 5 engines asks for .grid-5, which is not
// defined, so the grid falls back to a single column and stacks vertically.
const REPORT_CSS = '\n' +
  '  :root {\n' +
  '    --indigo-900: #150f35; --indigo-800: #241a5c; --indigo-700: #33268a;\n' +
  '    --indigo-600: #4a3aa8; --indigo-500: #5f4bd0; --indigo-400: #7a5af5;\n' +
  '    --indigo-300: #9c85f8; --indigo-200: #ae97f0; --indigo-100: #ddd6fb;\n' +
  '    --engine-dynamodb: var(--indigo-400); --engine-documentdb: var(--indigo-700);\n' +
  '    --engine-elasticache: var(--indigo-500); --engine-opensearch: var(--indigo-900);\n' +
  '    --engine-aurora_postgresql: var(--indigo-600); --engine-aurora_mysql: var(--indigo-800);\n' +
  '    --engine-aurora: var(--engine-aurora_postgresql); --engine-neptune: var(--indigo-300);\n' +
  '    --engine-keyspaces: var(--indigo-200); --engine-fallback: var(--indigo-600);\n' +
  '    --op-read: var(--indigo-400); --op-write: var(--indigo-700); --op-search: var(--indigo-200);\n' +
  '    --op-update: var(--indigo-500); --op-delete: var(--indigo-900);\n' +
  '    --chart-neutral: #8794a4;\n' +
  '    --radius-container: 16px;\n' +
  '    --color-bg-container: #fff; --color-bg-layout: #f7f9fb; --color-border: #d5dbdb;\n' +
  '    --color-border-subtle: #e6ebf0;\n' +
  '    --color-text: #0f1b2a; --color-text-secondary: #5f6b7a;\n' +
  '    --color-badge-neutral: #eaeded;\n' +
  '    --color-brand: #01a88d;\n' +
  '    --color-accent: var(--indigo-600); --color-accent-tint: var(--indigo-100);\n' +
  '    --color-blue: var(--color-accent);\n' +
  '    --font-family: \'Amazon Ember\', \'Helvetica Neue\', Roboto, Arial, sans-serif;\n' +
  '  }\n' +
  '  * { margin: 0; padding: 0; box-sizing: border-box; }\n' +
  '  button, input, select, textarea { font-family: inherit; }\n' +
  '  body { font-family: var(--font-family); background: var(--color-bg-layout); color: var(--color-text); padding: 24px; line-height: 1.5; }\n' +
  '  .container { max-width: 1400px; margin: 0 auto; }\n' +
  '  .section { background: var(--color-bg-container); padding: 28px 32px; border: 1px solid var(--color-border-subtle); border-radius: var(--radius-container); margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,28,36,0.04), 0 8px 24px -8px rgba(0,28,36,0.08); }\n' +
  '  .section-header { font-size: 20px; font-weight: 700; margin-bottom: 4px; }\n' +
  '  .section-desc { color: var(--color-text-secondary); font-size: 14px; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--color-border-subtle); }\n' +
  '  .section-body { margin-bottom: 20px; padding: 20px; background: var(--color-bg-container); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-container); box-shadow: 0 1px 2px rgba(0,28,36,0.04), 0 8px 24px -8px rgba(0,28,36,0.08); }\n' +
  '  .meta-pairs { display: flex; flex-wrap: wrap; gap: 0 40px; margin-top: 20px; }\n' +
  '  .section-desc + .meta-pairs { margin-top: 0; }\n' +
  '  .report-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 32px; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--color-border-subtle); }\n' +
  '  .report-head .section-desc { margin-bottom: 0; padding-bottom: 0; border-bottom: none; }\n' +
  '  .report-head + .meta-pairs { margin-top: 0; }\n' +
  '  .report-brand { display: flex; flex-direction: column; align-items: center; gap: 4px; flex-shrink: 0; }\n' +
  '  .report-brand svg { display: block; width: 56px; height: 56px; }\n' +
  '  .report-brand .badge { white-space: nowrap; margin-right: 0; }\n' +
  '  .meta-pair { padding-left: 20px; border-left: 1px solid var(--color-border-subtle); }\n' +
  '  .meta-pair-label, .key-value-label { font-size: 14px; font-weight: 700; color: var(--color-text); }\n' +
  '  .meta-pair-value, .key-value-value { font-size: 14px; color: var(--color-text); }\n' +
  '  .grid { display: grid; gap: 16px; }\n' +
  '  .grid-2 { grid-template-columns: repeat(2, 1fr); }\n' +
  '  .grid-3 { grid-template-columns: repeat(3, 1fr); }\n' +
  '  .grid-4 { grid-template-columns: repeat(4, 1fr); }\n' +
  '  .grid-auto { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }\n' +
  '  .stat-card, .item-card { padding: 20px; background: var(--color-bg-container); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-container); box-shadow: 0 1px 2px rgba(0,28,36,0.04), 0 8px 24px -8px rgba(0,28,36,0.08); }\n' +
  '  .item-card { margin: 12px 0; }\n' +
  '  .stat-label { font-size: 12px; color: var(--color-text-secondary); margin-bottom: 4px; }\n' +
  '  .stat-value { font-size: 24px; font-weight: 700; }\n' +
  '  .stat-note { font-size: 12px; color: var(--color-text-secondary); margin-top: 4px; font-weight: 400; }\n' +
  '  .badge { display: inline-block; padding: 3px 10px; border-radius: var(--radius-container); font-size: 12px; font-weight: 600; margin-right: 4px; background: var(--color-badge-neutral); color: var(--color-text); }\n' +
  '  .badge-blue { background: var(--indigo-600); color: white; }\n' +
  '  .badge-green { background: var(--indigo-700); color: white; }\n' +
  '  .badge-red { background: var(--indigo-500); color: white; }\n' +
  '  .badge-orange { background: var(--indigo-800); color: white; }\n' +
  '  .badge-grey { background: var(--color-badge-neutral); color: var(--color-text); }\n' +
  '  .badge[data-engine] { background: var(--engine-fallback); color: white; }\n' +
  '  .badge[data-engine="dynamodb"] { background: var(--engine-dynamodb); }\n' +
  '  .badge[data-engine="documentdb"] { background: var(--engine-documentdb); }\n' +
  '  .badge[data-engine="elasticache"] { background: var(--engine-elasticache); }\n' +
  '  .badge[data-engine="opensearch"] { background: var(--engine-opensearch); }\n' +
  '  .badge[data-engine="aurora_postgresql"] { background: var(--engine-aurora_postgresql); }\n' +
  '  .badge[data-engine="aurora_mysql"] { background: var(--engine-aurora_mysql); }\n' +
  '  .badge[data-engine="aurora"] { background: var(--engine-aurora); }\n' +
  '  .badge[data-engine="neptune"] { background: var(--engine-neptune); color: var(--indigo-900); }\n' +
  '  .badge[data-engine="keyspaces"] { background: var(--engine-keyspaces); color: var(--indigo-900); }\n' +
  '  table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 16px; }\n' +
  '  thead { background: var(--color-bg-container); border-bottom: 1px solid var(--color-border); }\n' +
  '  th { text-align: left; padding: 12px 16px; font-weight: 700; font-size: 14px; white-space: nowrap; }\n' +
  '  td { padding: 8px 16px; border-bottom: 1px solid var(--color-border); }\n' +
  '  th.nowrap, td.nowrap { white-space: nowrap; }\n' +
  '  tbody tr:hover { background: #f9fafb; }\n' +
  '  .link { color: var(--color-blue); text-decoration: none; cursor: pointer; }\n' +
  '  .link:hover { text-decoration: underline; }\n' +
  '  .chart-container { position: relative; height: 300px; margin: 16px 0; }\n' +
  '  .filter-bar { display: flex; gap: 16px; align-items: center; margin: 16px 0; padding: 16px; background: var(--color-bg-layout); border-radius: var(--radius-container); }\n' +
  '  .filter-chip { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px; background: var(--color-blue); color: white; border-radius: 16px; font-size: 14px; }\n' +
  '  .filter-chip button { background: none; border: none; color: white; cursor: pointer; font-size: 16px; padding: 0 4px; }\n' +
  '  .filter-input { flex: 1; padding: 8px 12px; border: 1px solid var(--color-border); border-radius: var(--radius-container); font-size: 14px; }\n' +
  '  .btn { padding: 8px 16px; border: 1px solid var(--color-border); background: white; border-radius: var(--radius-container); cursor: pointer; font-size: 14px; }\n' +
  '  .btn:hover { background: var(--color-bg-layout); }\n' +
  '  .pagination { display: flex; justify-content: center; gap: 8px; margin-top: 16px; }\n' +
  '  .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; }\n' +
  '  .modal-content { background: white; border-radius: 8px; max-width: 90%; max-height: 90%; overflow: auto; box-shadow: 0 4px 20px rgba(0,0,0,0.3); }\n' +
  '  .modal-header { padding: 24px; border-bottom: 1px solid var(--color-border); display: flex; justify-content: space-between; align-items: center; }\n' +
  '  .modal-header h2 { font-size: 20px; font-weight: 700; margin: 0; }\n' +
  '  .modal-close { background: none; border: none; font-size: 24px; cursor: pointer; color: var(--color-text-secondary); padding: 4px; line-height: 1; }\n' +
  '  .modal-body { padding: 28px; max-height: 70vh; overflow-y: auto; font-size: 14px; }\n' +
  '  .modal-body .badge { font-size: 14px; padding: 4px 12px; }\n' +
  '  .tab-bar { display: flex; border-bottom: 2px solid var(--color-border); margin-bottom: 20px; }\n' +
  '  .tab-button { padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; color: var(--color-text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px; }\n' +
  '  .tab-button.active { color: var(--color-blue); border-bottom-color: var(--color-blue); }\n' +
  '  .tab-content { display: none; }\n' +
  '  .tab-content.active { display: block; }\n' +
  '  .key-value-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; margin: 24px 0; }\n' +
  '  .key-value-item { }\n' +
  '  .key-value-block { margin-top: 24px; }\n' +
  '  .code-block { background: var(--color-badge-neutral); color: var(--color-text); padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 14px; white-space: pre-wrap; word-break: break-word; margin: 12px 0; }\n' +
  '  tbody tr { cursor: pointer; }\n' +
  '  .toggle-group { display: inline-flex; border: 1px solid var(--color-border); border-radius: var(--radius-container); overflow: hidden; }\n' +
  '  .toggle-btn { padding: 6px 16px; border: none; background: white; cursor: pointer; font-size: 14px; font-weight: 600; color: var(--color-text-secondary); border-right: 1px solid var(--color-border); }\n' +
  '  .toggle-btn:last-child { border-right: none; }\n' +
  '  .toggle-btn.active { background: var(--color-blue); color: white; }\n' +
  '  .toggle-btn:hover:not(.active) { background: var(--color-bg-layout); }\n';

// Helper function to generate the report JavaScript code without template literals
// This eliminates Semgrep false positives for missing-template-string-indicator
const generateReportScript = (data, ENGINE_LABELS) => {
  const { results, schemaDesigns, collector, jobId, queryJourneys } = data;

  // Build the script using string concatenation (not template literals)
  let script = '';
  script += '  <script>\n';
  script += '    const DATA = ' + jsonForScript({ results, schemaDesigns, collector, jobId, queryJourneys }, 2) + ';\n';
  script += '    const ENGINE_LABELS = ' + jsonForScript(ENGINE_LABELS) + ';\n';
  script += '\n';
  script += '    // The palette lives only in the CSS :root block. Charts and SVG need real\n';
  script += '    // colour values, so read the custom properties back at runtime -- the\n';
  script += '    // computed value of a custom property has its var() references resolved.\n';
  script += '    const PALETTE_CACHE = {};\n';
  script += '    function paletteColor(name) {\n';
  script += '      if (!(name in PALETTE_CACHE)) {\n';
  script += '        PALETTE_CACHE[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim();\n';
  script += '      }\n';
  script += '      return PALETTE_CACHE[name];\n';
  script += '    }\n';
  script += '    function engineColor(engine) { return paletteColor(\'--engine-\' + engine) || paletteColor(\'--engine-fallback\'); }\n';
  script += '    function opColor(op) { return paletteColor(\'--op-\' + op) || paletteColor(\'--engine-fallback\'); }\n';
  script += '    function engineBadge(engine, label) {\n';
  script += '      return \'<span class="badge" data-engine="\' + escapeHtml(engine) + \'">\' + escapeHtml(label) + \'</span>\';\n';
  script += '    }\n';
  script += '\n';
  // Same contract as escapeHtml in ./escapeHtml.js: & < > " ' are all escaped, so a
  // value is safe both in text and inside a quoted attribute. (The previous
  // textContent/innerHTML version left quotes alone, so a value could close an
  // attribute such as data-engine="..." and add its own.)
  script += '    // Escape a value for HTML text or a quoted attribute value.\n';
  script += '    const HTML_ESCAPES = { \'&\': \'&amp;\', \'<\': \'&lt;\', \'>\': \'&gt;\', \'"\': \'&quot;\', "\'": \'&#39;\' };\n';
  script += '    function escapeHtml(text) {\n';
  script += '      if (text == null) return \'\';\n';
  script += '      return String(text).replace(/[&<>"\']/g, function(ch) { return HTML_ESCAPES[ch]; });\n';
  script += '    }\n';
  script += '\n';
  // Inline handlers: the browser decodes entities in the attribute before running
  // it, so escapeHtml alone turns &#39; back into a quote that ends a '...' argument.
  // JSON-quote first (a valid JS string literal), then escape for the attribute.
  script += '    // Encode a value as one JS string argument inside an inline on* handler.\n';
  script += '    function jsArg(value) { return escapeHtml(JSON.stringify(String(value))); }\n';
  script += '\n';
  script += '    // Create query journey lookup by query_id\n';
  script += '    const QUERY_JOURNEY_LOOKUP = {};\n';
  script += '    if (DATA.queryJourneys) {\n';
  script += '      const journeyItems = DATA.queryJourneys.items || [];\n';
  script += '      journeyItems.forEach(item => {\n';
  script += '        if (item && item.query_id) {\n';
  script += '          QUERY_JOURNEY_LOOKUP[item.query_id] = item;\n';
  script += '        }\n';
  script += '      });\n';
  script += '      console.log(\'Query Journey Lookup created with\', Object.keys(QUERY_JOURNEY_LOOKUP).length, \'items\');\n';
  script += '    }\n';
  script += '    function hasJourney(queryId) { return Object.prototype.hasOwnProperty.call(QUERY_JOURNEY_LOOKUP, queryId); }\n';
  script += '\n';
  script += '    let engineChart, operationChart;\n';
  script += '    let activeFilters = { engines: [], operations: [], text: \'\' };\n';
  script += '    let allPatterns = [];\n';
  script += '    let sourceTableGroups = [];\n';
  script += '    let browseMode = \'pattern\';\n';
  script += '    const PAGE_SIZE = 10;\n';
  script += '    let currentPage = 1;\n';
  script += '\n';
  script += '    function extractPatterns() {\n';
  script += '      const patterns = [];\n';
  // #405: only skip the cache engine's own design when the report actually
  // carries a cache_overlay -- a legacy report with none at all still has
  // ElastiCache as a real owner (mirrors ownerSchemaDesigns's hasOverlay guard
  // and src/report/analysis_report.py, which only strips a design when
  // cache_overlay.engine names it).
  script += '      const hasCacheOverlay = !!(DATA.results && DATA.results.synthesis && DATA.results.synthesis.cache_overlay);\n';
  script += '      DATA.schemaDesigns.forEach(design => {\n';
  // sync_report_template.py requires this `script += '...'` line to be its own
  // single-quoted literal (it can't splice in CACHE_ENGINE_SKIP_EXPR at build
  // time), so the text below is a hardcoded copy of that constant --
  // ExportReport.cachePatterns.test.js asserts the two agree.
  script += '        if (hasCacheOverlay && design.target_type === \'elasticache\') return;\n';
  script += '        const engine = design.target_type;\n';
  script += '        const content = design.content || {};\n';
  script += '        (content.access_patterns || []).forEach(ap => {\n';
  script += '          const opCategory = getOpCategory(ap.operation || ap.http_method);\n';
  script += '          patterns.push({\n';
  script += '            id: ap.pattern_id || ap.name || (engine + \'-\' + patterns.length),\n';
  script += '            engine, operation: ap.operation || ap.http_method || \'—\', opCategory,\n';
  script += '            sourceTables: (ap.source_tables || []).map(t => t.split(\'.\').pop()).join(\', \'),\n';
  script += '            sourceTablesArray: (ap.source_tables || []).map(t => t.split(\'.\').pop()),\n';
  script += '            destTable: ap.table_name || ap.key_pattern || ap.index_or_stream || ap.index || ap.collection || \'—\',\n';
  script += '            description: ap.description || ap.name || \'\', gsiName: ap.gsi_name || null\n';
  script += '          });\n';
  script += '        });\n';
  script += '      });\n';
  script += '      return patterns;\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildSourceTableGroups() {\n';
  script += '      const filtered = filterPatterns();\n';
  script += '      const groups = {};\n';
  script += '      filtered.forEach(ap => {\n';
  script += '        ap.sourceTablesArray.forEach(table => {\n';
  script += '          if (!groups[table]) {\n';
  script += '            groups[table] = { table, engines: new Set(), destTables: new Set(), patterns: [], convergesFrom: new Set() };\n';
  script += '          }\n';
  script += '          groups[table].engines.add(ap.engine);\n';
  script += '          if (ap.destTable !== \'—\') groups[table].destTables.add(ap.destTable);\n';
  script += '          groups[table].patterns.push(ap);\n';
  script += '        });\n';
  script += '      });\n';
  script += '      const destToSources = {};\n';
  script += '      filtered.forEach(ap => {\n';
  script += '        if (ap.destTable !== \'—\') {\n';
  script += '          if (!destToSources[ap.destTable]) destToSources[ap.destTable] = new Set();\n';
  script += '          ap.sourceTablesArray.forEach(t => destToSources[ap.destTable].add(t));\n';
  script += '        }\n';
  script += '      });\n';
  script += '      Object.values(groups).forEach(g => {\n';
  script += '        g.destTables.forEach(dt => {\n';
  script += '          const sources = destToSources[dt];\n';
  script += '          if (sources && sources.size > 1) {\n';
  script += '            sources.forEach(s => { if (s !== g.table) g.convergesFrom.add(s); });\n';
  script += '          }\n';
  script += '        });\n';
  script += '      });\n';
  script += '      return Object.values(groups).sort((a, b) => b.patterns.length - a.patterns.length);\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchBrowseMode(mode) {\n';
  script += '      browseMode = mode;\n';
  script += '      document.querySelectorAll(\'.toggle-btn\').forEach(btn => btn.classList.remove(\'active\'));\n';
  script += '      event.target.classList.add(\'active\');\n';
  script += '      currentPage = 1;\n';
  script += '      buildTable();\n';
  script += '    }\n';
  script += '\n';
  script += '    function getOpCategory(op) {\n';
  script += '      if (!op) return \'read\';\n';
  script += '      if (/get|read|find|scan|query|select/i.test(op)) return \'read\';\n';
  script += '      if (/put|insert|create|write|set|add/i.test(op)) return \'write\';\n';
  script += '      if (/search|agg|match/i.test(op)) return \'search\';\n';
  script += '      if (/update|modify|patch/i.test(op)) return \'update\';\n';
  script += '      if (/delete|remove/i.test(op)) return \'delete\';\n';
  script += '      return \'read\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function filterPatterns() {\n';
  script += '      let filtered = [...allPatterns];\n';
  script += '      if (activeFilters.engines.length > 0) filtered = filtered.filter(p => activeFilters.engines.includes(p.engine));\n';
  script += '      if (activeFilters.operations.length > 0) filtered = filtered.filter(p => activeFilters.operations.includes(p.opCategory));\n';
  script += '      if (activeFilters.text) {\n';
  script += '        const text = activeFilters.text.toLowerCase();\n';
  script += '        filtered = filtered.filter(p => p.id.toLowerCase().includes(text) || p.engine.toLowerCase().includes(text) || p.operation.toLowerCase().includes(text) || p.sourceTables.toLowerCase().includes(text) || p.destTable.toLowerCase().includes(text) || p.description.toLowerCase().includes(text));\n';
  script += '      }\n';
  script += '      return filtered;\n';
  script += '    }\n';
  script += '\n';
  script += '    function updateFilterDisplay() {\n';
  script += '      const container = document.getElementById(\'active-filters\');\n';
  script += '      const chips = [];\n';
  script += '      activeFilters.engines.forEach(e => {\n';
  script += '        chips.push(\'<span class="filter-chip">Engine = \' + escapeHtml(e) + \' <button onclick="removeFilter(&quot;engine&quot;, \' + jsArg(e) + \')">×</button></span>\');\n';
  script += '      });\n';
  script += '      activeFilters.operations.forEach(o => {\n';
  script += '        chips.push(\'<span class="filter-chip">Operation = \' + escapeHtml(o) + \' <button onclick="removeFilter(&quot;operation&quot;, \' + jsArg(o) + \')">×</button></span>\');\n';
  script += '      });\n';
  script += '      container.innerHTML = chips.join(\'\');  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml(), handler args via jsArg()\n';
  script += '    }\n';
  script += '\n';
  script += '    function removeFilter(type, value) {\n';
  script += '      if (type === \'engine\') activeFilters.engines = activeFilters.engines.filter(e => e !== value);\n';
  script += '      else if (type === \'operation\') activeFilters.operations = activeFilters.operations.filter(o => o !== value);\n';
  script += '      updateFilterDisplay();\n';
  script += '      buildTable();\n';
  script += '      updateCharts();\n';
  script += '    }\n';
  script += '\n';
  script += '    function clearAllFilters() {\n';
  script += '      activeFilters = { engines: [], operations: [], text: \'\' };\n';
  script += '      document.getElementById(\'filter-input\').value = \'\';\n';
  script += '      updateFilterDisplay();\n';
  script += '      buildTable();\n';
  script += '      updateCharts();\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildTable() {\n';
  script += '      if (browseMode === \'pattern\') buildPatternTable();\n';
  script += '      else buildSourceTableTable();\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildPatternTable() {\n';
  script += '      const filtered = filterPatterns();\n';
  script += '      document.getElementById(\'pattern-count\').textContent = filtered.length;\n';
  script += '      const start = (currentPage - 1) * PAGE_SIZE;\n';
  script += '      const paginated = filtered.slice(start, start + PAGE_SIZE);\n';
  script += '      const totalPages = Math.ceil(filtered.length / PAGE_SIZE);\n';
  script += '      let html = \'<table><thead><tr><th class="nowrap">Pattern ID</th><th>Operation</th><th>Engine</th><th>Source Tables</th><th>Destination</th><th>Description</th></tr></thead><tbody>\';\n';
  script += '      paginated.forEach(p => {\n';
  script += '        html += \'<tr onclick="showPatternDetails(\' + jsArg(p.id) + \')">\';\n';
  script += '        html += \'<td class="nowrap"><span class="link">\' + escapeHtml(p.id.slice(0, 8)) + \'</span></td>\';\n';
  script += '        html += \'<td>\' + escapeHtml(p.operation) + \'</td>\';\n';
  script += '        html += \'<td>\' + engineBadge(p.engine, ENGINE_LABELS[p.engine] || p.engine) + \'</td>\';\n';
  script += '        html += \'<td>\' + escapeHtml(p.sourceTables) + \'</td>\';\n';
  script += '        html += \'<td>\' + escapeHtml(p.destTable) + (p.gsiName ? \' (GSI: \' + escapeHtml(p.gsiName) + \')\' : \'\') + \'</td>\';\n';
  script += '        html += \'<td>\' + escapeHtml(p.description) + \'</td>\';\n';
  script += '        html += \'</tr>\';\n';
  script += '      });\n';
  script += '      html += \'</tbody></table><div class="pagination">\';\n';
  script += '      html += \'<button class="btn" onclick="changePage(-1)" \' + (currentPage === 1 ? \'disabled\' : \'\') + \'>Previous</button>\';\n';
  script += '      html += \'<span>Page \' + currentPage + \' of \' + totalPages + \'</span>\';\n';
  script += '      html += \'<button class="btn" onclick="changePage(1)" \' + (currentPage === totalPages ? \'disabled\' : \'\') + \'>Next</button>\';\n';
  script += '      html += \'</div>\';\n';
  script += '      document.getElementById(\'access-patterns-container\').innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildSourceTableTable() {\n';
  script += '      sourceTableGroups = buildSourceTableGroups();\n';
  script += '      document.getElementById(\'pattern-count\').textContent = sourceTableGroups.length;\n';
  script += '      const start = (currentPage - 1) * PAGE_SIZE;\n';
  script += '      const paginated = sourceTableGroups.slice(start, start + PAGE_SIZE);\n';
  script += '      const totalPages = Math.ceil(sourceTableGroups.length / PAGE_SIZE);\n';
  script += '      let html = \'<table><thead><tr><th>Source Table</th><th>Engines</th><th>Destination Tables</th><th>Patterns</th><th>Convergence</th><th>Operations</th></tr></thead><tbody>\';\n';
  script += '      paginated.forEach(g => {\n';
  script += '        const engines = [...g.engines].map(e => {\n';
  script += '          return engineBadge(e, ENGINE_LABELS[e] || e);\n';
  script += '        }).join(\' \');\n';
  script += '        const destTables = [...g.destTables].join(\', \');\n';
  script += '        const opSummary = {};\n';
  script += '        g.patterns.forEach(p => { opSummary[p.operation] = (opSummary[p.operation] || 0) + 1; });\n';
  script += '        const operations = Object.entries(opSummary).map(function(entry) { return entry[0] + \'(\' + entry[1] + \')\'; }).join(\', \');\n';
  script += '        const convergence = g.convergesFrom.size > 0 ? \'<span class="badge badge-blue">Merged (\' + g.convergesFrom.size + \')</span>\' : \'—\';\n';
  script += '        html += \'<tr onclick="showSourceTableDetails(\' + jsArg(g.table) + \')">\';\n';
  script += '        html += \'<td><span class="link">\' + escapeHtml(g.table) + \'</span></td>\';\n';
  script += '        html += \'<td>\' + engines + \'</td>\';\n';
  script += '        html += \'<td>\' + escapeHtml(destTables) + \'</td>\';\n';
  script += '        html += \'<td><span class="badge badge-grey">\' + g.patterns.length + \'</span></td>\';\n';
  script += '        html += \'<td>\' + convergence + \'</td>\';\n';
  script += '        html += \'<td style="font-size: 12px;">\' + escapeHtml(operations) + \'</td>\';\n';
  script += '        html += \'</tr>\';\n';
  script += '      });\n';
  script += '      html += \'</tbody></table><div class="pagination">\';\n';
  script += '      html += \'<button class="btn" onclick="changePage(-1)" \' + (currentPage === 1 ? \'disabled\' : \'\') + \'>Previous</button>\';\n';
  script += '      html += \'<span>Page \' + currentPage + \' of \' + totalPages + \'</span>\';\n';
  script += '      html += \'<button class="btn" onclick="changePage(1)" \' + (currentPage === totalPages ? \'disabled\' : \'\') + \'>Next</button>\';\n';
  script += '      html += \'</div>\';\n';
  script += '      document.getElementById(\'access-patterns-container\').innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';
  script += '    function changePage(delta) {\n';
  script += '      currentPage += delta;\n';
  script += '      buildTable();\n';
  script += '    }\n';
  script += '\n';
  script += '    function createCharts() {\n';
  script += '      const filtered = filterPatterns();\n';
  script += '      const engineDist = {}, opDist = {};\n';
  script += '      filtered.forEach(p => {\n';
  script += '        engineDist[p.engine] = (engineDist[p.engine] || 0) + 1;\n';
  script += '        opDist[p.opCategory] = (opDist[p.opCategory] || 0) + 1;\n';
  script += '      });\n';
  script += '      if (engineChart) engineChart.destroy();\n';
  script += '      engineChart = new Chart(document.getElementById(\'engineChart\'), {\n';
  script += '        type: \'pie\',\n';
  script += '        data: {\n';
  script += '          labels: Object.keys(engineDist).map(e => ENGINE_LABELS[e] || e),\n';
  script += '          datasets: [{ data: Object.values(engineDist), backgroundColor: Object.keys(engineDist).map(e => engineColor(e)) }]\n';
  script += '        },\n';
  script += '        options: {\n';
  script += '          responsive: true, maintainAspectRatio: false,\n';
  script += '          onClick: (e, items) => {\n';
  script += '            if (items.length > 0) {\n';
  script += '              const index = items[0].index;\n';
  script += '              const engine = Object.keys(engineDist)[index];\n';
  script += '              if (!activeFilters.engines.includes(engine)) {\n';
  script += '                activeFilters.engines.push(engine);\n';
  script += '                updateFilterDisplay();\n';
  script += '                buildTable();\n';
  script += '              }\n';
  script += '            }\n';
  script += '          },\n';
  script += '          plugins: { legend: { position: \'bottom\' } }\n';
  script += '        }\n';
  script += '      });\n';
  script += '      if (operationChart) operationChart.destroy();\n';
  script += '      operationChart = new Chart(document.getElementById(\'operationChart\'), {\n';
  script += '        type: \'doughnut\',\n';
  script += '        data: {\n';
  script += '          labels: Object.keys(opDist).map(o => o.charAt(0).toUpperCase() + o.slice(1)),\n';
  script += '          datasets: [{ data: Object.values(opDist), backgroundColor: Object.keys(opDist).map(o => opColor(o)) }]\n';
  script += '        },\n';
  script += '        options: {\n';
  script += '          responsive: true, maintainAspectRatio: false,\n';
  script += '          onClick: (e, items) => {\n';
  script += '            if (items.length > 0) {\n';
  script += '              const index = items[0].index;\n';
  script += '              const op = Object.keys(opDist)[index];\n';
  script += '              if (!activeFilters.operations.includes(op)) {\n';
  script += '                activeFilters.operations.push(op);\n';
  script += '                updateFilterDisplay();\n';
  script += '                buildTable();\n';
  script += '              }\n';
  script += '            }\n';
  script += '          },\n';
  script += '          plugins: { legend: { position: \'bottom\' } }\n';
  script += '        }\n';
  script += '      });\n';
  script += '    }\n';
  script += '\n';
  script += '    function updateCharts() { createCharts(); }\n';
  script += '\n';
  script += '    function buildCostBreakdown() {\n';
  script += '      const container = document.getElementById(\'cost-breakdown-container\');\n';
  script += '      const costs = DATA.results?.synthesis?.tco_analysis?.cost_breakdown || [];\n';
  script += '      const afterDist = DATA.results?.synthesis?.reality_check?.after_distribution || {};\n';
  // #358: the cache engine (if any) keeps its own cost card even though it
  // owns no workload share -- mirrors cacheLayer.js's isKeptCostEngine and
  // resolveCostBreakdown's cache-engine labeling, duplicated here (as a
  // literal, not a runtime concatenation -- scripts/sync_report_template.py
  // mechanically lifts this function's `script += '...';` lines verbatim and
  // requires each to be its own single-quoted string literal) because this
  // script runs standalone in the browser with no bundler and so can't
  // import that module. KEEP_COST_CARD_EXPR/CACHE_LAYER_SUFFIX (above) hold
  // the same text for ExportReport.cacheLayer.test.js to assert the two
  // haven't drifted apart.
  script += '      const cacheEngine = DATA.results?.synthesis?.cache_overlay?.engine || null;\n';
  script += '      const active = costs.filter(cb => afterDist[cb.database] != null || cb.database === cacheEngine);\n';
  script += '      if (active.length === 0) { container.innerHTML = \'<p>No cost data available.</p>\'; return; }\n';
  script += '      let html = \'<div class="grid grid-auto">\';';
  script += '      active.forEach(cb => {\n';
  script += '        const isCacheLayer = cb.database === cacheEngine;\n';
  script += '        html += \'<div class="stat-card" style="text-align: center;">\';\n';
  script += '        html += engineBadge(cb.database, (ENGINE_LABELS[cb.database] || cb.database) + (isCacheLayer ? " (cache layer)" : \'\'));\n';
  script += '        html += \'<div style="font-size: 36px; font-weight: 700; margin: 8px 0 0; line-height: 1.15;">$\' + (typeof cb.monthly_cost_usd === \'number\' && isFinite(cb.monthly_cost_usd) ? cb.monthly_cost_usd.toFixed(2) : \'0.00\') + \'</div>\';\n';
  script += '        html += \'<div style="font-size: 13px; color: var(--color-text-secondary);">month · \' + escapeHtml(cb.pricing_mode) + \'</div>\';\n';
  script += '        html += \'</div>\';\n';
  script += '      });\n';
  script += '      html += \'</div>\';\n';
  script += '      container.innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';

  // Continue with buildQueryFlow - this is a large function with SVG generation
  script += '    function buildQueryFlow() {\n';
  script += '      const container = document.getElementById(\'query-flow-container\');\n';
  script += '      const afterDist = DATA.results?.synthesis?.reality_check?.after_distribution || {};\n';
  script += '      if (Object.keys(afterDist).length === 0) { container.innerHTML = \'<p>No query flow data available.</p>\'; return; }\n';
  script += '      const totalQueries = Object.values(afterDist).reduce((sum, count) => sum + count, 0);\n';
  script += '      const sortedEngines = Object.entries(afterDist).sort((a, b) => b[1] - a[1]);\n';
  script += '      const svgWidth = 900;\n';
  script += '      const engineSpacing = 100;\n';
  script += '      const svgHeight = Math.max(300, sortedEngines.length * engineSpacing + 100);\n';
  script += '      const nodeWidth = 26;\n';
  script += '      const sourceX = 100;\n';
  script += '      const targetX = svgWidth - 200;\n';
  script += '      const sourceY = svgHeight / 2;\n';
  script += '      const totalHeight = sortedEngines.length * engineSpacing;\n';
  script += '      const startY = (svgHeight - totalHeight) / 2;\n';
  script += '      let html = \'<div style="width: 100%; display: flex; justify-content: center; align-items: center;">\';\n';
  script += '      html += \'<svg width="\' + svgWidth + \'" height="\' + svgHeight + \'" style="background: transparent;">\';\n';
  script += '      html += \'<defs>\';\n';
  script += '      html += \'<linearGradient id="gradient-queries" x1="0%" y1="0%" x2="100%" y2="0%">\';\n';
  script += '      html += \'<stop offset="0%" style="stop-color:var(--chart-neutral);stop-opacity:0.4" />\';\n';
  script += '      html += \'<stop offset="100%" style="stop-color:var(--chart-neutral);stop-opacity:0.2" />\';\n';
  script += '      html += \'</linearGradient>\';\n';
  script += '      sortedEngines.forEach(function(entry) {\n';
  script += '        const engine = entry[0];\n';
  script += '        const color = engineColor(engine);\n';
  script += '        html += \'<linearGradient id="gradient-\' + escapeHtml(engine) + \'" x1="0%" y1="0%" x2="100%" y2="0%">\';\n';
  script += '        html += \'<stop offset="0%" style="stop-color:\' + escapeHtml(color) + \';stop-opacity:0.4" />\';\n';
  script += '        html += \'<stop offset="100%" style="stop-color:\' + escapeHtml(color) + \';stop-opacity:0.2" />\';\n';
  script += '        html += \'</linearGradient>\';\n';
  script += '      });\n';
  script += '      html += \'</defs>\';\n';
  script += '      sortedEngines.forEach(function(entry, idx) {\n';
  script += '        const engine = entry[0], count = entry[1];\n';
  script += '        const targetY = startY + (idx * engineSpacing) + (engineSpacing / 2);\n';
  script += '        const linkHeight = Math.max(8, (count / totalQueries) * 200);\n';
  script += '        const sourceTop = sourceY - linkHeight / 2;\n';
  script += '        const sourceBottom = sourceY + linkHeight / 2;\n';
  script += '        const targetTop = targetY - linkHeight / 2;\n';
  script += '        const targetBottom = targetY + linkHeight / 2;\n';
  script += '        const midX = (sourceX + nodeWidth + targetX) / 2;\n';
  script += '        const pathData = \'M \' + (sourceX + nodeWidth) + \' \' + sourceTop + \' C \' + midX + \' \' + sourceTop + \', \' + midX + \' \' + targetTop + \', \' + targetX + \' \' + targetTop + \' L \' + targetX + \' \' + targetBottom + \' C \' + midX + \' \' + targetBottom + \', \' + midX + \' \' + sourceBottom + \', \' + (sourceX + nodeWidth) + \' \' + sourceBottom + \' Z\';\n';
  script += '        html += \'<path d="\' + pathData + \'" fill="url(#gradient-\' + escapeHtml(engine) + \')" stroke="none" opacity="0.6" />\';\n';
  script += '      });\n';
  script += '      const sourceHeight = Math.min(150, svgHeight - 100);\n';
  script += '      html += \'<rect x="\' + sourceX + \'" y="\' + (sourceY - sourceHeight/2) + \'" width="\' + nodeWidth + \'" height="\' + sourceHeight + \'" style="fill:var(--chart-neutral);stroke:var(--chart-neutral)" rx="2" opacity="0.8" />\';\n';
  script += '      sortedEngines.forEach(function(entry, idx) {\n';
  script += '        const engine = entry[0], count = entry[1];\n';
  script += '        const targetY = startY + (idx * engineSpacing) + (engineSpacing / 2);\n';
  script += '        const nodeHeight = Math.max(20, (count / totalQueries) * 120);\n';
  script += '        const color = engineColor(engine);\n';
  script += '        const label = ENGINE_LABELS[engine] || engine;\n';
  script += '        html += \'<rect x="\' + targetX + \'" y="\' + (targetY - nodeHeight/2) + \'" width="\' + nodeWidth + \'" height="\' + nodeHeight + \'" fill="\' + escapeHtml(color) + \'" stroke="\' + escapeHtml(color) + \'" rx="2" opacity="0.8" />\';\n';
  script += '        html += \'<text x="\' + (targetX - 10) + \'" y="\' + targetY + \'" dy="0.35em" text-anchor="end" font-size="14" font-weight="600" fill="var(--color-text)">\' + escapeHtml(label) + \' (\' + Number(count).toFixed(1) + \'%)</text>\';\n';
  script += '      });\n';
  script += '      html += \'</svg></div>\';\n';
  script += '      container.innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';

  // Trade-offs and the Principal Engineer notes come from the same trade_offs list --
  // the notes are the entries prefixed "[PE note]". They render into two separate
  // containers, so both read the list through tradeoffsByEngine(kind).
  script += '    function tradeoffsByEngine(kind) {\n';
  script += '      const out = {};\n';
  script += '      DATA.schemaDesigns.forEach(d => {\n';
  script += '        const raw = d.content?.trade_offs || [];\n';
  script += '        const normalized = raw.map(t => typeof t === \'object\' ? t : { description: String(t), impact: \'\', source_tables: [], target_tables: [], query_ids: [] });\n';
  script += '        const wanted = normalized.filter(t => String(t.description || \'\').startsWith(\'[PE note]\') === (kind === \'pe\'));\n';
  script += '        if (wanted.length > 0) out[d.target_type] = wanted;\n';
  script += '      });\n';
  script += '      return out;\n';
  script += '    }\n';
  script += '\n';
  script += '    function sqlIdsHtml(queryIds) {\n';
  script += '      if (!queryIds || queryIds.length === 0) return \'\';\n';
  script += '      let out = \'<div style="margin-top: 8px;"><div style="font-size: 11px; color: var(--color-text-secondary); font-weight: 600; margin-bottom: 4px;">SQL IDs:</div><div>\';\n';
  // A journey can be missing (the renderer embeds only the busiest queries when the
  // journey budget applies), so only ids with one are clickable -- the same rule
  // the pattern modal uses.
  script += '      queryIds.forEach(function(qid) {\n';
  script += '        const has = hasJourney(qid);\n';
  script += '        const attrs = has ? \' class="link" onclick="showQueryJourney(\' + jsArg(qid) + \')" title="Click to view query journey"\' : \' title="Query journey not available"\';\n';
  script += '        out += \'<span data-query-id="\' + escapeHtml(qid) + \'"\' + attrs + \' style="font-family: monospace; font-size: 11px; margin-right: 8px; display: inline-block; padding: 2px 6px; background: var(--color-bg-layout); border-radius: 4px;\' + (has ? \'\' : \' opacity: 0.6; cursor: default;\') + \'">\' + escapeHtml(qid.substring(0, 12)) + \'...</span>\';\n';
  script += '      });\n';
  script += '      return out + \'</div></div>\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function engineTabBar(engines, byEngine, btnClass, switchFn) {\n';
  script += '      let out = \'<div style="border-bottom: 2px solid var(--color-border); margin-bottom: 20px;">\';\n';
  script += '      engines.forEach(function(engine, idx) {\n';
  script += '        const activeStyle = idx === 0 ? \'color: var(--color-blue); border-bottom-color: var(--color-blue);\' : \'color: var(--color-text-secondary); border-bottom-color: transparent;\';\n';
  script += '        out += \'<button class="\' + btnClass + (idx === 0 ? \' active\' : \'\') + \'" onclick="\' + switchFn + \'(\' + jsArg(engine) + \')" style="padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; \' + activeStyle + \' margin-bottom: -2px;">\' + escapeHtml(ENGINE_LABELS[engine] || engine) + \' (\' + byEngine[engine].length + \')</button>\';\n';
  script += '      });\n';
  script += '      return out + \'</div>\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildTradeoffs() {\n';
  script += '      const container = document.getElementById(\'tradeoffs-container\');\n';
  script += '      const byEngine = tradeoffsByEngine(\'decision\');\n';
  script += '      const engines = Object.keys(byEngine);\n';
  script += '      if (engines.length === 0) { container.innerHTML = \'<p>No trade-offs available.</p>\'; return; }\n';
  script += '      let html = engineTabBar(engines, byEngine, \'tradeoff-tab-btn\', \'switchTradeoffTab\');\n';
  script += '      engines.forEach(function(engine, idx) {\n';
  script += '        html += \'<div id="tradeoff-tab-\' + escapeHtml(engine) + \'" class="tradeoff-tab-content" style="display: \' + (idx === 0 ? \'block\' : \'none\') + \';">\';\n';
  script += '        byEngine[engine].forEach(function(to) {\n';
  script += '          html += \'<div class="item-card">\';\n';
  script += '          html += \'<div style="font-size: 13px; font-weight: 700; color: var(--color-text);">\' + escapeHtml(to.description) + \'</div>\';\n';
  script += '          if (to.impact) html += \'<div style="font-size: 13px; color: var(--color-text-secondary); margin-top: 4px;">\' + escapeHtml(to.impact) + \'</div>\';\n';
  script += '          html += sqlIdsHtml(to.query_ids);\n';
  script += '          html += \'</div>\';\n';
  script += '        });\n';
  script += '        html += \'</div>\';\n';
  script += '      });\n';
  script += '      container.innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildPeNotes() {\n';
  script += '      const container = document.getElementById(\'pe-notes-container\');\n';
  script += '      if (!container) return;\n';
  script += '      const byEngine = tradeoffsByEngine(\'pe\');\n';
  script += '      const engines = Object.keys(byEngine);\n';
  script += '      if (engines.length === 0) { container.innerHTML = \'<p>No engineering notes were raised.</p>\'; return; }\n';
  script += '      let html = engineTabBar(engines, byEngine, \'pe-tab-btn\', \'switchPeNoteTab\');\n';
  script += '      engines.forEach(function(engine, idx) {\n';
  script += '        html += \'<div id="pe-tab-\' + escapeHtml(engine) + \'" class="pe-tab-content" style="display: \' + (idx === 0 ? \'block\' : \'none\') + \';">\';\n';
  script += '        byEngine[engine].forEach(function(note, noteIdx) {\n';
  script += '          html += \'<div class="item-card">\';\n';
  script += '          html += \'<div style="display: flex; align-items: flex-start; gap: 12px;">\';\n';
  script += '          html += \'<span style="display: inline-block; padding: 2px 8px; background: var(--color-accent); color: white; border-radius: 4px; font-size: 12px; font-weight: 600; min-width: 24px; text-align: center;">\' + (noteIdx + 1) + \'</span>\';\n';
  script += '          html += \'<div style="flex: 1;"><div style="font-size: 13px;">\' + escapeHtml(note.description.replace(/^\\[PE note\\]\\s*/, \'\')) + \'</div>\';\n';
  script += '          if (note.impact) html += \'<div style="font-size: 12px; color: var(--color-text-secondary); margin-top: 4px;">\' + escapeHtml(note.impact) + \'</div>\';\n';
  script += '          html += sqlIdsHtml(note.query_ids);\n';
  script += '          html += \'</div></div></div>\';\n';
  script += '        });\n';
  script += '        html += \'</div>\';\n';
  script += '      });\n';
  script += '      container.innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';
  script += '    function buildMigrationRoadmap() {\n';
  script += '      const container = document.getElementById(\'migration-roadmap-container\');\n';
  script += '      if (!container) return;\n';
  script += '      const waves = DATA.results?.synthesis?.migration_waves || [];\n';
  script += '      if (waves.length === 0) { container.innerHTML = \'<p>No migration roadmap available.</p>\'; return; }\n';
  script += '      let html = \'\';\n';
  script += '      waves.forEach(wave => {\n';
  script += '        html += \'<div class="item-card">\';\n';
  script += '        html += \'<div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">\';\n';
  script += '        html += \'<span class="badge">Wave \' + escapeHtml(wave.wave) + \'</span>\';\n';
  script += '        (wave.engines || []).forEach(engine => { html += engineBadge(engine, ENGINE_LABELS[engine] || engine); });\n';
  script += '        html += \'</div>\';\n';
  script += '        html += \'<div style="font-size: 14px; font-weight: 700; margin-top: 6px;">\' + escapeHtml(wave.title) + \'</div>\';\n';
  script += '        const basis = wave.share_basis === \'calls\' ? \'% of calls\' : \'% of query patterns\';\n';
  script += '        let stats = (wave.query_count || 0) + \' queries · \' + Number(wave.workload_share_percent || 0).toFixed(1) + basis;\n';
  script += '        if (wave.table_count) stats += \' · \' + wave.table_count + \' source tables\';\n';
  script += '        html += \'<div style="font-size: 13px; color: var(--color-text-secondary); margin-top: 2px;">\' + escapeHtml(stats) + \'</div>\';\n';
  script += '        html += \'<div style="font-size: 13px; margin-top: 6px;">\' + escapeHtml(wave.rationale) + \'</div>\';\n';
  script += '        if ((wave.tables || []).length > 0) {\n';
  script += '          const waveTables = wave.tables;\n';
  script += '          const shownTables = waveTables.slice(0, 20).join(\', \');\n';
  script += '          const moreTables = waveTables.length > 20 ? \' (+\' + (waveTables.length - 20) + \' more)\' : \'\';\n';
  script += '          html += \'<div style="font-size: 12px; color: var(--color-text-secondary); margin-top: 6px;"><b>Tables:</b> \' + escapeHtml(shownTables) + escapeHtml(moreTables) + \'</div>\';\n';
  script += '        }\n';
  script += '        if (wave.gate) {\n';
  script += '          html += \'<div style="font-size: 12px; color: var(--color-blue); margin-top: 6px;">Gate before the next wave: \' + escapeHtml(wave.gate) + \'</div>\';\n';
  script += '        }\n';
  script += '        html += \'</div>\';\n';
  script += '      });\n';
  script += '      container.innerHTML = html;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchEngineTab(btnClass, contentClass, showId) {\n';
  script += '      document.querySelectorAll(\'.\' + btnClass).forEach(btn => { btn.style.color = \'var(--color-text-secondary)\'; btn.style.borderBottomColor = \'transparent\'; });\n';
  script += '      event.target.style.color = \'var(--color-blue)\';\n';
  script += '      event.target.style.borderBottomColor = \'var(--color-blue)\';\n';
  script += '      document.querySelectorAll(\'.\' + contentClass).forEach(content => content.style.display = \'none\');\n';
  script += '      document.getElementById(showId).style.display = \'block\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchTradeoffTab(engineId) { switchEngineTab(\'tradeoff-tab-btn\', \'tradeoff-tab-content\', \'tradeoff-tab-\' + engineId); }\n';
  script += '\n';
  script += '    function switchPeNoteTab(engineId) { switchEngineTab(\'pe-tab-btn\', \'pe-tab-content\', \'pe-tab-\' + engineId); }\n';
  script += '\n';

  // Due to the massive size of showPatternDetails, showSourceTableDetails, and showQueryJourney,
  // I'll add simplified versions that handle the core functionality
  // These functions are extremely large with many nested template literals

  script += '    function showPatternDetails(patternId) {\n';
  script += '      const pattern = allPatterns.find(p => p.id === patternId);\n';
  script += '      if (!pattern) { alert(\'Pattern not found: \' + patternId); return; }  // nosemgrep: javascript-alert -- intentional user notice in standalone exported report\n';
  script += '      let fullPattern = null;\n';
  script += '      DATA.schemaDesigns.forEach(design => {\n';
  script += '        const content = design.content || {};\n';
  script += '        (content.access_patterns || []).forEach(ap => {\n';
  script += '          const id = ap.pattern_id || ap.name || (design.target_type + \'-\' + ap.operation);\n';
  script += '          if (id === patternId) fullPattern = Object.assign({}, ap, { engine: design.target_type });\n';
  script += '        });\n';
  script += '      });\n';
  script += '      if (!fullPattern) fullPattern = pattern;\n';
  script += '      let modal = document.getElementById(\'pattern-modal\');\n';
  script += '      if (!modal) {\n';
  script += '        modal = document.createElement(\'div\');\n';
  script += '        modal.id = \'pattern-modal\';\n';
  script += '        modal.className = \'modal-overlay\';\n';
  script += '        modal.innerHTML = \'<div class="modal-content"><div class="modal-header"><h2 id="pattern-modal-title">Pattern Details</h2><button class="modal-close" onclick="closePatternModal()">×</button></div><div class="modal-body" id="pattern-modal-body"></div></div>\';\n';
  script += '        document.body.appendChild(modal);\n';
  script += '      }\n';
  script += '      document.getElementById(\'pattern-modal-title\').textContent = \'Pattern: \' + patternId.slice(0, 12);\n';
  script += '      let tabsHtml = \'<div class="tab-bar">\';\n';
  script += '      tabsHtml += \'<button class="tab-button active" onclick="switchPatternTab(\\\'overview\\\')">Overview</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-button" onclick="switchPatternTab(\\\'details\\\')">Details</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-button" onclick="switchPatternTab(\\\'source\\\')">Source Query</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-button" onclick="switchPatternTab(\\\'target\\\')">Target Pattern</button>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="pattern-tab-overview" class="tab-content active">\';\n';
  script += '      tabsHtml += \'<div style="margin-bottom: 24px;"><div class="key-value-label">Description</div>\';\n';
  script += '      tabsHtml += \'<div class="key-value-value" style="margin-top: 4px; padding: 16px; background: var(--color-bg-layout); border-radius: 8px;">\' + escapeHtml(fullPattern.description || fullPattern.name || \'No description available\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div class="key-value-grid">\';\n';
  script += '      tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Engine</div><div class="key-value-value">\' + engineBadge(fullPattern.engine, ENGINE_LABELS[fullPattern.engine] || fullPattern.engine) + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Operation</div><div class="key-value-value">\' + escapeHtml(fullPattern.operation || fullPattern.http_method || \'—\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Destination Table</div><div class="key-value-value">\' + escapeHtml(fullPattern.table_name || fullPattern.key_pattern || fullPattern.index_or_stream || fullPattern.index || fullPattern.collection || \'—\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      if (fullPattern.source_tables && fullPattern.source_tables.length > 0) {\n';
  script += '        tabsHtml += \'<div class="key-value-block"><div class="key-value-label">Source Tables</div><div>\';\n';
  script += '        fullPattern.source_tables.forEach(function(t) {\n';
  script += '          tabsHtml += \'<span class="badge badge-grey">\' + escapeHtml(t.split(\'.\').pop()) + \'</span> \';\n';
  script += '        });\n';
  script += '        tabsHtml += \'</div></div>\';\n';
  script += '      }\n';
  script += '      if (fullPattern.query_ids && fullPattern.query_ids.length > 0) {\n';
  script += '        tabsHtml += \'<div class="key-value-block"><div class="key-value-label">SQL IDs (\' + fullPattern.query_ids.length + \')</div><div style="display: flex; flex-wrap: wrap; gap: 8px;">\';\n';
  script += '        fullPattern.query_ids.forEach(function(qid) {\n';
  script += '          const journeyAvailable = hasJourney(qid);\n';
  script += '          const cursorStyle = journeyAvailable ? \'cursor: pointer;\' : \'opacity: 0.6;\';\n';
  script += '          const onclickAttr = journeyAvailable ? \' onclick="showQueryJourney(\' + jsArg(qid) + \')"\' : \'\';\n';
  script += '          const titleAttr = journeyAvailable ? \'Click to view query journey\' : \'Query journey not available\';\n';
  script += '          tabsHtml += \'<span class="badge badge-blue" style="\' + cursorStyle + \'" title="\' + titleAttr + \'"\' + onclickAttr + \'>\' + escapeHtml(qid.slice(0, 8)) + \'...</span>\';\n';
  script += '        });\n';
  script += '        tabsHtml += \'</div></div>\';\n';
  script += '      }\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="pattern-tab-details" class="tab-content"><div class="key-value-grid">\';\n';
  script += '      if (fullPattern.gsi_name) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">GSI Name</div><div class="key-value-value">\' + escapeHtml(fullPattern.gsi_name) + \'</div></div>\';\n';
  script += '      if (fullPattern.partition_key) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Partition Key</div><div class="key-value-value">\' + escapeHtml(fullPattern.partition_key) + \'</div></div>\';\n';
  script += '      if (fullPattern.sort_key) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Sort Key</div><div class="key-value-value">\' + escapeHtml(fullPattern.sort_key) + \'</div></div>\';\n';
  script += '      if (fullPattern.filter_expression) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Filter Expression</div><div class="key-value-value">\' + escapeHtml(fullPattern.filter_expression) + \'</div></div>\';\n';
  script += '      if (fullPattern.projection) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Projection</div><div class="key-value-value">\' + escapeHtml(fullPattern.projection) + \'</div></div>\';\n';
  script += '      if (fullPattern.consistency) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Consistency</div><div class="key-value-value">\' + escapeHtml(fullPattern.consistency) + \'</div></div>\';\n';
  script += '      if (fullPattern.estimated_rps != null) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Estimated RPS</div><div class="key-value-value">\' + escapeHtml(String(fullPattern.estimated_rps)) + \'</div></div>\';\n';
  script += '      if (fullPattern.estimated_item_size != null) tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Estimated Item Size</div><div class="key-value-value">\' + escapeHtml(String(fullPattern.estimated_item_size)) + \' bytes</div></div>\';\n';
  script += '      tabsHtml += \'</div></div>\';\n';
  script += '      tabsHtml += \'<div id="pattern-tab-source" class="tab-content">\';\n';
  script += '      if (fullPattern.source_query) {\n';
  script += '        tabsHtml += \'<div><div class="key-value-label">Source SQL Query</div><div class="code-block">\' + escapeHtml(fullPattern.source_query) + \'</div></div>\';\n';
  script += '      } else {\n';
  script += '        tabsHtml += \'<p style="color: var(--color-text-secondary);">No source query available for this pattern.</p>\';\n';
  script += '      }\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="pattern-tab-target" class="tab-content">\';\n';
  script += '      if (fullPattern.key_condition) tabsHtml += \'<div><div class="key-value-label">Key Condition Expression</div><div class="code-block">\' + escapeHtml(fullPattern.key_condition) + \'</div></div>\';\n';
  script += '      if (fullPattern.dsl_query) {\n';
  script += '        tabsHtml += \'<div class="key-value-block"><div class="key-value-label">OpenSearch DSL Query</div><div class="code-block">\' + (typeof fullPattern.dsl_query === \'string\' ? escapeHtml(fullPattern.dsl_query) : escapeHtml(JSON.stringify(fullPattern.dsl_query, null, 2))) + \'</div></div>\';\n';
  script += '      }\n';
  script += '      if (!fullPattern.key_condition && !fullPattern.dsl_query) tabsHtml += \'<p style="color: var(--color-text-secondary);">No target pattern details available.</p>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      document.getElementById(\'pattern-modal-body\').innerHTML = tabsHtml;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '      modal.style.display = \'flex\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function closePatternModal() {\n';
  script += '      const modal = document.getElementById(\'pattern-modal\');\n';
  script += '      if (modal) modal.style.display = \'none\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchPatternTab(tabName) {\n';
  script += '      document.querySelectorAll(\'#pattern-modal .tab-button\').forEach(btn => btn.classList.remove(\'active\'));\n';
  script += '      event.target.classList.add(\'active\');\n';
  script += '      document.querySelectorAll(\'#pattern-modal .tab-content\').forEach(content => content.classList.remove(\'active\'));\n';
  script += '      document.getElementById(\'pattern-tab-\' + tabName).classList.add(\'active\');\n';
  script += '    }\n';
  script += '\n';
  script += '    function showSourceTableDetails(tableName) {\n';
  script += '      const group = sourceTableGroups.find(g => g.table === tableName);\n';
  script += '      if (!group) { alert(\'Source table not found: \' + tableName); return; }  // nosemgrep: javascript-alert -- intentional user notice in standalone exported report\n';
  script += '      let modal = document.getElementById(\'source-table-modal\');\n';
  script += '      if (!modal) {\n';
  script += '        modal = document.createElement(\'div\');\n';
  script += '        modal.id = \'source-table-modal\';\n';
  script += '        modal.className = \'modal-overlay\';\n';
  script += '        modal.innerHTML = \'<div class="modal-content"><div class="modal-header"><h2 id="source-table-modal-title">Source Table Details</h2><button class="modal-close" onclick="closeSourceTableModal()">×</button></div><div class="modal-body" id="source-table-modal-body"></div></div>\';\n';
  script += '        document.body.appendChild(modal);\n';
  script += '      }\n';
  script += '      document.getElementById(\'source-table-modal-title\').textContent = \'Source Table: \' + tableName;\n';
  script += '      const byEngine = {};\n';
  script += '      group.patterns.forEach(ap => {\n';
  script += '        if (!byEngine[ap.engine]) byEngine[ap.engine] = [];\n';
  script += '        byEngine[ap.engine].push(ap);\n';
  script += '      });\n';
  script += '      let tabsHtml = \'<div class="tab-bar">\';\n';
  script += '      Object.keys(byEngine).forEach(function(engine, idx) {\n';
  script += '        const activeClass = idx === 0 ? \' active\' : \'\';\n';
  script += '        tabsHtml += \'<button class="tab-button\' + activeClass + \'" onclick="switchSourceTableTab(\' + jsArg(engine) + \')">\';\n';
  script += '        tabsHtml += escapeHtml(ENGINE_LABELS[engine] || engine) + \' (\' + byEngine[engine].length + \')\';\n';
  script += '        tabsHtml += \'</button>\';\n';
  script += '      });\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      Object.entries(byEngine).forEach(function(entry, idx) {\n';
  script += '        const engine = entry[0], patterns = entry[1];\n';
  script += '        const byDest = {};\n';
  script += '        patterns.forEach(ap => {\n';
  script += '          const key = ap.destTable;\n';
  script += '          if (!byDest[key]) byDest[key] = [];\n';
  script += '          byDest[key].push(ap);\n';
  script += '        });\n';
  script += '        const displayStyle = idx === 0 ? \'block\' : \'none\';\n';
  script += '        const activeClass = idx === 0 ? \' active\' : \'\';\n';
  script += '        tabsHtml += \'<div id="source-table-tab-\' + escapeHtml(engine) + \'" class="tab-content\' + activeClass + \'" style="display: \' + displayStyle + \';">\';\n';
  script += '        tabsHtml += \'<div style="margin-bottom: 16px;"><div class="key-value-grid">\';\n';
  script += '        tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Source Table</div><div class="key-value-value">\' + escapeHtml(tableName) + \'</div></div>\';\n';
  script += '        tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Target Engine</div><div class="key-value-value">\' + engineBadge(engine, ENGINE_LABELS[engine] || engine) + \'</div></div>\';\n';
  script += '        tabsHtml += \'<div class="key-value-item"><div class="key-value-label">Total Patterns</div><div class="key-value-value"><span class="badge badge-grey">\' + patterns.length + \'</span></div></div>\';\n';
  script += '        tabsHtml += \'</div></div>\';\n';
  script += '        Object.entries(byDest).forEach(function(destEntry) {\n';
  script += '          const destTable = destEntry[0], destPatterns = destEntry[1];\n';
  script += '          tabsHtml += \'<div style="margin-bottom: 24px; padding: 20px; background: var(--color-bg-layout); border-radius: 8px;">\';\n';
  script += '          tabsHtml += \'<div class="key-value-label">Destination: \' + escapeHtml(destTable) + \'</div>\';\n';
  script += '          tabsHtml += \'<div style="color: var(--color-text-secondary); margin-bottom: 8px;">\' + destPatterns.length + \' pattern(s)</div>\';\n';
  script += '          tabsHtml += \'<table><thead><tr>\';\n';
  script += '          tabsHtml += \'<th style="text-align: left; padding: 4px 8px;">Pattern ID</th>\';\n';
  script += '          tabsHtml += \'<th style="text-align: left; padding: 4px 8px;">Operation</th>\';\n';
  script += '          tabsHtml += \'<th style="text-align: left; padding: 4px 8px;">Description</th>\';\n';
  script += '          tabsHtml += \'</tr></thead><tbody>\';\n';
  script += '          destPatterns.forEach(function(p) {\n';
  script += '            tabsHtml += \'<tr onclick="closeSourceTableModal(); showPatternDetails(\' + jsArg(p.id) + \');" style="cursor: pointer;">\';\n';
  script += '            tabsHtml += \'<td style="padding: 4px 8px;"><span class="link">\' + escapeHtml(p.id.slice(0, 12)) + \'</span></td>\';\n';
  script += '            tabsHtml += \'<td style="padding: 4px 8px;">\' + escapeHtml(p.operation) + \'</td>\';\n';
  script += '            tabsHtml += \'<td style="padding: 4px 8px;">\' + escapeHtml(p.description) + \'</td>\';\n';
  script += '            tabsHtml += \'</tr>\';\n';
  script += '          });\n';
  script += '          tabsHtml += \'</tbody></table></div>\';\n';
  script += '        });\n';
  script += '        tabsHtml += \'</div>\';\n';
  script += '      });\n';
  script += '      document.getElementById(\'source-table-modal-body\').innerHTML = tabsHtml;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '      modal.style.display = \'flex\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function closeSourceTableModal() {\n';
  script += '      const modal = document.getElementById(\'source-table-modal\');\n';
  script += '      if (modal) modal.style.display = \'none\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchSourceTableTab(engineId) {\n';
  script += '      document.querySelectorAll(\'#source-table-modal .tab-button\').forEach(btn => btn.classList.remove(\'active\'));\n';
  script += '      event.target.classList.add(\'active\');\n';
  script += '      document.querySelectorAll(\'#source-table-modal .tab-content\').forEach(content => content.classList.remove(\'active\'));\n';
  script += '      document.getElementById(\'source-table-tab-\' + engineId).classList.add(\'active\');\n';
  script += '    }\n';
  script += '\n';
  script += '    function showQueryJourney(queryId) {\n';
  script += '      const journey = hasJourney(queryId) ? QUERY_JOURNEY_LOOKUP[queryId] : null;\n';
  script += '      let modal = document.getElementById(\'query-journey-modal\');\n';
  script += '      if (!modal) {\n';
  script += '        modal = document.createElement(\'div\');\n';
  script += '        modal.id = \'query-journey-modal\';\n';
  script += '        modal.style.cssText = \'display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center;\';\n';
  script += '        modal.innerHTML = \'<div class="modal-content"><div class="modal-header"><h2 id="query-modal-title">Query Journey</h2><button class="modal-close" onclick="closeQueryJourneyModal()">×</button></div><div class="modal-body" id="query-modal-body"></div></div>\';\n';
  script += '        document.body.appendChild(modal);\n';
  script += '      }\n';
  script += '      document.getElementById(\'query-modal-title\').textContent = \'Query Journey: \' + queryId.substring(0, 16) + \'...\';\n';
  // Missing journey: say so inside the modal rather than with a blocking alert().
  script += '      if (!journey) {\n';
  script += '        document.getElementById(\'query-modal-body\').innerHTML = \'<p style="color: var(--color-text-secondary);">The query journey for <code>\' + escapeHtml(queryId) + \'</code> is not embedded in this report.</p>\';  // nosemgrep: insecure-innerhtml,insecure-document-method -- value HTML-escaped via escapeHtml()\n';
  script += '        modal.style.display = \'flex\';\n';
  script += '        return;\n';
  script += '      }\n';
  script += '      const source = journey.source || {};\n';
  script += '      const assignment = journey.assignment || {};\n';
  script += '      const performance = source.performance || {};\n';
  script += '      const characteristics = source.characteristics || {};\n';
  script += '      let tabsHtml = \'<div style="border-bottom: 2px solid var(--color-border); margin-bottom: 16px;">\';\n';
  script += '      tabsHtml += \'<button class="tab-btn active" onclick="switchQueryTab(\\\'general\\\')" style="padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; color: var(--color-blue); border-bottom: 2px solid var(--color-blue); margin-bottom: -2px;">General Information</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-btn" onclick="switchQueryTab(\\\'performance\\\')" style="padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; color: var(--color-text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px;">Performance</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-btn" onclick="switchQueryTab(\\\'characteristics\\\')" style="padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; color: var(--color-text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px;">Characteristics</button>\';\n';
  script += '      tabsHtml += \'<button class="tab-btn" onclick="switchQueryTab(\\\'json\\\')" style="padding: 8px 24px; cursor: pointer; border: none; background: none; font-size: 14px; font-weight: 600; color: var(--color-text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px;">JSON</button>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="tab-general" class="tab-content" style="display: block;">\';\n';
  script += '      tabsHtml += \'<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 16px;">\';\n';
  script += '      tabsHtml += \'<div><div class="key-value-label">Query Type</div><div>\' + escapeHtml(source.query_type || \'—\') + \'</div></div>\';\n';
  // #296 cache overlay: a cached read keeps assigned_engine as its real owner;
  // cache_engine (e.g. "elasticache") just flags that it is also served from a
  // cache layer, so it is shown as a small secondary line, never as the engine.
  script += '      tabsHtml += \'<div><div class="key-value-label">Assigned Engine</div><div><span class="badge badge-blue">\' + escapeHtml(assignment.assigned_engine || \'—\') + \'</span>\' + (assignment.cache_engine ? (\'<div style="margin-top: 4px; font-size: 12px; font-style: italic; color: var(--color-text-secondary);">Cached by \' + escapeHtml(ENGINE_LABELS[assignment.cache_engine] || assignment.cache_engine) + \'</div>\') : \'\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div><div class="key-value-label">Confidence</div><div>\' + escapeHtml(String(assignment.confidence || \'—\')) + \'%</div></div>\';\n';
  script += '      tabsHtml += \'<div><div class="key-value-label">Frequency (per hour)</div><div>\' + (source.frequency_per_hour ? source.frequency_per_hour.toFixed(2) : \'—\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div><div class="key-value-label">Calls per Second</div><div>\' + (source.calls_per_second ? source.calls_per_second.toFixed(4) : \'—\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'<div><div class="key-value-label">In Scope</div><div>\' + (assignment.in_scope ? \'Yes\' : \'No\') + \'</div></div>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      if (source.tables_accessed && source.tables_accessed.length > 0) {\n';
  script += '        tabsHtml += \'<div class="key-value-block"><div class="key-value-label">Tables Accessed</div><div>\';\n';
  script += '        source.tables_accessed.forEach(function(t) { tabsHtml += \'<span class="badge badge-grey">\' + escapeHtml(t) + \'</span> \'; });\n';
  script += '        tabsHtml += \'</div></div>\';\n';
  script += '      }\n';
  script += '      if (source.query_text) {\n';
  script += '        tabsHtml += \'<div class="key-value-block"><div class="key-value-label">Query Text</div>\';\n';
  script += '        tabsHtml += \'<div class="code-block">\' + escapeHtml(source.query_text) + \'</div></div>\';\n';
  script += '      }\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="tab-performance" class="tab-content" style="display: none;">\';\n';
  script += '      if (Object.keys(performance).length > 0) {\n';
  script += '        tabsHtml += \'<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;">\';\n';
  script += '        Object.entries(performance).forEach(function(entry) {\n';
  script += '          const key = entry[0], value = entry[1];\n';
  script += '          const label = key.replace(/_/g, \' \').replace(/\\b\\w/g, function(l) { return l.toUpperCase(); });\n';
  script += '          const displayValue = value !== null && value !== undefined ? (typeof value === \'number\' ? value.toFixed(4) : escapeHtml(String(value))) : \'—\';\n';
  script += '          tabsHtml += \'<div><div class="key-value-label">\' + escapeHtml(label) + \'</div><div>\' + displayValue + \'</div></div>\';\n';
  script += '        });\n';
  script += '        tabsHtml += \'</div>\';\n';
  script += '      } else {\n';
  script += '        tabsHtml += \'<p style="color: var(--color-text-secondary);">No performance data available</p>\';\n';
  script += '      }\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="tab-characteristics" class="tab-content" style="display: none;">\';\n';
  script += '      if (Object.keys(characteristics).length > 0) {\n';
  script += '        tabsHtml += \'<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;">\';\n';
  script += '        Object.entries(characteristics).forEach(function(entry) {\n';
  script += '          const key = entry[0], value = entry[1];\n';
  script += '          const label = key.replace(/_/g, \' \').replace(/\\b\\w/g, function(l) { return l.toUpperCase(); });\n';
  script += '          const displayValue = value !== null && value !== undefined ? escapeHtml(String(value)) : \'—\';\n';
  script += '          tabsHtml += \'<div><div class="key-value-label">\' + escapeHtml(label) + \'</div><div>\' + displayValue + \'</div></div>\';\n';
  script += '        });\n';
  script += '        tabsHtml += \'</div>\';\n';
  script += '      } else {\n';
  script += '        tabsHtml += \'<p style="color: var(--color-text-secondary);">No characteristics data available</p>\';\n';
  script += '      }\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      tabsHtml += \'<div id="tab-json" class="tab-content" style="display: none;">\';\n';
  script += '      tabsHtml += \'<div class="code-block" style="white-space: pre;">\' + escapeHtml(JSON.stringify(journey, null, 2)) + \'</div>\';\n';
  script += '      tabsHtml += \'</div>\';\n';
  script += '      document.getElementById(\'query-modal-body\').innerHTML = tabsHtml;  // nosemgrep: insecure-innerhtml,insecure-document-method -- values HTML-escaped via escapeHtml()\n';
  script += '      modal.style.display = \'flex\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function closeQueryJourneyModal() {\n';
  script += '      const modal = document.getElementById(\'query-journey-modal\');\n';
  script += '      if (modal) modal.style.display = \'none\';\n';
  script += '    }\n';
  script += '\n';
  script += '    function switchQueryTab(tabName) {\n';
  script += '      document.querySelectorAll(\'.tab-btn\').forEach(btn => { btn.style.color = \'var(--color-text-secondary)\'; btn.style.borderBottomColor = \'transparent\'; });\n';
  script += '      event.target.style.color = \'var(--color-blue)\';\n';
  script += '      event.target.style.borderBottomColor = \'var(--color-blue)\';\n';
  script += '      document.querySelectorAll(\'.tab-content\').forEach(content => content.style.display = \'none\');\n';
  script += '      document.getElementById(\'tab-\' + tabName).style.display = \'block\';\n';
  script += '    }\n';
  script += '\n';
  script += '    window.addEventListener(\'click\', (event) => {\n';
  script += '      const queryModal = document.getElementById(\'query-journey-modal\');\n';
  script += '      if (queryModal && event.target === queryModal) closeQueryJourneyModal();\n';
  script += '      const patternModal = document.getElementById(\'pattern-modal\');\n';
  script += '      if (patternModal && event.target === patternModal) closePatternModal();\n';
  script += '      const sourceTableModal = document.getElementById(\'source-table-modal\');\n';
  script += '      if (sourceTableModal && event.target === sourceTableModal) closeSourceTableModal();\n';
  script += '    });\n';
  script += '\n';
  script += '    document.getElementById(\'filter-input\').addEventListener(\'input\', (e) => {\n';
  script += '      activeFilters.text = e.target.value;\n';
  script += '      currentPage = 1;\n';
  script += '      buildTable();\n';
  script += '    });\n';
  script += '\n';
  script += '    document.addEventListener(\'DOMContentLoaded\', () => {\n';
  script += '      allPatterns = extractPatterns();\n';
  script += '      buildMigrationRoadmap();\n';
  script += '      buildCostBreakdown();\n';
  script += '      buildQueryFlow();\n';
  script += '      buildTable();\n';
  script += '      createCharts();\n';
  script += '      buildTradeoffs();\n';
  script += '      buildPeNotes();\n';
  script += '    });\n';
  script += '  </script>\n';

  return script;
};

export const generateHTMLReport = (data) => {
  const { results, schemaDesigns, collector, jobId, exportDate, queryJourneys } = data;
  const afterDist = results?.synthesis?.reality_check?.after_distribution || {};

  // #296 cache overlay: synthesis.cache_overlay (same shape as the synthesis
  // report's top-level field) describes ElastiCache as a cache layer, never a
  // workload share. This export is i18n-exempt (standalone, English-only), so
  // the English default text from formatCacheLayerLine is used directly.
  const cacheOverlay = getCacheOverlay(results?.synthesis);

  // #358: "Target Engines" still names the cache layer -- it owns no
  // queries, so afterDist alone would leave it out entirely -- labeled
  // "<Name> (cache layer)" instead of a plain owner badge. Generated with DOM
  // methods (not template-string interpolation) to avoid Semgrep warnings.
  const engineBadges = targetEngineEntries(afterDist, cacheOverlay)
    .map(({ engine, isCacheLayer }) =>
      '<span class="badge" data-engine="' + escapeHtml(engine) + '">'
      + escapeHtml((ENGINE_LABELS[engine] || engine) + (isCacheLayer ? CACHE_LAYER_SUFFIX : ''))
      + '</span>')
    .join('');

  // Shown as its own stat below the Target Engines badges, not folded into them.
  const cacheLayerStat = cacheOverlay
    ? '<div class="stat-card"><div class="stat-label">Cache Layer</div>'
      + '<div class="stat-value" style="font-size: 14px;">' + escapeHtml(formatCacheLayerLine(cacheOverlay)) + '</div></div>'
    : '';

  // Untrusted report.json text in the shell, escaped once here. The ATX renderer
  // (src/report/analysis_report.py) fills the same slots with escaping.html_text.
  const safeJobId = escapeHtml(jobId);
  const safeDatabaseName = escapeHtml(results?.synthesis?.database_name || 'N/A');
  const safeDatabaseNameStat = escapeHtml(results?.synthesis?.database_name || '—');
  const safeSummary = escapeHtml(results?.synthesis?.summary || 'No summary available.');
  const safeExportDate = escapeHtml(new Date(exportDate).toLocaleString());

  // Only finite numbers are summed/counted: a string monthly_cost_usd would turn the
  // sum into string concatenation (and .toFixed into a TypeError that aborts the
  // export), and a non-array access_patterns with a hostile "length" would land in
  // the markup. Matches the Python renderer's numeric filtering.
  const finite = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  // #358: the headline total is the report's own tco_analysis.projected_monthly_cost
  // -- one source of truth, matching the decision report and chat -- never a sum
  // recomputed from a filtered subset. Falls back to summing the kept cards
  // (owners + cache layer) only when that field is missing (older artifacts).
  const { total: projectedCostValue } = resolveCostBreakdown(
    results?.synthesis?.tco_analysis, afterDist, cacheOverlay
  );
  const projectedCost = escapeHtml(finite(projectedCostValue).toFixed(2));
  // #405: the cache layer gets its own schema design (and its own
  // access_patterns) too, but those patterns were never queries it owns --
  // counting them here double-counted ElastiCache as if it were an owner engine
  // in the "Access Patterns" stat and the explorer's total/pie/rows, the same
  // gap #361 fixed on the Results page. ownerSchemaDesigns drops it;
  // cacheAccessPatternCount (below) tracks it separately so the report can
  // still say how many there are, just not as owned patterns. Both are no-ops
  // without a cache_overlay: a legacy report (no cache_overlay at all) has
  // ElastiCache as a real owner, so its design's patterns stay in the owner
  // total, matching src/report/analysis_report.py (which only strips a design
  // when cache_overlay.engine names it) and the Results page.
  const safeSchemaDesigns = Array.isArray(schemaDesigns) ? schemaDesigns : [];
  const hasCacheOverlay = !!cacheOverlay;
  const totalPatterns = escapeHtml(ownerSchemaDesigns(safeSchemaDesigns, hasCacheOverlay).reduce(
    (sum, d) => sum + (Array.isArray(d?.content?.access_patterns) ? d.content.access_patterns.length : 0), 0));
  // #405: shown as its own note under the "Access Patterns" stat, worded like
  // the Results page's "+N cache-layer pattern(s)" caption
  // (analysis-results-v2.executive-summary.cache-patterns-note in
  // locales/en.json). This export is i18n-exempt (standalone, English-only --
  // see file header), so the English singular/plural text is inlined directly
  // instead of going through i18next's t().
  const cachePatternCount = cacheAccessPatternCount(safeSchemaDesigns, hasCacheOverlay);
  const cachePatternsNote = cachePatternCount > 0
    ? '<div class="stat-note">+' + escapeHtml(cachePatternCount) + ' cache-layer pattern'
      + (cachePatternCount === 1 ? '' : 's') + ' (not counted above)</div>'
    : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Analysis Report - ${escapeHtml(jobId)}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>${REPORT_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="section">
      <div class="report-head">
        <div>
          <h1>Database Modernization Analysis Report</h1>
          <p class="section-desc">Review the AWS database engine recommended for each access pattern in your source database, the estimated cost of running it, and the trade-offs each design accepts.</p>
        </div>
        <div class="report-brand">
          <svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="AWS Transform"><rect width="80" height="80" rx="16" style="fill: var(--color-brand)"/><path fill="white" d="M62.9961 29.5693C62.3662 29.207 61.6182 29.2099 60.9922 29.5761L51.4961 35.1172C51.1885 35.2964 51 35.6255 51 35.9809V42.3628L47.5527 44.0864C47.2139 44.2558 47 44.602 47 44.9809V48.4809L44 50.7309L41 48.4814V31.4805L44 29.2309L47 31.4809V35.3809L49 34.2309V30.9809C49 30.666 48.8516 30.3696 48.5996 30.1811L44.5996 27.1811C44.2441 26.9145 43.7559 26.9145 43.4004 27.1811L40 29.7309L36.5996 27.1811C36.2695 26.9321 35.8213 26.9141 35.4697 27.1328L31.4697 29.6328C31.1777 29.8159 31 30.1362 31 30.4809V34.3628L27.5527 36.0864C27.2139 36.2558 27 36.602 27 36.9809V43.3872L18 48.2964V28.791C18 27.4048 18.7363 26.0952 19.9199 25.374L37.7285 14.5342C39.0859 13.7065 40.8037 13.7637 42.1035 14.6738L56.8277 24.981H52V26.981H60C60.5527 26.981 61 26.5332 61 25.981V17.981H59V24.0604L43.25 13.0352C41.2988 11.669 38.7236 11.5869 36.6894 12.8257L18.8808 23.666C17.1035 24.7476 16 26.7114 16 28.791V48.2964C16 49.0093 16.3662 49.6524 16.9785 50.0161C17.2959 50.2046 17.6475 50.2988 17.999 50.2988C18.3271 50.2988 18.6553 50.2168 18.957 50.0522L28.4785 44.8589C28.7998 44.6836 29 44.3467 29 43.981V37.5991L32.4473 35.8755C32.7861 35.7061 33 35.3599 33 34.981V31.0352L35.9482 29.1924L39 31.4805V48.4814L36 50.731L33 48.481V44.581L31 45.681V48.981C31 49.2959 31.1484 49.5923 31.4004 49.7808L35.4004 52.7808C35.7558 53.0474 36.2441 53.0474 36.5996 52.7808L40 50.231L43.4004 52.7808C43.7558 53.0474 44.2441 53.0474 44.5996 52.7808L48.5996 49.7808C48.8515 49.5923 49 49.2959 49 48.981V45.5991L52.4473 43.8755C52.7861 43.7061 53 43.3599 53 42.981V36.5552L62 31.3037V51.1709C62 52.5571 61.2637 53.8667 60.0801 54.5879L42.2715 65.4277C40.9131 66.2534 39.1963 66.1982 37.8965 65.2881L23.1722 54.981H28V52.981H20C19.4473 52.981 19 53.4287 19 53.981V61.981H21V55.9016L36.75 66.9268C37.7803 67.6479 38.9844 68.0112 40.1914 68.0112C41.2695 68.0112 42.3506 67.7207 43.3105 67.1362L61.1191 56.2959C62.8965 55.2144 64 53.2505 64 51.1709V31.3037C64 30.5786 63.625 29.9301 62.9961 29.5693Z"/></svg>
          <span class="badge badge-blue">Generated by AWS Transform</span>
        </div>
      </div>
      <div class="meta-pairs">
        <div class="meta-pair">
          <div class="meta-pair-label">Job ID</div>
          <div class="meta-pair-value">${safeJobId}</div>
        </div>
        <div class="meta-pair">
          <div class="meta-pair-label">Created</div>
          <div class="meta-pair-value">${safeExportDate}</div>
        </div>
        <div class="meta-pair">
          <div class="meta-pair-label">Database</div>
          <div class="meta-pair-value">${safeDatabaseName}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header">Executive Summary</div>
      <p class="section-desc">The target architecture recommended for this database, its projected monthly cost, and the workload it covers.</p>
      <p class="section-body">${safeSummary}</p>
      <div class="grid grid-4">
        <div class="stat-card"><div class="stat-label">Database</div><div class="stat-value">${safeDatabaseNameStat}</div></div>
        <div class="stat-card"><div class="stat-label">Target Engines</div><div class="stat-value">${engineBadges}</div></div>
        <div class="stat-card"><div class="stat-label">Projected Cost</div><div class="stat-value">$${projectedCost}/mo</div></div>
        <div class="stat-card"><div class="stat-label">Access Patterns</div><div class="stat-value">${totalPatterns}</div>${cachePatternsNote}</div>
        ${cacheLayerStat}
      </div>
    </div>

    <div class="section">
      <div class="section-header">Migration Roadmap</div>
      <p class="section-desc">The incremental migration waves computed from this assessment. One suggested adoption path, not the only one — to modernize in one step instead, adopt the target architecture in Executive Summary above directly.</p>
      <div id="migration-roadmap-container"></div>
    </div>

    <div class="section">
      <div class="section-header">Cost Breakdown</div>
      <p class="section-desc">Estimated monthly cost of running each recommended engine at your current workload volume.</p>
      <div id="cost-breakdown-container"></div>
    </div>

    <div class="section">
      <div class="section-header">Query Flow</div>
      <p class="section-desc">How your access patterns distribute across the recommended engines, from source table to target.</p>
      <div id="query-flow-container"></div>
    </div>

    <div class="section">
      <div class="section-header">Access Pattern Explorer (<span id="pattern-count">${totalPatterns}</span>)</div>
      <p class="section-desc">Browse every access pattern by pattern or by source table. Filter by engine, operation, or text to narrow the list, then select a row to see its target design.</p>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <div class="toggle-group">
          <button class="toggle-btn active" onclick="switchBrowseMode('pattern')">By access pattern</button>
          <button class="toggle-btn" onclick="switchBrowseMode('source')">By source table</button>
        </div>
      </div>
      <div class="filter-bar">
        <input type="text" id="filter-input" class="filter-input" placeholder="Filter by engine, source table, destination, or operation">
        <button class="btn" onclick="clearAllFilters()">Clear filters</button>
      </div>
      <div id="active-filters" style="margin: 16px 0;"></div>
      <div class="grid grid-2">
        <div><div style="font-weight: 600; margin-bottom: 8px;">Filter by engine</div><div class="chart-container"><canvas id="engineChart"></canvas></div></div>
        <div><div style="font-weight: 600; margin-bottom: 8px;">Filter by operation type</div><div class="chart-container"><canvas id="operationChart"></canvas></div></div>
      </div>
      <div id="access-patterns-container"></div>
    </div>

    <div class="section">
      <div class="section-header">Trade-offs and Design Decisions</div>
      <p class="section-desc">What each target design gains, what it gives up, and the reasoning behind the decision.</p>
      <div id="tradeoffs-container"></div>
    </div>

    <div class="section">
      <div class="section-header">Principal Engineer Notes</div>
      <p class="section-desc">Observations raised while reviewing each design, including the decisions to validate with your team before you commit to them.</p>
      <div id="pe-notes-container"></div>
    </div>
  </div>

${generateReportScript(data, ENGINE_LABELS)}

</body>
</html>`;
};
