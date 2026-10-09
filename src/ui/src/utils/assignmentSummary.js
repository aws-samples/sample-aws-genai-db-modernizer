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

// #381 review round 2: the resolver may have picked one of the two competing
// Aurora engines for a heterogeneous source (SQL Server, Oracle, DB2 -- no Aurora
// dialect of its own), recorded on the assignment as `aurora_engine_choice.engine`.
// The losing engine is never a valid target for this assessment -- offering it in
// the per-query engine picker would let a customer pick it and then have the
// override rejected server-side (LosingAuroraEngineOverride) with no warning here.
const AURORA_ENGINES = ['aurora_mysql', 'aurora_postgresql'];

/**
 * Drop the #381 losing Aurora engine from a list of engine `options`
 * (`{ label, value }`), when `assignment.aurora_engine_choice` names a winner.
 * Returns `options` unchanged when there is no recorded choice, or the winner is
 * not one of the two Aurora engines (homogeneous source, or a legacy assignment).
 */
export function engineOptionsForAssignment(options, assignment) {
  const winner = assignment?.aurora_engine_choice?.engine;
  if (!AURORA_ENGINES.includes(winner)) {
    return options;
  }
  const loser = AURORA_ENGINES.find((engine) => engine !== winner);
  return options.filter((option) => option.value !== loser);
}

// OpenSearch is a read model (#303): it never owns a write or a locking
// read. Mirrors the server's write/locking-read detection
// (is_write_query/is_locking_read in src/agents/referee/cache_overlay.py)
// so this picker never offers an option the server would reject -- a
// customer override that put a write on OpenSearch is rejected server-side
// (AssignmentValidator) with no warning here otherwise.
const WRITE_QUERY_TYPES = ['INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'MERGE', 'UPSERT'];
// A data-modifying CTE, CALL, TRUNCATE, COPY or a comment-led statement all
// come through the collector as query_type "OTHER" instead (same regexes as
// cache_overlay.py's _DML_KEYWORD_RE/_LEADING_WRITE_VERB_RE/_LEADING_COMMENT_RE).
const LEADING_COMMENT_RE = /^(\s*(--[^\n]*\n|#[^\n]*\n|\/\*[\s\S]*?\*\/)\s*)+/;
const DML_KEYWORD_RE = /\b(insert\s+into|update\s+\S+\s+set|delete\s+from|merge\s+into)\b/i;
const LEADING_WRITE_VERB_RE = /^\s*(call|truncate|copy)\b/i;
// Same as cache_overlay.py's _LOCKING_RE.
const LOCKING_RE = /\bfor\s+(update|share)\b|\block\s+in\s+share\s+mode\b/i;

function isWriteQuery(queryType, queryText) {
  const qtype = String(queryType || '').toUpperCase();
  if (WRITE_QUERY_TYPES.includes(qtype)) {
    return true;
  }
  if (qtype !== 'OTHER') {
    return false;
  }
  const text = String(queryText || '').replace(LEADING_COMMENT_RE, '');
  return DML_KEYWORD_RE.test(text) || LEADING_WRITE_VERB_RE.test(text);
}

function isLockingRead(queryText) {
  return LOCKING_RE.test(String(queryText || ''));
}

/**
 * Drop OpenSearch from a list of engine `options` (`{ label, value }`) when
 * this query is a write or a locking read (same detection as the server).
 * Returns `options` unchanged for a plain read.
 */
export function engineOptionsForQuery(options, queryType, queryText) {
  if (!isWriteQuery(queryType, queryText) && !isLockingRead(queryText)) {
    return options;
  }
  return options.filter((option) => option.value !== 'opensearch');
}
