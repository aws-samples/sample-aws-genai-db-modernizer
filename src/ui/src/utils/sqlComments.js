/**
 * Linear-time SQL comment stripping: the JavaScript copy of
 * `src/shared/sql_comments.py`'s `strip_leading_comments` (#814), so the UI and
 * the server read comments by one rule. Both are held to the same cases by
 * `tests/unit/shared/sql_comment_cases.json`.
 *
 * A regex that repeats a comment alternation (`/^(\s*(--[^\n]*\n|\/\*[\s\S]*?\*\/)\s*)+/`)
 * overflowed the stack at about 1M repeated comments. This scans the text once,
 * left to right, with `indexOf` to skip each comment body.
 *
 * A `--` line comment (and `#` with `hashComments`, MySQL) counts only with its
 * newline. A block comment ends at its first `*\/`, unless `nested` is set: then
 * each `/*` inside opens one more level, as PostgreSQL and SQL Server read them.
 * An unterminated block comment is not a comment.
 */

const BLOCK_TOKEN_RE = /\/\*|\*\//g;

function blockEnd(text, start, nested) {
  if (!nested) {
    const close = text.indexOf('*/', start + 2);
    return close < 0 ? -1 : close + 2;
  }
  const tokens = new RegExp(BLOCK_TOKEN_RE.source, 'g');
  tokens.lastIndex = start + 2;
  let depth = 1;
  let token = tokens.exec(text);
  while (token !== null) {
    depth += token[0] === '/*' ? 1 : -1;
    if (depth === 0) {
      return tokens.lastIndex;
    }
    token = tokens.exec(text);
  }
  return -1;
}

// Exactly the characters Python's str.isspace() accepts, so this scanner and
// src/shared/sql_comments.py agree on every input: JS's \s lacks U+001C-U+001F
// and U+0085, and adds U+FEFF.
const PY_SPACE = new Set(
  [
    0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000,
    0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
    0x2029, 0x202f, 0x205f, 0x3000,
  ].map((code) => String.fromCharCode(code)),
);

function isSpace(ch) {
  return PY_SPACE.has(ch);
}

function skipSpace(text, start) {
  let i = start;
  while (i < text.length && isSpace(text[i])) {
    i += 1;
  }
  return i;
}

/**
 * `text` without the comments it opens with (and the whitespace before each).
 * `stripFollowingSpace` also removes the whitespace after the last comment.
 * Text that opens with no comment is returned unchanged.
 */
export function stripLeadingComments(
  text,
  { hashComments = false, stripFollowingSpace = false, nested = false } = {},
) {
  const source = String(text ?? '');
  let i = 0;
  let cut = 0;
  for (;;) {
    i = skipSpace(source, i);
    if (source.startsWith('--', i) || (hashComments && source.startsWith('#', i))) {
      const newline = source.indexOf('\n', i);
      if (newline < 0) {
        break;
      }
      i = newline + 1;
      cut = i;
    } else if (source.startsWith('/*', i)) {
      const end = blockEnd(source, i, nested);
      if (end < 0) {
        break;
      }
      i = end;
      cut = i;
    } else {
      break;
    }
  }
  if (cut === 0) {
    return source;
  }
  return source.slice(stripFollowingSpace ? skipSpace(source, cut) : cut);
}
