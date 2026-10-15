/**
 * Renderers only format, JavaScript side (#748, ADR-032).
 *
 * Synthesis computes every customer-facing figure once into report.json's
 * `facts`; the UI and the interactive report read it (reportFacts.js) and
 * format it. This is the JavaScript twin of
 * tests/unit/report/test_renderers_only_format.py and follows the same rule:
 * no UI code counts, totals or does arithmetic on a figure the facts layer
 * owns.
 *
 * Why a jest test over a real parse (@babel/parser, shipped with
 * react-scripts) rather than a Python test: Python has no JavaScript parser,
 * and a regex over the source cannot follow `const risks = ra.risks.filter(..)`
 * to `risks.length` three lines later. The checker is a small per-function
 * taint check over the AST, the same shape as the Python one:
 *
 * - the owned keys are every figure field of the ReportFacts contract (read
 *   from tests/contract/schema_snapshot.json, so a new fact is owned the
 *   moment it joins the contract), plus the report.json inputs the facts are
 *   computed from (INPUT_KEYS);
 * - a source is a read of an owned key (`x.risks`, `x['risks']`), any field
 *   read off the facts block (`synthesis.facts.*`, a name assigned from it,
 *   `waveFacts(...)`), a call to a helper that returns a facts-owned list,
 *   any name assigned from an expression holding one, and the parameter of a
 *   callback run over one (`risks.forEach(r => ...)`);
 * - a sink is `.length` / `.size` of a source, `reduce`, `Math.max/min` over
 *   a source, arithmetic with a source operand, an assignment-arithmetic of a
 *   source, and any accumulator (`n++`, `n += 1`, `d[k] = (d[k] || 0) + 1`)
 *   inside a loop or array callback that walks a source.
 *
 * An emptiness check (`xs.length > 0`, `!xs.length`, `xs.length === 0`) shows
 * no figure and is not a sink. Counting what a list *shows* (a page count of
 * the explorer's own patterns, the "+N more" after 20 names) never touches an
 * owned key and is not flagged. A line that genuinely computes on a fact for
 * presentation only says so with `// facts: presentation (<reason>)`; a
 * pragma with no reason is itself a violation and the exemptions in use are
 * pinned in PINNED_EXEMPTIONS so a new one is reviewed.
 *
 * Allowed as presentation, narrowly: a comparison of a count with a number
 * (`rs.length > 3`, "show the first N"), a sort comparator subtracting one
 * field of its two parameters (`(a, b) => b.score - a.score`), and scaling an
 * element's `score` by 100. Anything else, a `+ 1` or `engine_count - 1` in the
 * same places included, is still flagged; read the pragma rule above before
 * reaching for it.
 *
 * Object destructuring is followed (`const { risks } = ra`,
 * `const { engine_count } = facts`, `const { facts: { views } } = synthesis`,
 * `const { length } = fact.list`).
 *
 * Known limits, deliberately not traced: `%`, aliasing through a function
 * argument (a fact list handed to `count = xs => xs.length`, or any value that
 * crosses a function boundary under another name), a fact object held under a
 * name nothing taints (`Object.keys(fact).length`), and the strings of
 * ExportReport.js (the client script it embeds is guarded in its generated
 * form, src/report/templates/analysis_report.js, which
 * scripts/sync_report_template.py --check keeps equal to the source).
 */

const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');
const t = require('@babel/types');

const REPO = path.resolve(__dirname, '../../../../..');
const UI_SRC = path.join(REPO, 'src', 'ui', 'src');
const SNAPSHOT = path.join(REPO, 'tests', 'contract', 'schema_snapshot.json');
const TEMPLATE_JS = path.join(REPO, 'src', 'report', 'templates', 'analysis_report.js');

