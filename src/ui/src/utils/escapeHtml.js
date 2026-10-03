/**
 * Output escaping for the HTML files the UI exports (ReportResults "Export to HTML"
 * and the interactive report built by ExportReport.js).
 *
 * Everything those exporters interpolate comes from report.json: risk prose,
 * trade-offs and summaries an LLM wrote, and table/column names and SQL taken from
 * the customer's database. None of it is trusted markup, so every interpolated value
 * goes through one of these helpers. They never emit markup; they only neutralise
 * the input so the exporter's own surrounding markup is the only markup in the file.
 *
 * The interactive report also carries a copy of escapeHtml inside its embedded
 * <script> (the exported file is standalone and cannot import this module). The two
 * must stay equivalent; src/ui/src/utils/__tests__/exportEscaping.test.js checks both.
 */

const HTML_ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/**
 * Escape a value for an HTML text node or a quoted attribute value.
 *
 * Escapes & < > " ' so the value can neither open a tag nor close the attribute it
 * sits in. null/undefined become the empty string; anything else is stringified.
 * Works without a DOM, unlike the textContent/innerHTML trick (which also leaves
 * quotes alone and so is unsafe inside attributes).
 */
export const escapeHtml = (value) => {
  if (value == null) return '';
  return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
};

// Schemes allowed in an href/src built from report data. Anything else that carries
// a scheme -- javascript:, data:, vbscript:, ... -- is neutralised.
const SAFE_URL_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

/**
 * Neutralise a URL taken from report data before it is placed in an href/src.
 *
 * Returns '#' for any URL whose scheme is not http(s)/mailto, including obfuscated
 * forms such as "  JaVaScRiPt:alert(1)" or "java\tscript:alert(1)" (browsers strip
 * ASCII whitespace and control characters before resolving the scheme). Relative
 * URLs are kept. The result still has to go through escapeHtml for the attribute.
 */
export const safeUrl = (value) => {
  if (value == null) return '#';
  const raw = String(value);
  // eslint-disable-next-line no-control-regex
  const compact = raw.replace(/[\u0000- \u007f]/g, '');
  const scheme = compact.match(/^([a-z][a-z0-9+.-]*):/i);
  if (scheme && !SAFE_URL_SCHEMES.has(scheme[1].toLowerCase() + ':')) return '#';
  return raw.trim();
};

/**
 * Serialise a value as JSON that is safe inside an inline <script> element.
 *
 * A "</script>" inside a JSON string (a captured SQL statement, an LLM trade-off)
 * would otherwise end the script element early and turn the rest of the string
 * into live markup. Escaping "<" (and the two JS line terminators) keeps it valid
 * JSON. Mirrors _json() in src/report/analysis_report.py.
 */
export const jsonForScript = (value, space) => {
  const text = JSON.stringify(value, null, space);
  if (text === undefined) return 'null';
  return text
    .replace(/</g, '\\u003c')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
};

/**
 * Markup produced by the `html` tag below. Only `html` creates these, so a value of
 * this type is known to be built from literal template text plus escaped values.
 */
export class SafeHtml {
  constructor(value) {
    this.value = value;
  }

  toString() {
    return this.value;
  }
}

const renderInterpolation = (value) => {
  if (value instanceof SafeHtml) return value.value;
  if (Array.isArray(value)) return value.map(renderInterpolation).join('');
  return escapeHtml(value);
};

/**
 * Tagged template for building HTML: every `${...}` is escaped with escapeHtml unless
 * it is itself the result of `html` (nested fragments), and arrays are joined after
 * escaping each element. Escaping is therefore the default rather than something each
 * interpolation has to remember, which is what let #242 happen.
 *
 *   html`<td>${risk.description}</td>`            -> description is escaped
 *   html`<tr>${rows.map(r => html`<td>${r}</td>`)}</tr>` -> nested rows kept as markup
 */
export const html = (strings, ...values) => {
  let out = strings[0];
  for (let i = 0; i < values.length; i += 1) {
    out += renderInterpolation(values[i]) + strings[i + 1];
  }
  return new SafeHtml(out);
};
