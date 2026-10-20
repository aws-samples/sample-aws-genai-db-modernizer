/**
 * The locking clause of a SQL read: the JavaScript copy of
 * `src/agents/referee/query_shape.py`'s `locking_clause` (#838), the lock the
 * ownership gate reads (`cache_overlay.is_locking_read`, ADR-031 Decision 2
 * step 3). The engine picker uses it so it never offers OpenSearch for a read
 * the server would reject. Both are held to the same cases by
 * `tests/unit/agents/referee/locking_clause_cases.json`.
 *
 * The source engine (`metadata.source_database.engine` of the collector payload)
 * picks the dialect, as `src/shared/sql_spans.py` does (#1143): MySQL and MariaDB
 * add `#` line comments, PostgreSQL adds `$$...$$` / `$tag$...$tag$` strings, and
 * every other engine (or none) keeps the common grammar. A case in the shared
 * file with an `engine` runs in that dialect; one without runs in the common one.
 *
 * Forms: `FOR UPDATE` / `FOR NO KEY UPDATE` / `FOR SHARE` / `FOR KEY SHARE` (with
 * an optional `OF <tables>` list and `NOWAIT` / `SKIP LOCKED`), MySQL `LOCK IN
 * SHARE MODE`, and a SQL Server `WITH (...)` table-hint list naming `UPDLOCK`,
 * `HOLDLOCK`, `XLOCK` or `SERIALIZABLE`. Matched after comments, literals and
 * quoted identifiers are removed in one linear pass, so `'FOR UPDATE'` (a
 * value), `-- FOR UPDATE` (a comment) or `"update"` (a column) is never a lock.
 *
 * The regexes spell out Python's `re` rules rather than JavaScript's, so the two
 * agree on every input: `\s` is Python's whitespace set, a word character is a
 * Unicode letter, number or `_`, and a keyword letter matches what Python's
 * IGNORECASE matches (`K` also matches U+212A, `S` U+017F, `I` U+0130/U+0131).
 */
import { PY_SPACE_CODES } from './sqlComments';

const SPACE_CLASS = `[${PY_SPACE_CODES.map((code) => `\\u{${code.toString(16)}}`).join('')}]`;
const WORD = '[\\p{L}\\p{N}_]';
const BOUNDARY = `(?:(?<=${WORD})(?!${WORD})|(?<!${WORD})(?=${WORD}))`;
const S = `${SPACE_CLASS}*`;
const S1 = `${SPACE_CLASS}+`;

// Python's re.IGNORECASE matches for the keyword letters beyond their two ASCII cases.
const EXTRA_CASES = { I: '\\u{130}\\u{131}', K: '\\u{212a}', S: '\\u{17f}' };

/** A case-insensitive keyword; a space in `words` is one or more whitespace characters. */
function kw(words) {
  return words
    .split(' ')
    .map((word) =>
      [...word]
        .map((ch) => `[${ch.toUpperCase()}${ch.toLowerCase()}${EXTRA_CASES[ch] || ''}]`)
        .join(''),
    )
    .join(S1);
}

// The openers normalizedSql looks for; whichever opens first wins. Per dialect, as
// src/shared/sql_spans.py's grammars: MySQL adds `#`, PostgreSQL adds a dollar quote
// (`$$` or `$tag$`, a tag cannot start with a digit, and not inside a name like `a$b$`).
const TAG_START = '(?![\\p{Nd}])[\\p{L}\\p{N}_]';
const DOLLAR_OPENER = `(?<![\\p{L}\\p{N}_$])\\$(?:${TAG_START}${WORD}*)?\\$`;
const DOLLAR_TOKEN_RE = new RegExp(`(?=(\\$(?:${TAG_START}${WORD}*)?\\$))`, 'gu');
const COMMON_OPENER = "/\\*|--|['\"`[]";
const MYSQL_OPENER = "/\\*|--|#|['\"`[]";
const POSTGRES_OPENER = `/\\*|--|['"\`[]|${DOLLAR_OPENER}`;
// A fresh regex per scan (it carries lastIndex), built from fixed sources only.
const OPENER_RES = {
  common: () => new RegExp(COMMON_OPENER, 'gu'),
  mysql: () => new RegExp(MYSQL_OPENER, 'gu'),
  postgres: () => new RegExp(POSTGRES_OPENER, 'gu'),
};
const SPAN_CLOSERS = { '`': '`', '[': ']', '/*': '*/' };