// report.json fields the facts are computed from: reading one is fine,
// computing on it is the facts layer's job (same list as the Python guard).
const INPUT_KEYS = new Set([
  'table_count', 'table_owners', 'owned_counts_by_engine', 'table_counts_by_engine',
  'cost_breakdown', 'monthly_cost_usd', 'workload_percent', 'cache_overlay_queries',
  'cache_call_share_percent', 'tied_queries', 'risks', 'resolved_risks',
  'table_mappings', 'post_schema_reroutes', 'ranking', 'trade_offs', 'query_traces',
]);
// Facts fields whose name the UI also uses for an unrelated value (a wave's
// engine ids, a wave object, its number, a list position). Read off the facts block itself they
// are still owned.
const NOT_A_FIGURE = new Set(['engines', 'waves', 'wave', 'index']);
// Helpers that return a facts-owned list or record.
const LIST_HELPERS = new Set([
  'waveFacts', 'realityCheckDecisions', 'currentTradeOffs', 'filterRisksWithContent',
  'groupRisksBySeverity', 'splitRankingByRole', 'cacheLayerCostBreakdown',
]);
const ARRAY_WALKERS = new Set([
  'forEach', 'map', 'filter', 'some', 'every', 'find', 'findIndex', 'flatMap', 'reduce', 'sort',
]);
const ARITHMETIC = new Set(['+', '-', '*', '/']);
const COMPARISONS = new Set(['<', '<=', '>', '>=', '===', '!==', '==', '!=']);
const ASSIGN_ARITHMETIC = new Set(['+=', '-=', '*=', '/=']);
const PRAGMA = /\/\/\s*facts:\s*presentation\b(?:\s*\(([^)]*)\))?/;

// Every `// facts: presentation` exemption in use, by file and reason.
const PINNED_EXEMPTIONS = new Set([]);

// The files the guard covers: every non-test UI source file that renders or
// reads report.json, plus the interactive report's generated client script.
// Files not listed here must not read an owned key (checked below).
const GUARDED_UI_FILES = [
  'pages/AnalysisResults-02.js',
  'pages/JobMonitoring.js',
  'pages/JobMonitoringSummary.js',
  'utils/ExportReport.js',
  'utils/ReportHtmlExport.js',
  'utils/reportFacts.js',
  'utils/riskAssessment.js',
  'utils/tcoAnalysis.js',
  'utils/cacheLayer.js',
  'utils/assignmentSummary.js',
  'utils/postSchemaReroutes.js',
  'utils/rankingConfidence.js',
  'utils/workloadFitLabels.js',
  'utils/optimizationLabels.js',
  'utils/auroraDesign.js',
  'utils/summaryFallback.js',
  'components/ChartSankey-01.js',
  'components/SummaryFallbackNotice.js',
  'components/QueryDecisionTrace.js',
  'components/QueryRoutingTable.js',
  'components/CloseCallCallout.js',
  'utils/queryRouting.js',
];

function figureFields(schema, out) {
  // schema: the contract snapshot's node (kind scalar/array/object/anyOf).
  if (!schema) return;
  if (schema.kind === 'anyOf') {
    schema.members.forEach(m => figureFields(m, out));
  } else if (schema.kind === 'array') {
    figureFields(schema.items, out);
  } else if (schema.kind === 'object' && schema.properties) {
    Object.entries(schema.properties).forEach(([name, f]) => {
      if (isFigure(f.schema)) out.add(name);
      figureFields(f.schema, out);
    });
  }
}

function isFigure(schema) {
  const kinds = schema.kind === 'anyOf' ? schema.members : [schema];
  return kinds.some(k => {
    if (k.kind === 'scalar') return k.type === 'integer' || k.type === 'number';
    if (k.kind === 'object') {
      if (k.additional) return k.additional.kind === 'scalar' && ['integer', 'number'].includes(k.additional.type);
      return true; // a nested record
    }
    if (k.kind === 'array') return k.items && k.items.kind === 'object';
    return false;
  });
}

function loadOwnedKeys() {
  const snap = JSON.parse(fs.readFileSync(SNAPSHOT, 'utf8'));
  const fields = snap['synthesis_output.SynthesisOutputContract'].fields;
  const out = new Set();
  figureFields(fields.facts.schema, out);
  NOT_A_FIGURE.forEach(k => out.delete(k));
  INPUT_KEYS.forEach(k => out.add(k));
  return out;
}

const OWNED_KEYS = loadOwnedKeys();

function parseSource(code) {
  return parse(code, {
    sourceType: 'unambiguous',
    plugins: ['jsx', 'optionalChaining', 'classProperties'],
    errorRecovery: false,
  });
}

