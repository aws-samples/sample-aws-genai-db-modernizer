// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2346: a failed request never prints its URL, status code, stack or server text.
 * Pages show the fixed message for the kind of failure (utils/requestFailure.js) and
 * log the detail to the console. This walks every UI source file and fails when a
 * render path reads an error's message, interpolates a status or a request URL, or
 * renders an `error`/`message` value directly. Localised text is scanned too: no
 * placeholder may carry a URL, status or message.
 *
 * Caught:
 *   A. `x.error.message`, `err.message`, `detail.message` (not a comparison with a literal)
 *   B. the names apiUrl, statusCode, errorMessage, errorDetails
 *   C. a status read of a request result inside a template string, JSX or a t() call
 *   D. JSX that renders `error`, `err`, `message` or `detail` (or a property of that name)
 *   F. an error value passed into localised text: t('key', { error }), t('key', { message: x })
 *   E. text that starts an API error line ("API Error", "(Status:")
 * The one exception is the API's own refusal sentence, through `failureReason` or the
 * `allowReason` option of requestFailure.js, for four call sites only (see the last test).
 * Not scanned: tests, and the two modules that produce error text for the console.
 */
const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');

const SRC = path.join(__dirname, '..');
// requestFailure.js reads the detail to log it; apiErrors.js builds the Error from the API's body.
const PRODUCERS = new Set(['utils/requestFailure.js', 'utils/apiErrors.js']);

function sourceFiles() {
  return fs.readdirSync(SRC, { recursive: true })
    .filter((rel) => /\.jsx?$/.test(rel) && !rel.split(path.sep).includes('__tests__'))
    .map((rel) => rel.split(path.sep).join('/'))
    .filter((rel) => !PRODUCERS.has(rel))
    .map((rel) => ({ rel, file: `${SRC}/${rel}` }));
}

// Names that hold a failure: the usual ones, and every catch parameter of the file.
const ERRORISH = /^(error|err|e|ex|exc|exception|detail|cause|problem|failure)$/i;
const RESULTISH = /^(r|res|resp|response|result|results|apiResult|\w*Result|\w*Response|xhr|error|err|e|ex)$/i;
const CHILDISH = /^(error|err|e|ex|exc|exception|message|msg|detail|problem|failure|reason|errorMsg)$/i;
const BANNED_NAMES = new Set(['apiUrl', 'statusCode', 'errorMessage', 'errorDetails', 'rawReason']);
const LEAKY_KEYS = new Set(['message', 'stack', 'reason', 'detail', 'response']);

// The names along a member chain: results['x']?.error.message -> ['results', 'x', 'error', 'message'].
function chainNames(node) {
  const names = [];
  let n = node;
  while (n) {
    if (n.type === 'MemberExpression' || n.type === 'OptionalMemberExpression') {
      if (!n.computed && n.property.type === 'Identifier') names.unshift(n.property.name);
      else if (n.computed && n.property.type === 'StringLiteral') names.unshift(n.property.value);
      else names.unshift('[]');
      n = n.object;
    } else if (n.type === 'CallExpression' && n.callee && isMember(n.callee) && n.arguments.length === 0) {
      n = n.callee.object; // error.toString(): look at the receiver
    } else if (n.type === 'Identifier') {
      names.unshift(n.name);
      break;
    } else break;
  }
  return names;
}

function walk(node, visit, ancestors = []) {
  if (!node || typeof node.type !== 'string') return;
  visit(node, ancestors);
  const next = ancestors.concat(node);
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') continue;
    const v = node[key];
    if (Array.isArray(v)) v.forEach((c) => c && walk(c, visit, next));
    else if (v && typeof v === 'object') walk(v, visit, next);
  }
}

const isMember = (n) => n.type === 'MemberExpression' || n.type === 'OptionalMemberExpression';

