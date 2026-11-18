// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2346: what a reader sees when a request fails. One mapping from a failed request
 * (an ApiManager result, an axios or fetch error, a bare status) to a short kind, and
 * from the kind to a fixed message. The page never prints a request URL, host, port,
 * status code, stack or server text: those go to the console through `reportFailure`.
 * Server and customer text is data (AGENTS.md), so it is never read into a message.
 *
 * Kinds:
 *   not-found    404: the service has nothing for this request
 *   rejected     other 4xx: the service refused the request
 *   server       5xx, and 429: the service failed or is busy
 *   timeout      408, or the request ran out of time
 *   unreachable  no answer at all: the network, the API down, a refused connection
 *   other        anything else, including an unreadable answer
 *
 * `src/ui/src/__tests__/NoRawErrorText.test.js` fails when a render path prints an
 * error's message, status or URL instead of going through this module.
 */
import { rawReason } from './apiErrors';

export const FAILURE_KINDS = ['not-found', 'rejected', 'server', 'timeout', 'unreachable', 'other'];

const RETRYABLE = new Set(['server', 'timeout', 'unreachable', 'other']);

function kindOfStatus(status) {
  if (status === 404) return 'not-found';
  if (status === 408) return 'timeout';
  if (status === 429 || status >= 500) return 'server';
  if (status >= 400) return 'rejected';
  return 'other';
}

/** The kind of a failed request. Reads shapes only; it never returns any of their text. */
export function failureKind(source) {
  if (typeof source === 'string') return FAILURE_KINDS.includes(source) ? source : 'other';
  if (typeof source === 'number') return kindOfStatus(source);
  if (source === null || source === undefined) return 'unreachable';
  if (typeof source !== 'object') return 'other';
  // A kind already set: by ApiManager (network error, timeout) or by a caller that threw one.
  if (FAILURE_KINDS.includes(source.kind)) return source.kind;
  // axios: the server answered (response.status), or the request left and nothing came back.
  const status = typeof source.status === 'number' ? source.status : source.response?.status;
  if (typeof status === 'number') return kindOfStatus(status);
  if (source.code === 'ECONNABORTED' || source.code === 'ETIMEDOUT' || source.name === 'TimeoutError' || source.name === 'AbortError') return 'timeout';
  if (source.code === 'ERR_NETWORK' || source.code === 'ECONNREFUSED' || (source.request && !source.response)) return 'unreachable';
  // fetch rejects with a TypeError when it cannot reach the server; any other TypeError is a script error.
  if (source instanceof TypeError && /failed to fetch|networkerror|load failed|network request failed/i.test(source.message)) return 'unreachable';
  // A thrown Error with no request detail is a script error, not an unreachable API.
  if (source instanceof Error) return 'other';
  // An ApiManager result with no status: the request never got an answer.
  return source.success === false ? 'unreachable' : 'other';
}

/** True when trying the same request again can help. */
export function isRetryable(kind) {
  return RETRYABLE.has(kind);
}

/**
 * The i18n key of the fixed message for a kind. `variant` picks the wording of a
 * `rejected` answer: 'action' for a cancel or delete ("refused this action") and 'form'
 * for a submit ("check the values you entered"); other pages get the neutral text.
 */
export function failureMessageKey(kind, variant) {
  const k = FAILURE_KINDS.includes(kind) ? kind : 'other';
  if (k === 'rejected' && (variant === 'action' || variant === 'form')) return `errors.request.rejected-${variant}`;
  return `errors.request.${k}`;
}

const REASON_MAX = 300;
const TLDS = new Set(['com', 'net', 'org', 'io', 'dev', 'app', 'cloud', 'corp', 'internal', 'intranet', 'local', 'localhost',
  'lan', 'home', 'example', 'test', 'invalid', 'edu', 'gov', 'mil', 'int', 'info', 'biz', 'co', 'me', 'us', 'uk', 'de',
  'fr', 'jp', 'cn', 'ru', 'in', 'br', 'au', 'ca', 'aws', 'amazon', 'amazonaws', 'xyz', 'top', 'site', 'online', 'ws',
  'ai', 'sh', 'zip', 'mov', 'ly', 'page', 'link', 'gg', 'to', 'tv', 'cc', 'nl', 'se', 'ch', 'pl', 'eu', 'asia', 'tech',
  'store', 'shop', 'click', 'space', 'live', 'tk', 'ml', 'ga', 'cf', 'gq', 'cx', 'vc', 'fm', 'ink', 'icu', 'club', 'vip',
  'win', 'bid', 'download', 'network', 'email']);
const FIELDS_MAX = 150;

/**
 * The text of an API sentence that is safe to show, or null. NFKC-normalised first, so a
 * fullwidth colon or a compatibility letter is judged as what it reads like. Refused at
 * once: any control, format, surrogate or line/paragraph separator character, and any
 * non-ASCII character left after normalising. What remains must be only
 * letters, digits, space and . , ; : ' " ( ) _ - / and up to 300 characters. Even then
 * it is refused when it reads as a scheme (`word:` glued to the next character, or `//`),
 * a dotted domain or dotted-quad, an IPv6 address (a `::` or a colon run of hex groups),
 * a path (an absolute path, or two or more `/` segments) or a host and port.
 */