function children(node) {
  const keys = t.VISITOR_KEYS[node.type] || [];
  const out = [];
  keys.forEach(k => {
    const v = node[k];
    if (Array.isArray(v)) v.forEach(c => c && out.push(c));
    else if (v) out.push(v);
  });
  return out;
}

function* walk(node) {
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    stack.push(...children(n));
  }
}

// Like walk, minus lookup keys: in `x[k]` the key selects a value and is not
// the value, so a tainted key does not make the value a fact.
function* walkData(node) {
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    if (t.isMemberExpression(n) || t.isOptionalMemberExpression(n)) {
      stack.push(n.object);
      if (n.computed && !t.isStringLiteral(n.property) && !t.isNumericLiteral(n.property)) continue;
    } else if (t.isFunction(n)) {
      // A function value is not its body: what it returns is only known when
      // it is called (a callback over a source is handled where it is passed).
      continue;
    } else if ((t.isCallExpression(n) || t.isOptionalCallExpression(n)) && calleeName(n) === 'useMemo'
      && n.arguments.length && t.isFunction(n.arguments[0])) {
      // useMemo(() => expr, deps): the value is what the callback returns.
      const cb = n.arguments[0];
      if (t.isBlockStatement(cb.body)) {
        for (const r of walk(cb.body)) if (t.isReturnStatement(r) && r.argument) stack.push(r.argument);
      } else {
        stack.push(cb.body);
      }
    } else {
      stack.push(...children(n));
    }
  }
}

function propName(m) {
  if (!m.computed && t.isIdentifier(m.property)) return m.property.name;
  if (m.computed && t.isStringLiteral(m.property)) return m.property.value;
  return null;
}

function calleeName(call) {
  const c = call.callee;
  if (t.isIdentifier(c)) return c.name;
  if (t.isMemberExpression(c) || t.isOptionalMemberExpression(c)) return propName(c);
  return null;
}

const STRING_CALLS = new Set([
  'escapeHtml', 'String', 'join', 'toFixed', 'toLocaleString', 'toUpperCase', 'toLowerCase', 'slice',
  'substring', 'charAt', 'trim', 'replace', 'jsArg', 'html', 't', 'jsonForScript',
]);

// A string, template, join or concatenation holding one: `+` on it formats.
function stringy(n) {
  if (t.isStringLiteral(n) || t.isTemplateLiteral(n)) return true;
  if (t.isBinaryExpression(n) && n.operator === '+') return stringy(n.left) || stringy(n.right);
  if (t.isConditionalExpression(n)) return stringy(n.consequent) || stringy(n.alternate);
  if (t.isLogicalExpression(n)) return stringy(n.left) && stringy(n.right);
  if (t.isParenthesizedExpression(n)) return stringy(n.expression);
  if (t.isCallExpression(n) || t.isOptionalCallExpression(n)) {
    const name = calleeName(n) || '';
    return STRING_CALLS.has(name) || /(Html|Badge|Label|Text)$/.test(name);
  }
  return false;
}

const isMember = n => t.isMemberExpression(n) || t.isOptionalMemberExpression(n);
const isCall = n => t.isCallExpression(n) || t.isOptionalCallExpression(n);
const isFn = n => t.isFunction(n);

// The nodes of one function's own body: nested functions are their own scope
// (analyzed with the names this scope has tainted), so a loop variable called
// `a` in one callback does not make an unrelated `a` in another a fact.
function* walkOwn(root) {
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    for (const c of children(n)) {
      if (!isFn(c)) stack.push(c);
    }
  }
}

class Analysis {
  constructor(code, fnNode, parent, parents) {
    this.code = code;
    this.parents = parents;
    this.fn = fnNode;
    this.facts = new Set(parent ? parent.facts : ['facts']);
    this.tainted = new Set(parent ? parent.tainted : []);
    this.callbackParams = new Map();
    if (parent && parent.callbackParams.has(fnNode)) {
      parent.callbackParams.get(fnNode).forEach(x => this.tainted.add(x));
    }
    this.nodes = [...walkOwn(fnNode)];
    this.run();
  }

  text(n) {
    return this.code.slice(n.start, n.end);
  }

