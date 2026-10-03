/**
 * #242: the HTML exports must render report.json text as text, never as markup.
 *
 * report.json carries LLM-written prose (risks, mitigations, trade-offs, summaries)
 * and customer-derived names (tables, patterns, SQL). Each test feeds hostile strings
 * through an exporter and asserts the output contains no live tags or attributes.
 */
import { escapeHtml, html, jsonForScript, safeUrl } from '../escapeHtml';
import { buildReportHtml } from '../ReportHtmlExport';
import { generateHTMLReport } from '../ExportReport';

const IMG = '<img src=x onerror=alert(1)>';
const BREAKOUT = '"><script>alert(1)</script>';
const JS_URL = 'javascript:alert(1)';
const SQUOTE = "');alert(1);//";
const ATTR = '" onmouseover="alert(1)';
const END_SCRIPT = '</script><script>alert(1)</script>';
const HOSTILE = [IMG, BREAKOUT, JS_URL, SQUOTE, ATTR, END_SCRIPT];
const hostile = (i) => HOSTILE[i % HOSTILE.length];

const ACTIVE_TAGS = 'script, img, iframe, object, embed, svg script, link, meta[http-equiv], base, form';

/** Every attribute in the document whose name starts with "on". */
const eventAttributes = (doc) => [...doc.querySelectorAll('*')].flatMap(el =>
  [...el.attributes].filter(a => a.name.toLowerCase().startsWith('on')).map(a => ({ el, name: a.name, value: a.value })));

const urlAttributes = (doc) => [...doc.querySelectorAll('[href], [src], [action], [formaction]')].flatMap(el =>
  ['href', 'src', 'action', 'formaction'].filter(n => el.hasAttribute(n)).map(n => el.getAttribute(n)));

describe('escapeHtml', () => {
  it('escapes all five HTML-significant characters', () => {
    expect(escapeHtml(`<a href="x" title='y'>&</a>`)).toBe('&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
  });

  it('stringifies values and maps null/undefined to empty', () => {
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
    expect(escapeHtml(0)).toBe('0');
    expect(escapeHtml(12.5)).toBe('12.5');
  });
});

describe('html tag', () => {
  it('escapes interpolations, keeps nested fragments and joins arrays', () => {
    const rows = [IMG, 'ok'].map(v => html`<td>${v}</td>`);
    const out = html`<tr title="${ATTR}">${rows}</tr>`.toString();
    expect(out).toBe('<tr title="&quot; onmouseover=&quot;alert(1)"><td>&lt;img src=x onerror=alert(1)&gt;</td><td>ok</td></tr>');
  });

  it('does not treat plain strings that look like markup as markup', () => {
    expect(html`${'<b>x</b>'}`.toString()).toBe('&lt;b&gt;x&lt;/b&gt;');
  });
});

describe('safeUrl', () => {
  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)',
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    '\u0001javascript:alert(1)',
    'vbscript:msgbox(1)',
    'data:text/html,<script>alert(1)</script>',
  ])('neutralises %j', (url) => {
    expect(safeUrl(url)).toBe('#');
  });

  it.each(['https://aws.amazon.com/', 'http://localhost:3000/x', 'mailto:a@b.c', '/relative', 'report.html', '#frag'])(
    'keeps %j', (url) => {
      expect(safeUrl(url)).toBe(url);
    });

  it('maps null to "#"', () => {
    expect(safeUrl(null)).toBe('#');
  });
});

describe('jsonForScript', () => {
  it('cannot end a script element and stays valid JSON', () => {
    const value = { sql: END_SCRIPT, sep: '\u2028\u2029' };
    const text = jsonForScript(value);
    expect(text).not.toMatch(/<\/script/i);
    expect(text).not.toMatch(/[\u2028\u2029]/);
    expect(JSON.parse(text)).toEqual(value);
  });
});

// --------------------------------------------------------------------------------
// ReportResults "Export to HTML" (static document, no scripts at all)
// --------------------------------------------------------------------------------