export function safeSentence(value) {
  if (typeof value !== 'string') return null;
  const text = value.normalize('NFKC');
  if (/[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/u.test(text) || /[^\x20-\x7E]/.test(text)) return null;
  if (/^\s*$/.test(text) || text.length > REASON_MAX) return null;
  if (!/^[A-Za-z0-9 .,;:'"()_\-/]+$/.test(text)) return null;
  if (/\/\//.test(text) || /[A-Za-z0-9_]:\S/.test(text)) return null; // scheme, host:port
  if (/[A-Za-z0-9_-]\s+:\s*\d/.test(text)) return null; // host : port, with spaces
  if (/\b\d{8,10}\b|\b0x[0-9a-f]+\b/i.test(text)) return null; // IPv4 written as one decimal or hex number
  if (/Traceback|\bat\s+[\w$.]+\s*\(|\bFile\s*"|\.(py|js|ts|java|rb|go):\d+|\bline \d+, in\b/.test(text)) return null; // a stack line
  if (/::/.test(text) || /[0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{0,4}){2,}/.test(text)) return null; // IPv6
  if (/(^|\s)\/|[\w.-]\/[\w.-]+\/|\.\.?\//.test(text)) return null; // a path
  // A dotted token: three or more labels, a known top-level label, or a label before a slash.
  const tokens = text.match(/[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+/g) || [];
  if (tokens.some((tok) => tok.split('.').length >= 3 || TLDS.has(tok.split('.').pop().toLowerCase()))) return null;
  if (/[A-Za-z0-9_-]\.[A-Za-z0-9_-]+\//.test(text)) return null;
  return text;
}

// FastAPI's own location parts are not a field a reader entered.
const LOC_NOISE = new Set(['body', 'query', 'path', 'header', 'cookie']);

/**
 * The API's own reason for a refusal, or null. Only for the call sites that opt in
 * (`allowReason`, or a direct call): a rejected assignment override, the secrets refusal
 * on create, an assumption out of range and a refused saved schema design. Every other
 * page keeps the fixed message. The response must be a 4xx. The reason is one of:
 *   - a plain-string `detail`, through `safeSentence`;
 *   - FastAPI's 422 list, as a fixed sentence naming the fields from each `loc`;
 *   - the `{message, errors}` object, as a fixed sentence plus each error through `safeSentence`.
 * The raw `detail` lives only in apiErrors.js (`rawReason`); no page reads it. `t` is
 * needed for the two sentences that name fields or errors. Render the result as text; a
 * caller falls back to the kind's message on null.
 */
export function failureReason(source, t) {
  if (!source || typeof source !== 'object') return null;
  const status = source.status;
  if (typeof status !== 'number' || status < 400 || status > 499) return null;
  const raw = rawReason(source.error);
  if (!raw) return null;
  if (typeof raw.text === 'string') return safeSentence(raw.text);
  if (!t) return null;
  if (Array.isArray(raw.locs)) {
    const names = [];
    raw.locs.forEach((loc) => {
      (Array.isArray(loc) ? loc : []).forEach((part) => {
        if (typeof part !== 'string' || LOC_NOISE.has(part)) return;
        const name = safeSentence(part);
        if (!name || /[\s:/,;'"()]/.test(name) || names.includes(name)) return;
        // The whole list stays short: a name that would push it past the cap is left out.
        if ([...names, name].join(', ').length <= FIELDS_MAX) names.push(name);
      });
    });
    return names.length ? t('errors.request.fields', { fields: names.slice(0, 5).join(', ') }) : null;
  }
  if (Array.isArray(raw.errors)) {
    const lines = raw.errors.map(safeSentence).filter(Boolean).slice(0, 5);
    const joined = lines.join('; ');
    return lines.length && joined.length <= REASON_MAX ? t('errors.request.validation', { details: joined }) : null;
  }
  return null;
}

/**
 * The technical detail of a failure goes to the console and nowhere else.
 * Returns the kind so a caller can store it: `setError(reportFailure(result, 'deliverables'))`.
 */
export function reportFailure(source, context) {
  const kind = failureKind(source);
  const err = source && typeof source === 'object' ? (source.error || source) : null;
  // eslint-disable-next-line no-console
  console.warn('[request failed]', {
    context,
    kind,
    status: (source && source.status) ?? source?.response?.status,
    detail: err && typeof err === 'object' ? err.message : undefined,
  });
  return kind;
}

/**
 * An Error to throw from a request that failed, for a catch block further up that shows it
 * with `failureFlash`. It carries the kind only; the detail is logged here.
 */
export function failureError(source, context) {
  return Object.assign(new Error(context || 'request failed'), { kind: reportFailure(source, context) });
}

let flashCounter = 0;

/** Add a Flashbar item, or replace the one with the same id (a retry shows one flash, not a stack). */
export function upsertFlash(items, item) {
  return items.some((i) => i.id === item.id) ? items.map((i) => (i.id === item.id ? item : i)) : [...items, item];
}

/**
 * A Flashbar item for a failed request: the header says what failed, the content is the
 * fixed message for the kind (or, for an opt-in call site, the API's safe reason), and a
 * Retry button is added when retrying can help. `source` may be the failed request or a
 * kind already stored by `reportFailure`. `variant` picks the `rejected` wording (see
 * `failureMessageKey`). The id is unique unless the caller gives one.
 */
export function failureFlash(t, source, { header, onRetry, id, context, allowReason = false, variant } = {}) {
  const kind = typeof source === 'string' ? failureKind(source) : reportFailure(source, context || header);
  flashCounter += 1;
  const item = {
    type: 'error',
    header,
    content: (allowReason && typeof source !== 'string' && failureReason(source, t)) || t(failureMessageKey(kind, variant)),
    dismissible: true,
    id: id || `error-${context || 'request'}-${flashCounter}`,
  };
  if (onRetry && isRetryable(kind)) {
    item.buttonText = t('common.actions.retry');
    item.onButtonClick = onRetry;
  }
  return item;
}