  // The facts block: `x.facts`, `waveFacts(...)`, a name assigned from either,
  // or a `facts` identifier / parameter.
  isFactsBlock(n) {
    if (t.isIdentifier(n)) return this.facts.has(n.name);
    if (isMember(n)) return propName(n) === 'facts';
    if (isCall(n)) return LIST_HELPERS.has(calleeName(n)) && calleeName(n) === 'waveFacts';
    if (t.isLogicalExpression(n)) return this.isFactsBlock(n.left) || this.isFactsBlock(n.right);
    if (t.isConditionalExpression(n)) return this.isFactsBlock(n.consequent) || this.isFactsBlock(n.alternate);
    if (t.isParenthesizedExpression(n) || t.isTSNonNullExpression(n)) return this.isFactsBlock(n.expression);
    return false;
  }

  isSource(n) {
    if (t.isIdentifier(n)) return this.tainted.has(n.name);
    if (isMember(n)) {
      if (this.isFactsBlock(n.object)) return true;
      const k = propName(n);
      return k !== null && OWNED_KEYS.has(k);
    }
    if (isCall(n)) return LIST_HELPERS.has(calleeName(n));
    return false;
  }

  contains(n) {
    for (const x of walkData(n)) if (this.isSource(x)) return true;
    return false;
  }

  run() {
    const nodes = this.nodes;
    let changed = true;
    while (changed) {
      const before = this.tainted.size + this.facts.size;
      for (const n of nodes) {
        if (t.isVariableDeclarator(n) && n.init && t.isObjectPattern(n.id)) {
          this.destructure(n.id, this.isFactsBlock(n.init), this.contains(n.init));
        } else if (t.isVariableDeclarator(n) && n.init) {
          const names = bound(n.id);
          if (this.isFactsBlock(n.init)) names.forEach(x => this.facts.add(x));
          if (this.contains(n.init)) names.forEach(x => this.tainted.add(x));
        } else if (t.isAssignmentExpression(n) && n.operator === '=') {
          const names = bound(n.left);
          if (this.isFactsBlock(n.right)) names.forEach(x => this.facts.add(x));
          if (this.contains(n.right)) names.forEach(x => this.tainted.add(x));
        } else if (t.isAssignmentExpression(n) && ASSIGN_ARITHMETIC.has(n.operator)) {
          if (!stringy(n.right) && this.contains(n.right)) bound(n.left).forEach(x => this.tainted.add(x));
        } else if ((t.isForOfStatement(n) || t.isForInStatement(n)) && this.contains(n.right)) {
          bound(t.isVariableDeclaration(n.left) ? n.left.declarations[0].id : n.left).forEach(x => this.tainted.add(x));
          this.collectInto(n.body);
        } else if (isCall(n) && isMember(n.callee) && ARRAY_WALKERS.has(propName(n.callee))
          && this.contains(n.callee.object)) {
          n.arguments.filter(isFn).forEach(cb => {
            // The element is the first parameter (the second of a reduce);
            // the index and the array are positions, not data.
            const element = propName(n.callee) === 'reduce' ? cb.params[1] : cb.params[0];
            const names = element ? bound(element) : [];
            this.callbackParams.set(cb, new Set(names));
            this.collectInto(cb.body);
          });
        }
      }
      changed = this.tainted.size + this.facts.size !== before;
    }
  }

  // `const { risks } = ra`, `const { engine_count } = facts`,
  // `const { facts: { views } } = synthesis`, `const { length } = fact.list`:
  // a name bound off the facts block, off a source, or under an owned key is a
  // source; `facts` under any key is the facts block.
  destructure(pattern, inFacts, inSource) {
    for (const prop of pattern.properties) {
      if (t.isRestElement(prop)) {
        if (inFacts || inSource) bound(prop.argument).forEach(x => this.tainted.add(x));
        continue;
      }
      const key = t.isIdentifier(prop.key) && !prop.computed ? prop.key.name
        : (t.isStringLiteral(prop.key) ? prop.key.value : null);
      const value = t.isAssignmentPattern(prop.value) ? prop.value.left : prop.value;
      const owned = inFacts || inSource || (key !== null && OWNED_KEYS.has(key));
      const isFactsKey = key === 'facts';
      if (owned) bound(value).forEach(x => this.tainted.add(x));
      if (isFactsKey && t.isIdentifier(value)) this.facts.add(value.name);
      if (t.isObjectPattern(value)) this.destructure(value, inFacts || isFactsKey, owned || isFactsKey);
    }
  }