const hostileResults = () => ({
  synthesis: {
    database_name: BREAKOUT,
    summary: IMG,
    summary_deterministic: ATTR,
    timestamp: '2026-10-03T12:00:00Z',
    ranking: [{ target: IMG, confidence_score: BREAKOUT, tables_analyzed: ATTR, access_patterns: SQUOTE, monthly_cost_usd: 'not-a-number' }],
    table_mappings: [{ source_table: IMG, recommended_database: BREAKOUT, target_table: JS_URL, aggregate_pattern: ATTR, confidence_score: SQUOTE }],
    risk_assessment: {
      overall_risk_level: BREAKOUT,
      risks: [{ risk_id: ATTR, engine: IMG, severity: 'HIGH', risk_type: JS_URL, description: `[dynamodb] ${IMG}`, mitigation: BREAKOUT }],
      resolved_risks: [{ engine: ATTR, resolved_on: IMG, severity: BREAKOUT, description: `[x] ${END_SCRIPT}`, reason: JS_URL }],
    },
    trade_offs: [
      { engine: IMG, description: BREAKOUT, impact: ATTR, source_tables: [IMG], target_tables: [JS_URL] },
      `[dynamodb] ${IMG}`,
    ],
    tco_analysis: { current_monthly_cost: 10, projected_monthly_cost: 5, savings_percent: 50 },
    query_groups: [{
      group_name: IMG,
      engines: [BREAKOUT, ATTR],
      total_design_rps: 3,
      source_queries: [1],
      access_patterns: [{ pattern_id: IMG, operation: BREAKOUT, table_name: ATTR, design_rps: 1, description: END_SCRIPT }],
    }],
  },
  triage_summary: { database_name: IMG, source_database_type: JS_URL },
});

describe('buildReportHtml (ReportResults export)', () => {
  // A translation can carry interpolated data too; prove its output is escaped.
  const t = (key) => (key.endsWith('resolved-export-description') ? IMG : key);
  const parse = (text) => new DOMParser().parseFromString(text, 'text/html');

  it('renders hostile report text as inert text', () => {
    const doc = parse(buildReportHtml({ resultsData: hostileResults(), jobId: SQUOTE, t }));

    expect(doc.querySelectorAll(ACTIVE_TAGS.replace(', meta[http-equiv]', ''))).toHaveLength(0);
    expect(eventAttributes(doc)).toEqual([]);
    expect(urlAttributes(doc).filter(u => /script:/i.test(u))).toEqual([]);

    // The literal characters are what the reader sees.
    const text = doc.body.textContent;
    for (const payload of HOSTILE) expect(text).toContain(payload);
    expect(doc.title).toBe(`Database Modernization Report - ${BREAKOUT}`);
  });

  it('the only <meta> tags are the two static ones', () => {
    const doc = parse(buildReportHtml({ resultsData: hostileResults(), jobId: 'job', t }));
    expect([...doc.querySelectorAll('meta')].map(m => m.outerHTML)).toEqual([
      '<meta charset="UTF-8">',
      '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
    ]);
  });

  it('still renders an ordinary report', () => {
    const resultsData = {
      synthesis: {
        database_name: 'wordpress',
        ranking: [{ target: 'dynamodb', confidence_score: 80, monthly_cost_usd: 12.345 }],
        risk_assessment: { risks: [{ severity: 'HIGH', description: '[dynamodb] Hot partition', mitigation: 'Shard keys' }] },
      },
    };
    const doc = parse(buildReportHtml({ resultsData, jobId: 'abc', t }));
    expect(doc.querySelector('h1').textContent).toBe('wordpress Analysis');
    expect(doc.body.textContent).toContain('$12.35/mo');
    expect(doc.body.textContent).toContain('Hot partition');
  });
});

// --------------------------------------------------------------------------------
// Interactive report (ExportReport.js; its client script is also the ATX template)
// --------------------------------------------------------------------------------

const hostileExportData = () => {
  const qids = HOSTILE.map((p, i) => `q${i}${p}`);
  const engines = ['dynamodb', IMG, ATTR, SQUOTE];
  return {
    jobId: BREAKOUT,
    exportDate: '2026-10-03T12:00:00Z',
    collector: {},
    results: {
      synthesis: {
        database_name: IMG,
        summary: END_SCRIPT,
        reality_check: { after_distribution: Object.fromEntries(engines.map((e, i) => [e, 10 + i])) },
        tco_analysis: { cost_breakdown: engines.map(e => ({ database: e, monthly_cost_usd: 1, pricing_mode: ATTR })) },
      },
    },
    schemaDesigns: engines.map((engine, ei) => ({
      target_type: engine,
      content: {
        access_patterns: HOSTILE.map((p, i) => ({
          pattern_id: `${ei}-${i}${p}`,
          operation: hostile(i + 1),
          source_tables: [`s.${hostile(i + 2)}`],
          table_name: hostile(i + 3),
          description: hostile(i + 4),
          gsi_name: hostile(i + 5),
          partition_key: IMG,
          source_query: END_SCRIPT,
          query_ids: qids,
        })),
        trade_offs: [
          { description: IMG, impact: ATTR, query_ids: qids },
          { description: `[PE note] ${BREAKOUT}`, impact: SQUOTE, query_ids: qids },
        ],
      },
    })),
    queryJourneys: {
      items: qids.map(q => ({
        query_id: q,
        source: { query_type: IMG, query_text: END_SCRIPT, tables_accessed: [ATTR], performance: { [IMG]: BREAKOUT }, characteristics: { k: SQUOTE } },
        assignment: { assigned_engine: BREAKOUT, confidence: ATTR },
      })),
    },
  };
};