function offenders(code, rel = 'x.js') {
  const ast = parse(code, { sourceType: 'module', plugins: ['jsx'] });
  const found = [];
  const add = (n, why) => found.push(`${why} at line ${n.loc.start.line}`);
  // Every catch parameter of the file is an error holder, whatever its name.
  const catchNames = new Set();
  walk(ast.program, (n) => {
    if (n.type === 'CatchClause' && n.param && n.param.type === 'Identifier') catchNames.add(n.param.name);
    // p.catch((x) => ...) and p.then(ok, (x) => ...): the callback's parameter holds the failure.
    if (n.type === 'CallExpression' && isMember(n.callee) && !n.callee.computed) {
      const which = n.callee.property.name === 'catch' ? 0 : n.callee.property.name === 'then' ? 1 : -1;
      const cb = which >= 0 ? n.arguments[which] : null;
      if (cb && /^(ArrowFunctionExpression|FunctionExpression)$/.test(cb.type) && cb.params[0] && cb.params[0].type === 'Identifier') catchNames.add(cb.params[0].name);
    }
  });
  const holds = (names) => names.length > 0 && (ERRORISH.test(names[0]) || catchNames.has(names[0]) || names.slice(0, -1).some((x) => ERRORISH.test(x)));
  // A value that is an error: a bare error variable, or its message, stack, error or detail.
  const isErrorValue = (names) => holds(names) && (names.length === 1 || /^(message|stack|error|detail)$/.test(names[names.length - 1]));
  const inPrint = (anc) => anc.some((a) => a.type === 'TemplateLiteral' || a.type === 'JSXExpressionContainer'
    || (a.type === 'CallExpression' && a.callee.type === 'Identifier' && a.callee.name === 't'));
  walk(ast.program, (n, anc) => {
    const parent = anc[anc.length - 1];
    if (isMember(n)) {
      const names = chainNames(n);
      const last = names[names.length - 1];
      // A. reading an error's message or stack, by dot or by index, under any catch name
      const compared = parent && parent.type === 'BinaryExpression' && ['===', '==', '!==', '!='].includes(parent.operator)
        && [parent.left, parent.right].some((s) => s !== n && s.type === 'StringLiteral');
      if ((last === 'message' || last === 'stack') && holds(names.slice(0, -1).length ? names : []) && !compared) add(n, 'reads an error message');
      // C. a status of a request result in a template, JSX or t() call
      if (/^(status|statusText|statusCode)$/.test(last) && names.slice(0, -1).some((x) => RESULTISH.test(x) || x === '[]' || catchNames.has(x)) && inPrint(anc)) {
        add(n, 'prints a request status');
      }
      // G. the API base URL in a template (the request URL itself)
      if (last === 'baseUrl' && rel !== 'classes/ApiManager.js' && anc.some((a) => a.type === 'TemplateLiteral' || (a.type === 'BinaryExpression' && a.operator === '+'))) add(n, 'builds text from the API base URL');
    }
    // B. names that only exist to print a URL, status or message
    if ((n.type === 'Identifier' || n.type === 'JSXIdentifier') && BANNED_NAMES.has(n.name)) add(n, `uses ${n.name}`);
    // D. rendering an error value directly as a JSX child
    if (n.type === 'JSXExpressionContainer' && parent && (parent.type === 'JSXElement' || parent.type === 'JSXFragment')) {
      const names = chainNames(n.expression);
      const last = names[names.length - 1];
      if (names.length && ((names.length === 1 && (CHILDISH.test(last) || catchNames.has(last))) || /^(error|err|message|detail)$/i.test(last))) add(n, 'renders an error value');
    }
    // H. an error interpolated into a template string, or turned into text
    if (n.type === 'TemplateLiteral') {
      n.expressions.forEach((e) => { if (isErrorValue(chainNames(e))) add(e, 'interpolates an error'); });
    }
    if (n.type === 'CallExpression' && n.callee.type === 'Identifier' && ['String', 'JSON'].includes(n.callee.name) && n.arguments[0] && isErrorValue(chainNames(n.arguments[0]))) {
      add(n, 'turns an error into text');
    }
    if (n.type === 'CallExpression' && n.callee.type === 'MemberExpression' && n.callee.object.type === 'Identifier' && n.callee.object.name === 'JSON'
      && n.arguments[0] && isErrorValue(chainNames(n.arguments[0]))) add(n, 'turns an error into text');
    if (n.type === 'CallExpression' && isMember(n.callee) && !n.callee.computed && n.callee.property.name === 'toString' && isErrorValue(chainNames(n.callee.object))) {
      add(n, 'turns an error into text');
    }
    if (n.type === 'BinaryExpression' && n.operator === '+' && [n.left, n.right].some((s) => s.type === 'StringLiteral' || s.type === 'TemplateLiteral')
      && [n.left, n.right].some((s) => isErrorValue(chainNames(s)))) add(n, 'joins an error into text');
    // I. destructuring an error: const { message: m } = error;  catch ({ message })
    if (n.type === 'VariableDeclarator' && n.id.type === 'ObjectPattern' && n.init && holds(chainNames(n.init))) {
      n.id.properties.forEach((p) => { if (p.type === 'ObjectProperty' && LEAKY_KEYS.has(p.key.name || p.key.value)) add(p, 'destructures an error'); });
    }
    if (n.type === 'CatchClause' && n.param && n.param.type === 'ObjectPattern') {
      n.param.properties.forEach((p) => { if (p.type === 'ObjectProperty' && LEAKY_KEYS.has(p.key.name || p.key.value)) add(p, 'destructures an error'); });
    }
    // F. an error value passed into localised text: t('key', { error })
    if (n.type === 'ObjectProperty' && anc.some((a) => a.type === 'CallExpression' && a.callee.type === 'Identifier' && a.callee.name === 't')) {
      const key = n.key.name || n.key.value;
      const valueNames = chainNames(n.value);
      if (/^(error|err|message|detail)$/i.test(key) || (valueNames.length && /^(error|err|message|detail)$/i.test(valueNames[valueNames.length - 1]))) add(n, 'passes an error value to t()');
    }
    // E. the old "API Error (Status: ...)" lines
    const text = n.type === 'StringLiteral' ? n.value : n.type === 'TemplateElement' ? n.value.raw : null;
    if (text && /\bAPI Error\b|\(Status:/.test(text)) add(n, 'writes an API error line');
  });
  return found;
}

const flags = (code, why, rel) => {
  const found = offenders(code, rel);
  expect(found.length).toBeGreaterThan(0);
  expect(found.some((f) => f.startsWith(why))).toBe(true);
};

describe('the checker flags every form of printing an error', () => {
  test('error.message in a flash', () => flags("add({ content: error.message });", 'reads an error message'));
  test('a chained result error message', () => flags("add({ content: results['x']?.error?.message || 'Failed' });", 'reads an error message'));
  test('ex.message under another catch name', () => flags("try { a(); } catch (ex) { add({ content: ex.message }); }", 'reads an error message'));
  test('a catch parameter with any name', () => flags("try { a(); } catch (boom) { add({ content: boom.message }); }", 'reads an error message'));
  test('error[\'message\']', () => flags("add({ content: error['message'] });", 'reads an error message'));
  test('the stack', () => flags("try { a(); } catch (boom) { add({ content: boom.stack }); }", 'reads an error message'));
  test('String(error)', () => flags("add({ content: String(error) });", 'turns an error into text'));
  test('error.toString()', () => flags("add({ content: error.toString() });", 'turns an error into text'));
  test('JSON.stringify(error)', () => flags("add({ content: JSON.stringify(error) });", 'turns an error into text'));
  test('a template that interpolates an error', () => flags('add({ content: `Failed: ${error}` });', 'interpolates an error'));
  test('a template that interpolates a catch parameter', () => flags('try { a(); } catch (boom) { add({ content: `Failed: ${boom}` }); }', 'interpolates an error'));
  test('a string joined to an error', () => flags("add({ content: 'Failed: ' + error });", 'joins an error into text'));
  test('renamed destructuring of an error', () => flags('const { message: m } = error;', 'destructures an error'));
  test('plain destructuring of an error', () => flags('try { a(); } catch (boom) { const { message } = boom; }', 'destructures an error'));
  test('a destructured catch parameter', () => flags('try { a(); } catch ({ message }) { add(message); }', 'destructures an error'));
  test('an error variable as a JSX child', () => {
    flags('const a = <b>{err}</b>;', 'renders an error value');
    flags('const a = <b>{e}</b>;', 'renders an error value');
    flags('const a = <b>{problem}</b>;', 'renders an error value');
    flags('const a = <b>{res.error.message}</b>;', 'reads an error message');
    flags('const a = <b>{queryJourneyData.error}</b>;', 'renders an error value');
    flags('try { a(); } catch (boom) { b = <i>{boom}</i>; }', 'renders an error value');
  });
  test('a request status in a template', () => {
    flags('const h = `Failed (${r.status})`;', 'prints a request status');
    flags('const h = `Failed (${apiResult.status})`;', 'prints a request status');
    flags("const h = `Failed (${results['x'].status})`;", 'prints a request status');
    flags("const h = t('x', { s: result.status });", 'prints a request status');
    flags('const h = <i>{results.a.status}</i>;', 'prints a request status');
  });
  test('the API base URL in text', () => {
    flags('const u = `${ApiConfigurations.baseUrl}assessments`;', 'builds text from the API base URL');
    flags('const u = `Failed at ${config.baseUrl}`;', 'builds text from the API base URL', 'pages/X.js');
  });
  test('a callback parameter of catch() or then() holds an error', () => {
    flags('p.catch((reason) => add({ content: reason.message }));', 'reads an error message');
    flags('p.then(ok, (x) => add({ content: x.message }));', 'reads an error message');
    flags('p.catch(function (x) { add({ content: `Failed: ${x}` }); });', 'interpolates an error');
  });
  test('the API base URL joined with +', () => {
    flags("const u = 'At ' + ApiConfigurations.baseUrl;", 'builds text from the API base URL');
    flags('const u = config.baseUrl + path;', 'builds text from the API base URL', 'pages/X.js');
  });
  test('the old names and lines', () => {
    flags('const apiUrl = 1;', 'uses apiUrl');
    flags('const statusCode = 1;', 'uses statusCode');
    flags('const h = `API Error (Status: ${n})`;', 'writes an API error line');
  });
  test('the raw reason reader outside the producers', () => {
    flags("import { rawReason } from '../utils/apiErrors'; const r = rawReason(e);", 'uses rawReason');
  });
  test('an error value handed to t()', () => {
    flags("const a = t('x', { error });", 'passes an error value to t()');
    flags("const a = t('x', { message: error });", 'passes an error value to t()');
  });
});

test('the checker leaves safe reads alone', () => {
  expect(offenders("if (e.message === 'ResizeObserver loop') x();")).toEqual([]);
  expect(offenders('if (result.status === 409) x();')).toEqual([]);
  expect(offenders('const h = <b>{job.status}</b>;')).toEqual([]);
  expect(offenders("const m = results['cancel']?.message;")).toEqual([]);
  expect(offenders("console.error('Error loading:', error);")).toEqual([]);
  expect(offenders("const x = t('errors.request.server');")).toEqual([]);
  expect(offenders("const a = t('x', { count: 2, id: queryId });")).toEqual([]);
  expect(offenders('const c = `${e.classes} cross-wave`;')).toEqual([]);
  expect(offenders('const a = <b>{c.failure}</b>;')).toEqual([]);
  expect(offenders('p.catch((x) => log(x.status)); p.then((v) => v.message);')).toEqual([]);
  expect(offenders('const u = this.baseUrl + path;', 'classes/ApiManager.js')).toEqual([]);
  expect(offenders('const u = `${this.baseUrl}${path}`;', 'classes/ApiManager.js')).toEqual([]);
});

test('no render path in the UI source prints an error message, status or URL', () => {
  const bad = [];
  expect(sourceFiles().length).toBeGreaterThan(50);
  for (const { rel, file } of sourceFiles()) {
    offenders(fs.readFileSync(file, 'utf8'), rel).forEach((f) => bad.push(`${rel}: ${f}`));
  }
  expect(bad).toEqual([]);
});

test('no localised text carries a URL, a status or a message placeholder', () => {
  const en = require('../locales/en.json');
  const bad = Object.entries(en)
    .filter(([, v]) => typeof v === 'string' && /\{\{\s*(apiUrl|url|status|statusCode|errorMessage|errorDetails|message|error|detail)\s*\}\}|\(Status:|https?:\/\//.test(v))
    .map(([k]) => k);
  // The docs link in the help text is a fixed address, not a request URL.
  expect(bad.filter((k) => !/^(help|docs)/.test(k))).toEqual([]);
});

test('the error messages of every kind name no URL, host, port or status', () => {
  const en = require('../locales/en.json');
  const { FAILURE_KINDS, failureMessageKey } = require('../utils/requestFailure');
  FAILURE_KINDS.forEach((kind) => {
    const text = en[failureMessageKey(kind)];
    expect(text).toBeTruthy();
    expect(text).not.toMatch(/\d|https?|localhost|:\/\//);
  });
});

test('only the four refusal call sites may show the API reason, and each spelling is counted', () => {
  const source = (rel) => fs.readFileSync(`${SRC}/${rel}`, 'utf8');
  const ALLOW = /\ballowReason\b/g;
  const REASON = /\bfailureReason\b/g;
  const count = (rel, re) => (source(rel).match(re) || []).length;
  const users = sourceFiles()
    .filter(({ rel }) => /\ballowReason\b|\bfailureReason\b/.test(source(rel)))
    .map(({ rel }) => rel)
    .sort();
  expect(users).toEqual([
    'components/AssumptionsPanel.js', // an assumption out of range
    'pages/AnalysisResults.js', // a refused saved schema design
    'pages/AssignmentGate.js', // a rejected assignment override
    'pages/CreateAnalysis.js', // the secrets refusal on create
  ]);
  // Every occurrence of either word, imports and definitions included, is pinned per file.
  expect([count('components/AssumptionsPanel.js', ALLOW), count('components/AssumptionsPanel.js', REASON)]).toEqual([0, 2]); // import, call
  expect([count('pages/AnalysisResults.js', ALLOW), count('pages/AnalysisResults.js', REASON)]).toEqual([0, 2]); // import, call
  expect([count('pages/AssignmentGate.js', ALLOW), count('pages/AssignmentGate.js', REASON)]).toEqual([2, 0]); // save overrides, save choices
  expect([count('pages/CreateAnalysis.js', ALLOW), count('pages/CreateAnalysis.js', REASON)]).toEqual([1, 0]); // online create
  // The raw reason is read in requestFailure.js only, through apiErrors.js's WeakMap.
  expect(sourceFiles().filter(({ rel }) => /\brawReason\b/.test(source(rel)))).toEqual([]);
});