  // A list or set filled from what a loop walks is counted like the loop
  // itself (`highs.push(r)` then `highs.length`).
  collectInto(body) {
    for (const n of walk(body)) {
      if (isCall(n) && isMember(n.callee) && ['push', 'add', 'unshift'].includes(propName(n.callee))) {
        // `highs.push(r)`, `bySeverity[sev].push(r)`: the collection it fills.
        let root = n.callee.object;
        while (isMember(root)) root = root.object;
        if (t.isIdentifier(root)) this.tainted.add(root.name);
      }
    }
  }

  isEmptinessCheck(len, parent) {
    if (!parent) return false;
    if (t.isUnaryExpression(parent) && parent.operator === '!') return true;
    if (t.isIfStatement(parent) && parent.test === len) return true;
    if (t.isConditionalExpression(parent) && parent.test === len) return true;
    if (t.isLogicalExpression(parent)) return true;
    // A comparison with a number (`xs.length === 0`, `rs.length > 3`) shows no figure.
    if (t.isBinaryExpression(parent) && COMPARISONS.has(parent.operator)) {
      return t.isNumericLiteral(parent.left === len ? parent.right : parent.left);
    }
    return false;
  }

  // Arithmetic that is presentation, narrowly: a sort comparator subtracting the
  // same field of its two parameters (`(a, b) => b.score - a.score`) and scaling
  // an element's score to a percentage (`x.score * 100`).
  isPresentationArithmetic(n, parents) {
    const member = x => isMember(x) && !x.computed && t.isIdentifier(x.object);
    if (n.operator === '*') {
      const [a, b] = [n.left, n.right];
      return (member(a) && propName(a) === 'score' && t.isNumericLiteral(b, { value: 100 }))
        || (member(b) && propName(b) === 'score' && t.isNumericLiteral(a, { value: 100 }));
    }
    if (n.operator !== '-' || !member(n.left) || !member(n.right)
      || propName(n.left) !== propName(n.right)) return false;
    let fn = parents.get(n);
    while (fn && !isFn(fn)) fn = parents.get(fn);
    const call = fn && parents.get(fn);
    if (!call || !isCall(call) || !isMember(call.callee) || propName(call.callee) !== 'sort') return false;
    const params = fn.params.filter(p => t.isIdentifier(p)).map(p => p.name);
    const used = [n.left.object.name, n.right.object.name].sort();
    return params.length === 2 && used.join() === [...params].sort().join();
  }

  violations() {
    const hits = [];
    const parents = this.parents;
    const arithmetic = n => t.isBinaryExpression(n) && ARITHMETIC.has(n.operator)
      && !stringy(n.left) && !stringy(n.right);

    for (const n of this.nodes) {
      if (isMember(n) && ['length', 'size'].includes(propName(n)) && this.contains(n.object)
        && !this.isEmptinessCheck(n, parents.get(n))) {
        hits.push(n);
      } else if (t.isVariableDeclarator(n) && t.isObjectPattern(n.id) && n.init && this.contains(n.init)
        && n.id.properties.some(p => t.isObjectProperty(p) && ['length', 'size'].includes(
          t.isIdentifier(p.key) ? p.key.name : p.key.value))) {
        hits.push(n); // `const { length } = fact.list` is `fact.list.length`
      } else if (isCall(n) && isMember(n.callee) && propName(n.callee) === 'reduce' && this.contains(n.callee.object)) {
        hits.push(n);
      } else if (isCall(n) && isMember(n.callee) && t.isIdentifier(n.callee.object, { name: 'Math' })
        && ['max', 'min'].includes(propName(n.callee)) && n.arguments.some(a => this.contains(a))) {
        hits.push(n);
      } else if (arithmetic(n) && (this.contains(n.left) || this.contains(n.right))
        && !this.isPresentationArithmetic(n, parents)) {
        hits.push(n);
      } else if (t.isAssignmentExpression(n) && ASSIGN_ARITHMETIC.has(n.operator)
        && !(n.operator === '+=' && stringy(n.right)) && this.contains(n.right)) {
        hits.push(n);
      } else if ((t.isForOfStatement(n) || t.isForInStatement(n)) && this.contains(n.right)) {
        hits.push(...this.accumulators(n.body));
      } else if (isCall(n) && isMember(n.callee) && ARRAY_WALKERS.has(propName(n.callee))
        && this.contains(n.callee.object)) {
        n.arguments.filter(isFn).forEach(cb => hits.push(...this.accumulators(cb.body)));
      }
    }
    return hits;
  }