/**
 * Run the exported report's own script in this test's jsdom window.
 *
 * Jest's jsdom environment executes scripts ("runScripts: dangerously"), so the
 * report's markup is moved into this document and its inline script appended, which
 * is what a browser does when the file is opened. The Chart.js CDN tag is dropped
 * (no network) and Chart is stubbed. The report declares top-level consts, so it can
 * only be loaded once per test file.
 */
const loadInteractiveReport = (markup) => {
  const alerts = [];
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter(el => !el.src);
  expect(inline).toHaveLength(1);
  const code = inline[0].textContent;
  parsed.querySelectorAll('script').forEach(el => el.remove());

  window.alert = (...args) => alerts.push(args);
  window.Chart = class { destroy() {} };
  // showSourceTableDetails still reads a constant that no longer exists (#243);
  // stub it so the source-table rows can be clicked. Remove with the #243 fix.
  window.ENGINE_BADGE_CLASSES = {};
  jest.spyOn(console, 'log').mockImplementation(() => {});
  document.head.innerHTML = parsed.head.innerHTML;
  document.body.innerHTML = parsed.body.innerHTML;
  const script = document.createElement('script');
  script.textContent = code;
  document.body.appendChild(script);
  document.dispatchEvent(new Event('DOMContentLoaded'));
  return { window, alerts };
};

/** Click every element with an inline handler, repeatedly, so modals get rendered too. */
const clickEverything = (window) => {
  const seen = new Set();
  for (let round = 0; round < 4; round += 1) {
    const targets = [...window.document.querySelectorAll('[onclick]')].filter(el => !seen.has(el));
    targets.forEach(el => {
      seen.add(el);
      el.dispatchEvent(new window.MouseEvent('click', { bubbles: false }));
    });
  }
  return seen.size;
};

describe('generateHTMLReport (interactive export)', () => {
  it('embeds DATA so that "</script>" cannot end the script element', () => {
    const markup = generateHTMLReport(hostileExportData());
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    // Chart.js CDN tag + the one inline report script, nothing injected.
    expect(doc.querySelectorAll('script')).toHaveLength(2);
    expect(doc.querySelectorAll('img')).toHaveLength(0);
    expect(doc.querySelector('.section-body').textContent).toBe(END_SCRIPT);
    expect(doc.querySelector('.meta-pair-value').textContent).toBe(BREAKOUT);
  });

  describe('in a browser', () => {
    let loaded;
    beforeAll(() => {
      loaded = loadInteractiveReport(generateHTMLReport(hostileExportData()));
    });

    it('renders hostile data without live tags, attributes or handler breakouts', () => {
      const { window: win, alerts } = loaded;
      const doc = win.document;
      const browse = (mode) => doc.querySelectorAll('.toggle-btn')[mode === 'pattern' ? 0 : 1].dispatchEvent(new win.MouseEvent('click'));
      browse('pattern');
      expect(doc.querySelectorAll('#access-patterns-container tbody tr').length).toBeGreaterThan(0);

      const scriptCount = doc.querySelectorAll('script').length;
      // Pattern view, then the source-table view, clicking everything in each.
      let clicked = clickEverything(win);
      browse('source');
      clicked += clickEverything(win);
      expect(clicked).toBeGreaterThan(20);

      expect(doc.querySelectorAll('script')).toHaveLength(scriptCount);
      expect(doc.querySelectorAll('img, iframe, object, embed')).toHaveLength(0);
      expect(eventAttributes(doc).filter(a => a.name !== 'onclick')).toEqual([]);
      expect(urlAttributes(doc).filter(u => /script:/i.test(u))).toEqual([]);
      // Any alert the page raised is its own "not found" notice, never alert(1).
      expect(alerts.filter(args => args[0] === 1)).toEqual([]);
    });

    it('passes the exact hostile value through inline handlers', () => {
      const { window: win, alerts } = loaded;
      const doc = win.document;
      doc.querySelectorAll('.toggle-btn')[0].dispatchEvent(new win.MouseEvent('click'));
      const row = doc.querySelector('#access-patterns-container tbody tr');
      alerts.length = 0;
      row.dispatchEvent(new win.MouseEvent('click'));
      expect(alerts).toEqual([]); // "Pattern not found" would mean the id was mangled
      expect(doc.getElementById('pattern-modal').style.display).toBe('flex');
      expect(doc.getElementById('pattern-modal-body').textContent).toContain(END_SCRIPT);
    });

    it('client-side escapeHtml matches the shared helper', () => {
      for (const value of [...HOSTILE, `&'"<>`, null, undefined, 42]) {
        expect(loaded.window.escapeHtml(value)).toBe(escapeHtml(value));
      }
    });
  });
});