/** The dialect of a source engine, chosen as sql_spans.literals_and_comments_for does. */
function dialectOf(sourceEngine) {
  const engine = String(sourceEngine ?? '').trim().toLowerCase();
  if (engine === 'mysql' || engine === 'mariadb') {
    return 'mysql';
  }
  if (engine === 'postgresql' || engine === 'postgres') {
    return 'postgres';
  }
  return 'common';
}

/** The collection's source engine, lower-cased (query_facts.source_engine_of). */
export function sourceEngineOf(collector) {
  const engine = collector?.metadata?.source_database?.engine;
  return String(engine ?? '').trim().toLowerCase();
}

/** Every `$tag$` token in `text` (overlaps included) with its start offsets, ascending. */
function dollarTokens(text) {
  const found = new Map();
  for (const match of text.matchAll(DOLLAR_TOKEN_RE)) {
    const list = found.get(match[1]);
    if (list) {
      list.push(match.index);
    } else {
      found.set(match[1], [match.index]);
    }
  }
  return found;
}

/** End (exclusive) of the dollar-quoted string opening at `start`, or -1: its next same token. */
function dollarEnd(tokens, start, opener) {
  const positions = tokens.get(opener) || [];
  const from = start + opener.length;
  let low = 0;
  let high = positions.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (positions[mid] < from) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  return low < positions.length ? positions[low] + opener.length : -1;
}

const STRENGTH = `(?<strength>${kw('NO KEY UPDATE')}|${kw('UPDATE')}|${kw('KEY SHARE')}|${kw('SHARE')})`;
const LOCKED_TABLE = `(?:${WORD}|\\$)+(?:${S}\\.${S}(?:${WORD}|\\$)+)*`;
const FOR_LOCKING_SOURCE =
  `${BOUNDARY}${kw('FOR')}${S1}${STRENGTH}` +
  `(?:${S1}${kw('OF')}${S1}${LOCKED_TABLE}(?:${S},${S}${LOCKED_TABLE})*)?` +
  `(?:${S1}(?<wait>${kw('NOWAIT')}|${kw('SKIP LOCKED')}))?${BOUNDARY}`;
const LOCK_IN_SHARE_MODE_RE = new RegExp(`${BOUNDARY}${kw('LOCK IN SHARE MODE')}${BOUNDARY}`, 'u');
// `WITH ( ... )` with at most one level of nested parentheses (`INDEX(pk)`).
const TABLE_HINTS_SOURCE = `${BOUNDARY}${kw('WITH')}${S}\\((?<hints>(?:[^()]|\\([^()]*\\))*)\\)`;
const HINT_ARGUMENTS_RE = /\([^()]*\)/g;
const HINT_WORD_RE = /[A-Za-z_]+/g;

// The SQL Server table hints (the documented list); nothing else is recorded.
const TABLE_HINT_NAMES = new Set([
  'FORCESCAN',
  'FORCESEEK',
  'HOLDLOCK',
  'INDEX',
  'NOEXPAND',
  'NOLOCK',
  'NOWAIT',
  'PAGLOCK',
  'READCOMMITTED',
  'READCOMMITTEDLOCK',
  'READPAST',
  'READUNCOMMITTED',
  'REPEATABLEREAD',
  'ROWLOCK',
  'SERIALIZABLE',
  'SNAPSHOT',
  'TABLOCK',
  'TABLOCKX',
  'UPDLOCK',
  'XLOCK',
]);
const LOCKING_HINTS = ['UPDLOCK', 'HOLDLOCK', 'XLOCK', 'SERIALIZABLE'];

const SPACE_RUN_RE = new RegExp(S1, 'gu');

/** Python's `" ".join(text.upper().split())`. */
function canonical(text) {
  return text
    .toUpperCase()
    .split(SPACE_RUN_RE)
    .filter((part) => part !== '')
    .join(' ');
}

/**
 * End (exclusive) of the `quote`-quoted span opening at `start`, or -1: the same
 * answer as query_shape.py's _quoted_span_end. A doubled quote is an escaped
 * quote (and in a ' literal, a backslash and the next character). When the text
 * ends inside the span, it closes at the first quote of its last doubled pair.
 */
