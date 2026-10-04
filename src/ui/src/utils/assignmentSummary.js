/**
 * Hero text for the Assignment review page (#250).
 *
 * The opening sentence is always built from the data the page itself shows
 * (database name, access-pattern count from after_distribution, table count from
 * the collector), so the approver can see which workload they are signing off on.
 * An LLM-written reality-check summary, when present, follows that sentence; it
 * does not replace it. Without one, the distribution and the top integration
 * pattern are described client-side.
 *
 * All strings are routed through i18next's `t` (#254) so they are translatable,
 * and counts (access patterns, tables) are pluralised via CLDR `_one`/`_other`
 * keys in locales/en.json instead of being hardcoded as always-plural English.
 *
 * #296 cache overlay: `afterDist` must never attribute a workload share to
 * ElastiCache once a `cacheOverlay` is present (it is a cache layer, not an
 * owner) -- so a surviving-engine count or "We map N to ElastiCache" sentence
 * can't appear alongside it. Instead, a trailing sentence names it as a cache
 * layer with its own cached-read count and share of calls. Old artifacts with
 * no cacheOverlay and ElastiCache still as a real owner render exactly as
 * before.
 */
import { ownerDistribution, formatCacheLayerLine } from './cacheLayer';

// Fallback used when no `t` is supplied, so the function stays safe to call
// without react-i18next (e.g. a caller that forgets to wire it up). It mirrors
// the plural choice i18next itself would make for English and performs the
// same `{{var}}` interpolation i18next does, using only `defaultValue`.
function defaultT(key, options = {}) {
  const { defaultValue, ...vars } = options;
  let text = defaultValue !== undefined ? defaultValue : key;
  Object.entries(vars).forEach(([name, value]) => {
    text = text.split(`{{${name}}}`).join(value);
  });
  return text;
}

export function buildAssignmentSummary({
  llmSummary,
  databaseName,
  afterDist: rawAfterDist = {},
  tableCount = 0,
  patterns = [],
  cacheOverlay = null,
  engineLabel = (engine) => engine,
  t = defaultT,
}) {
  const narrative = typeof llmSummary === 'string' ? llmSummary.trim() : '';
  const afterDist = ownerDistribution(rawAfterDist, !!cacheOverlay);
  const engines = Object.keys(afterDist);
  const cacheLine = formatCacheLayerLine(cacheOverlay, { t });
  if (engines.length === 0) return [narrative, cacheLine].filter(Boolean).join(' ');

  const totalQueries = Object.values(afterDist).reduce((a, b) => a + b, 0);
  const database = databaseName || t('assignment-gate.summary.default-database', {
    defaultValue: 'database',
  });

  const patternsText = t('assignment-gate.summary.access-patterns-count', {
    count: totalQueries,
    defaultValue: totalQueries === 1 ? '{{count}} access pattern' : '{{count}} access patterns',
  });

  const factSentence = tableCount > 0
    ? t('assignment-gate.summary.workload-fact-with-tables', {
      database,
      patterns: patternsText,
      tables: t('assignment-gate.summary.tables-count', {
        count: tableCount,
        defaultValue: tableCount === 1 ? '{{count}} table' : '{{count}} tables',
      }),
      defaultValue: 'Your {{database}} workload has {{patterns}} across {{tables}}.',
    })
    : t('assignment-gate.summary.workload-fact', {
      database,
      patterns: patternsText,
      defaultValue: 'Your {{database}} workload has {{patterns}}.',
    });

  const parts = [factSentence];

  if (narrative) {
    parts.push(narrative);
  } else if (engines.length === 1) {
    parts.push(t('assignment-gate.summary.single-engine', {
      engine: engineLabel(engines[0]),
      defaultValue: 'All access patterns map to {{engine}}.',
    }));
  } else {
    const distribution = Object.entries(afterDist)
      .filter(([, count]) => count > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([engine, count]) => t('assignment-gate.summary.distribution-item', {
        count,
        engine: engineLabel(engine),
        defaultValue: '{{count}} to {{engine}}',
      }))
      .join(', ');
    parts.push(t('assignment-gate.summary.distribution', {
      distribution,
      defaultValue: 'We map {{distribution}}.',
    }));
    if (patterns.length > 0) {
      parts.push(t('assignment-gate.summary.recommended-pattern', {
        pattern: patterns[0].name,
        defaultValue: 'Recommended integration pattern: {{pattern}}.',
      }));
    }
  }

  if (cacheLine) {
    parts.push(t('assignment-gate.summary.cache-layer', {
      cacheLine,
      defaultValue: '{{cacheLine}}.',
    }));
  }

  return parts.join(' ');
}