  // Counting what the loop walks: `n++`, `n += 1`, `d[k] = (d[k] || 0) + 1`.
  accumulators(body) {
    const out = [];
    for (const n of walk(body)) {
      if (t.isUpdateExpression(n)) out.push(n);
      else if (t.isAssignmentExpression(n) && ASSIGN_ARITHMETIC.has(n.operator)
        && !stringy(n.right)) {
        out.push(n);
      } else if (t.isAssignmentExpression(n) && n.operator === '=' && t.isBinaryExpression(n.right)
        && ARITHMETIC.has(n.right.operator)) {
        const target = this.text(n.left);
        const objText = isMember(n.left) ? this.text(n.left.object) : null;
        const reads = [...walk(n.right)].some(x => isMember(x) && (this.text(x) === target
          || (objText !== null && this.text(x.object) === objText)))
          || [...walk(n.right)].some(x => t.isIdentifier(x) && x.name === target);
        if (reads) out.push(n);
      }
    }
    return out;
  }
}

// The plain names a binding target introduces (`a`, `[a, ...b]`, `{ k: a, ...b }`);
// `x.y = ...` binds nothing new.
function bound(target) {
  if (!target) return [];
  if (t.isIdentifier(target)) return [target.name];
  if (t.isAssignmentPattern(target)) return bound(target.left);
  if (t.isRestElement(target)) return bound(target.argument);
  if (t.isArrayPattern(target)) return target.elements.flatMap(bound);
  if (t.isObjectPattern(target)) {
    return target.properties.flatMap(p => (t.isRestElement(p) ? bound(p.argument) : bound(p.value)));
  }
  return [];
}

function violationsIn(code, label) {
  const ast = parseSource(code);
  const lines = code.split('\n');
  const found = new Map();
  const parents = new Map();
  for (const n of walk(ast.program)) children(n).forEach(c => parents.set(c, n));
  const analyze = (scope, parent) => {
    const a = new Analysis(code, scope, parent, parents);
    a.violations().forEach(hit => {
      const line = hit.loc.start.line;
      const m = PRAGMA.exec(lines[line - 1]);
      if (m && (m[1] || '').trim()) return;
      found.set(`${line}:${code.slice(hit.start, hit.end).replace(/\s+/g, ' ').slice(0, 100)}`, line);
    });
    a.nodes.flatMap(n => children(n)).filter(isFn).forEach(child => analyze(child, a));
  };
  analyze(ast.program, null);
  return [...found.entries()].sort((a, b) => a[1] - b[1]).map(([k]) => `${label}:${k}`);
}

function guardedSources() {
  const files = GUARDED_UI_FILES.map(f => [`src/ui/src/${f}`, `${UI_SRC}/${f}`]);
  files.push(['src/report/templates/analysis_report.js', TEMPLATE_JS]);
  return files;
}