function quotedSpanEnd(text, start, quote) {
  // What can end or escape the span: ' literals take backslash escapes, "
  // identifiers do not. A fresh literal per call, so lastIndex starts clean.
  const stops = quote === "'" ? /['\\]/g : /"/g;
  let lastPair = -1;
  stops.lastIndex = start + 1;
  for (;;) {
    const stop = stops.exec(text);
    if (stop === null) {
      return lastPair >= 0 ? lastPair + 1 : -1;
    }
    const pos = stop.index;
    if (text[pos] === '\\') {
      if (pos + 1 >= text.length) {
        return lastPair >= 0 ? lastPair + 1 : -1;
      }
      stops.lastIndex = pos + 2;
    } else if (text.startsWith(quote + quote, pos)) {
      lastPair = pos;
      stops.lastIndex = pos + 2;
    } else {
      return pos + 1;
    }
  }
}

/**
 * `text` with comments and literals blanked and quoted identifiers made `ident`,
 * in one left-to-right pass (query_shape.py's _normalized_sql, review of #871):
 * the span that opens first is replaced whole, so a quote inside a comment or a
 * literal never pairs with one outside it. An opener with no closer is plain
 * text; once one kind has no closer left, later openers of that kind are skipped
 * at once, so the scan stays linear.
 */
function normalizedSql(text, sourceEngine) {
  const dialect = dialectOf(sourceEngine);
  const openers = OPENER_RES[dialect]();
  const tokens = dialect === 'postgres' ? dollarTokens(text) : null;
  const unclosed = new Set();
  const out = [];
  let last = 0;
  let match = openers.exec(text);
  while (match !== null) {
    const start = match.index;
    const opener = match[0];
    let end = -1;
    if (!unclosed.has(opener)) {
      if (opener === '--' || opener === '#') {
        const newline = text.indexOf('\n', start);
        end = newline < 0 ? text.length : newline;
      } else if (opener.startsWith('$')) {
        end = dollarEnd(tokens, start, opener);
      } else if (opener in SPAN_CLOSERS) {
        const closer = SPAN_CLOSERS[opener];
        const found = text.indexOf(closer, start + opener.length);
        end = found >= 0 ? found + closer.length : -1;
      } else {
        end = quotedSpanEnd(text, start, opener);
      }
      if (end < 0) {
        unclosed.add(opener);
      }
    }
    if (end < 0) {
      openers.lastIndex = start + 1;
    } else {
      out.push(
        text.slice(last, start),
        ['--', '/*', '#', "'"].includes(opener) || opener.startsWith('$') ? ' ' : ' ident ',
      );
      last = end;
      openers.lastIndex = end;
    }
    match = openers.exec(text);
  }
  out.push(text.slice(last));
  return out.join('');
}

function hintNames(hints) {
  const words = hints.replace(HINT_ARGUMENTS_RE, ' ').match(HINT_WORD_RE) || [];
  const names = words.map((w) => w.toUpperCase()).filter((name) => TABLE_HINT_NAMES.has(name));
  return [...new Set(names)];
}

/**
 * The locking clause of `queryText` in canonical form (`"FOR UPDATE"`,
 * `"FOR NO KEY UPDATE NOWAIT"`, `"LOCK IN SHARE MODE"`, `"WITH (UPDLOCK,
 * ROWLOCK)"`; several joined with `"; "`), or `null` when the read takes no lock.
 * `sourceEngine` is the source database's engine, which sets the dialect.
 */
export function lockingClause(queryText, sourceEngine) {
  const sql = normalizedSql(String(queryText ?? ''), sourceEngine);
  const clauses = [];
  for (const match of sql.matchAll(new RegExp(FOR_LOCKING_SOURCE, 'gu'))) {
    const words = ['FOR', canonical(match.groups.strength)];
    if (match.groups.wait) {
      words.push(canonical(match.groups.wait));
    }
    clauses.push(words.join(' '));
  }
  if (LOCK_IN_SHARE_MODE_RE.test(sql)) {
    clauses.push('LOCK IN SHARE MODE');
  }
  for (const match of sql.matchAll(new RegExp(TABLE_HINTS_SOURCE, 'gu'))) {
    const names = hintNames(match.groups.hints);
    if (names.some((name) => LOCKING_HINTS.includes(name))) {
      clauses.push(`WITH (${names.join(', ')})`);
    }
  }
  return clauses.length ? clauses.join('; ') : null;
}

/** Whether `queryText` takes a row lock (the ownership gate's `is_locking_read`). */
export function isLockingRead(queryText, sourceEngine) {
  return lockingClause(queryText, sourceEngine) !== null;
}