describe('renderers only format (#748, ADR-032)', () => {
  test('no UI code does arithmetic on a figure the facts layer owns', () => {
    const found = guardedSources().flatMap(([label, file]) => violationsIn(fs.readFileSync(file, 'utf8'), label));
    expect(found).toEqual([]);
  });

  test('the owned keys come from the facts contract', () => {
    [
      'open_risk_count', 'open_risks_by_severity', 'resolved_risk_count', 'engine_count',
      'migration_wave_count', 'projected_monthly_cost_usd', 'tables_migrate', 'tied_query_count',
      'post_schema_reroute_count', 'views', 'tables_only', 'table_basis', 'no_migration',
      'moving_tables', 'kept_in_sync', 'served_tables', 'reality_check_decisions',
    ].forEach(k => expect(OWNED_KEYS.has(k)).toBe(true));
    NOT_A_FIGURE.forEach(k => expect(OWNED_KEYS.has(k)).toBe(false));
  });

  test('presentation exemptions carry a reason and are pinned', () => {
    const found = new Set();
    guardedSources().forEach(([label, file]) => {
      fs.readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
        const m = PRAGMA.exec(line);
        if (m) {
          const reason = (m[1] || '').trim();
          if (!reason) throw new Error(`${label}:${i + 1}: '// facts: presentation' needs a reason`);
          found.add(`${label}|${reason}`);
        }
      });
    });
    expect(found).toEqual(PINNED_EXEMPTIONS);
  });

  test('every other UI source file reads no owned figure', () => {
    // A new file that renders report.json joins GUARDED_UI_FILES with the code.
    const listed = new Set(GUARDED_UI_FILES.map(f => `${UI_SRC}/${f}`));
    const stray = [];
    const visit = dir => fs.readdirSync(dir, { withFileTypes: true }).forEach(e => {
      const p = `${dir}/${e.name}`;
      if (e.isDirectory()) {
        if (!['__tests__', 'locales', 'node_modules'].includes(e.name)) visit(p);
      } else if (/\.jsx?$/.test(e.name) && !listed.has(p)) {
        const ast = parseSource(fs.readFileSync(p, 'utf8'));
        const reads = [...walk(ast.program)].filter(n => isMember(n)
          && ['risks', 'resolved_risks', 'cost_breakdown', 'table_mappings', 'ranking', 'tied_queries',
            'table_owners', 'owned_counts_by_engine', 'facts'].includes(propName(n)));
        if (reads.length) stray.push(path.relative(UI_SRC, p));
      }
    });
    visit(UI_SRC);
    expect(stray).toEqual([]);
  });

  test('the wave-headline rule is not in any UI code: waves read facts.waves only', () => {
    // #581 came back for old reports because ExportReport.js and
    // ReportHtmlExport.js each subtracted table_owners from table_count. The
    // inputs of that rule are read nowhere on the UI side now; the phrasing
    // (waveTableStats) is reportFacts.js's, mirrored as text by the exporter.
    const withRule = guardedSources().filter(([, file]) => /table_owners|owned_counts_by_engine/.test(fs.readFileSync(file, 'utf8')));
    expect(withRule.map(([label]) => label)).toEqual([]);
  });

  describe('the guard itself', () => {
    const PROBE = `
function render(synthesis, wave) {
  const ra = synthesis.risk_assessment || {};
  const risks = (ra.risks || []).filter(r => r);
  const a = risks.length;
  const facts = synthesis.facts;
  const b = facts.engines.length;
  const wf = waveFacts(synthesis, wave);
  const c = wf.moving_tables + wf.kept_in_sync_tables;
  const d = Object.keys(facts.open_risks_by_severity).length;
  let n = 0;
  risks.forEach(r => { n++; });
  const by = {};
  risks.forEach(r => { by[r.severity] = (by[r.severity] || 0) + 1; });
  const total = (synthesis.tco_analysis.cost_breakdown || []).reduce((s, cb) => s + cb.monthly_cost_usd, 0);
  const highs = [];
  for (const r of risks) { highs.push(r); }
  const e = highs.length;
  const f = Math.max(...Object.values(facts.open_risks_by_severity));
  const label = '(' + risks.length + ')';
  const bare = risks.length; // facts: presentation
  const ok = risks.length > 0;
  const ok2 = !facts.engines.length;
  const shown = ra.names.slice(0, 20);
  const more = ra.names.length - 20;
  const read = facts.open_risk_count + ' open';
  return [a, b, c, d, n, by, total, e, f, label, bare, ok, ok2, shown, more, read];
}
`;
    test('flags every counting shape and nothing else', () => {
      const lines = PROBE.split('\n');
      const flagged = new Set(violationsIn(PROBE, 'probe').map(v => Number(v.split(':')[1])));
      const lineOf = text => lines.findIndex(l => l.includes(text)) + 1;
      const expected = new Set([
        lineOf('const a = risks.length'),
        lineOf('const b = facts.engines.length'),
        lineOf('const c = wf.moving_tables'),
        lineOf('const d = Object.keys'),
        lineOf('n++'),
        lineOf('|| 0) + 1'),
        lineOf('.reduce('),
        lineOf('const e = highs.length'),
        lineOf('const f = Math.max'),
        lineOf("const label = '(' + risks.length"),
        lineOf('const bare = risks.length'),
      ]);
      expect([...flagged].sort()).toEqual([...expected].sort());
    });

    const flaggedLines = (code) => new Set(violationsIn(code, 'probe').map(v => Number(v.split(':')[1])));
    const lineIn = (code, text) => code.split('\n').findIndex(l => l.includes(text)) + 1;

    test('object destructuring carries the taint (#748 round 2)', () => {
      const code = `
function render(synthesis, ra) {
  const { risks } = synthesis.risk_assessment;
  const a = risks.length;
  const { open_risk_count, engine_count } = synthesis.facts;
  const b = open_risk_count + engine_count;
  const { facts: { tables_migrate } } = synthesis;
  const c = tables_migrate + 1;
  const { length } = synthesis.risk_assessment.risks;
  const d = length;
  const { risks: openRisks = [] } = ra.risk_assessment;
  const e = openRisks.length;
  const { overall_risk_level, title } = ra;
  const ok = title.length + 1;
  return [a, b, c, d, e, overall_risk_level, ok];
}`;
      const flagged = flaggedLines(code);
      ['const a = risks.length', 'const b = open_risk_count', 'const c = tables_migrate', 'const { length } =', 'const e = openRisks.length']
        .forEach(text => expect([text, flagged.has(lineIn(code, text))]).toEqual([text, true]));
      expect(flagged.has(lineIn(code, 'const ok'))).toBe(false);
    });

    test('a comparison against a number, a sort comparator and score scaling are presentation; recomputing is not', () => {
      const allowed = `
function render(synthesis) {
  const rs = synthesis.risk_assessment.risks;
  const many = rs.length > 3;
  const few = 3 >= rs.length;
  const sorted = [...synthesis.ranking].sort((a, b) => b.score - a.score);
  const pct = synthesis.ranking.map(x => Math.round(x.score * 100));
  return [many, few, sorted, pct];
}`;
      expect(violationsIn(allowed, 'probe')).toEqual([]);
      const caught = `
function render(synthesis, facts) {
  const rs = synthesis.risk_assessment.risks;
  const a = rs.length + 1;
  const b = [...synthesis.ranking].sort((x, y) => facts.engine_count - rs.length);
  const c = synthesis.ranking.map(x => x.workload_percent * 100);
  const d = synthesis.ranking.length * 100;
  const e = [...synthesis.ranking].sort((x, y) => x.score + y.score);
  return [a, b, c, d, e];
}`;
      const flagged = flaggedLines(caught);
      ['const a =', 'const b =', 'const c =', 'const d =', 'const e ='].forEach(text =>
        expect([text, flagged.has(lineIn(caught, text))]).toEqual([text, true]));
    });

    // Each real file passes as shipped; putting back the count the facts layer
    // owns makes the guard fail, so the passing run above is not vacuous.
    test.each([
      ['src/ui/src/utils/ReportHtmlExport.js', 'const openRiskCount = openRiskCountOf(synthesis, risks);', 'const openRiskCount = risks.length;'],
      ['src/ui/src/utils/ReportHtmlExport.js', '(${tableMappingCount})', '(${tableMappings.length})'],
      ['src/ui/src/pages/AnalysisResults-02.js', 'count: severityCount }', 'count: risksAtSeverity.length }'],
      ['src/ui/src/pages/AnalysisResults-02.js', 'count: resolvedRiskCountOf(synthesis, resolvedRisksList)', 'count: resolvedRisksList.length'],
      ['src/report/templates/analysis_report.js', "' (' + openRiskCountAt(sev, risks) + ')", "' (' + risks.length + ')"],
      ['src/report/templates/analysis_report.js', "resolvedRiskCount(resolvedRisks) + ' resolved", "resolvedRisks.length + ' resolved"],
      ['src/ui/src/utils/cacheLayer.js', 'finite(facts?.projected_monthly_cost_usd);', 'finite(facts?.projected_monthly_cost_usd) ?? items.reduce((s, cb) => s + cb.monthly_cost_usd, 0);'],
    ])('%s: %s is caught when it is a recomputation', (file, fact, recomputed) => {
      const real = fs.readFileSync(`${REPO}/${file}`, 'utf8');
      expect(violationsIn(real, file)).toEqual([]);
      expect(real).toContain(fact);
      expect(violationsIn(real.replace(fact, recomputed), file).length).toBeGreaterThan(0);
    });
  });
});
