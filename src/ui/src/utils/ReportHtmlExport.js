/**
 * Builds the standalone HTML file behind ReportResults' "Export to HTML" button.
 *
 * Kept out of the page component so it is a pure function of report.json and can be
 * unit-tested without rendering the page. Every value from report.json -- risk prose,
 * mitigations, reasons, trade-offs, summaries (LLM-written) and table/pattern names
 * (customer-derived) -- is untrusted, so the document is assembled with the `html`
 * tag from ./escapeHtml, which HTML-escapes every interpolation by default (#242).
 */
import { html } from './escapeHtml';
import { splitRankingByRole, formatCacheLayerLine } from './cacheLayer';
import { analysisConfidence, confidenceText, hasRoutedConfidence } from './rankingConfidence';
import { displayEngine } from './engineNames';

// A wave with more tables than this shows "+N more" rather than every name
// inline (PR #315 review finding 14 -- discourse wave 4 lists 226 tables).
const MAX_INLINE_TABLES = 20;

const tablesSummary = (tables) => {
  const shown = tables.slice(0, MAX_INLINE_TABLES).join(', ');
  const more = tables.length > MAX_INLINE_TABLES ? ` (+${tables.length - MAX_INLINE_TABLES} more)` : '';
  return shown + more;
};

// Normalize a trade-off (structured object or legacy string) into a consistent shape.
export const normalizeTradeoff = (item, fallbackEngine = 'unknown') => {
  if (typeof item === 'object' && item !== null && item.description) {
    return {
      description: item.description,
      impact: item.impact || '',
      engine: item.engine || fallbackEngine,
      source_tables: item.source_tables || [],
      target_tables: item.target_tables || [],
      query_ids: item.query_ids || [],
    };
  }
  // Legacy string format: "[engine] text" or plain text
  const str = String(item);
  const engineMatch = str.match(/^\[(\w+)\]\s*/);
  const engine = engineMatch ? engineMatch[1] : fallbackEngine;
  const text = engineMatch ? str.replace(/^\[\w+\]\s*/, '') : str;
  return {
    description: text,
    impact: '',
    engine,
    source_tables: [],
    target_tables: [],
    query_ids: [],
  };
};

// A risk whose description has no real text after its "[engine] " prefix
// (or the legacy "[engine] unknown:" pattern -- see #210) must not be counted or
// rendered. Mirrors src/report/renderers.py's filtered_risks/_risk_has_content so
// the UI's risk count can't diverge from the decision report, engineering report
// and executive summary deck (#201 found exactly this kind of divergence: 9 vs 12).
export const riskHasContent = (description) => {
  if (!description) return false;
  const afterEngine = String(description).replace(/^\[[^\]]*\]\s*/, '');
  const body = afterEngine.toLowerCase().startsWith('unknown:')
    ? afterEngine.slice('unknown:'.length).trim()
    : afterEngine.trim();
  return body.length > 0;
};

// Number formatting that tolerates a non-numeric value in report.json instead of
// throwing (a string "12.5" would make `.toFixed` a TypeError and abort the export).
const fixed = (value, digits, fallback) => {
  const n = typeof value === 'number' ? value : Number.NaN;
  return Number.isFinite(n) ? n.toFixed(digits) : fallback;
};

const asArray = (value) => (Array.isArray(value) ? value : []);

/**
 * Return the exported report as an HTML string.
 *
 * @param {object} resultsData  the job's results (report.json payload)
 * @param {string} jobId        job identifier shown in the header
 * @param {function} t          i18next translate function
 * @param {Date} [now]          generation time for the footer (injectable for tests)
 */
export const buildReportHtml = ({ resultsData, jobId, t, now = new Date() }) => {
  const synthesis = resultsData?.synthesis || {};
  const triage = resultsData?.triage_summary || {};
  // #296 cache overlay: pull the trailing role: "cache_layer" ranking entry (if
  // any) out of the ranked owners so it renders as its own cache layer card
  // instead of a ranked engine with workload_percent 0 / "0%" confidence.
  const { owners: ownerRanking, cacheLayer } = splitRankingByRole(asArray(synthesis.ranking));
  const ranking = ownerRanking;
  const cacheLayerLine = formatCacheLayerLine(cacheLayer, { t, withLabel: false });
  const tableMappings = asArray(synthesis.table_mappings);
  const riskAssessment = synthesis.risk_assessment || {};
  const risks = asArray(riskAssessment.risks).filter(risk => riskHasContent(risk.description));
  // Analysis risks the effective assignment resolved (risk_assessment.resolved_risks).
  // Mirrors the "Resolved" tab on the page (see resolvedRisks in ReportResults.js).
  const resolvedRisks = asArray(riskAssessment.resolved_risks)
    .filter(risk => riskHasContent(risk.description))
    .map(risk => ({
      engine: risk.engine || null,
      resolved_on: risk.resolved_on || null,
      severity: risk.severity,
      description: String(risk.description).replace(/^\[\w+\]\s*/, ''),
      reason: risk.reason || '',
    }));
  const tradeoffs = asArray(synthesis.trade_offs);
  const tcoAnalysis = synthesis.tco_analysis || {};
  const queryGroups = asArray(synthesis.query_groups);
  // #225: the incremental migration roadmap synthesis writes to report.json.
  // Omitted (not a guessed placeholder) when the report has none.
  const migrationWaves = asArray(synthesis.migration_waves);
  const overallRisk = riskAssessment.overall_risk_level || 'MEDIUM';
  const alertClass = overallRisk === 'HIGH' ? 'error' : overallRisk === 'LOW' ? 'info' : 'warning';

  const doc = html`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Database Modernization Report - ${synthesis.database_name || 'Database'}</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: 'Amazon Ember', Arial, sans-serif; line-height: 1.6; max-width: 1400px; margin: 0 auto; padding: 20px; background: #f9f9f9; }
    .header { background: linear-gradient(135deg, #232f3e 0%, #1a242f 100%); color: white; padding: 30px; border-radius: 8px; margin-bottom: 20px; }
    .header h1 { margin: 0 0 10px 0; font-size: 32px; }
    .header .subtitle { opacity: 0.9; font-size: 16px; }
    .badge { display: inline-block; padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: 600; }
    .badge-blue { background: #0972d3; color: white; }
    .badge-green { background: #037f0c; color: white; }
    .badge-red { background: #d91515; color: white; }
    .badge-grey { background: #5f6b7a; color: white; }
    .container { background: white; padding: 24px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .section-separator { background: #FF9900; padding: 12px 20px; border-radius: 12px; margin: 24px 0; }
    .section-separator h2 { margin: 0; color: #000; font-size: 24px; }
    .section-separator .desc { color: #1F2937; font-size: 14px; margin-top: 4px; }
    .key-value { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; padding: 20px 0; border-top: 1px solid #eee; border-bottom: 1px solid #eee; }
    .key-value-label { font-size: 12px; color: #666; text-transform: uppercase; font-weight: 600; margin-bottom: 4px; }
    .key-value-value { font-size: 16px; color: #000; font-family: monospace; }
    .ranking-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }
    .ranking-card { border: 1px solid #eee; padding: 20px; border-radius: 8px; text-align: center; }
    .ranking-card .rank { font-size: 14px; color: #666; margin-bottom: 10px; }
    .ranking-card .engine { font-size: 20px; font-weight: 600; margin: 10px 0; }
    .ranking-card .confidence { font-size: 36px; font-weight: 700; color: #0972d3; }
    .ranking-card .confidence-label { font-size: 12px; color: #666; }
    .roadmap-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }
    .roadmap-phase { border: 1px solid #eee; padding: 20px; border-radius: 8px; }
    .roadmap-phase h3 { margin: 10px 0; font-size: 18px; }
    .roadmap-phase ul { padding-left: 20px; margin: 10px 0; }
    .roadmap-phase .timeline { font-size: 12px; color: #666; margin-top: 10px; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
    th { background: #f5f5f5; font-weight: 600; font-size: 14px; }
    td { font-size: 14px; }
    .alert { padding: 15px; border-radius: 8px; margin: 15px 0; }
    .alert-info { background: #e6f2ff; border-left: 4px solid #0972d3; }
    .alert-warning { background: #fff8e6; border-left: 4px solid #ff9900; }
    .alert-error { background: #ffe6e6; border-left: 4px solid #d91515; }
    .footer { text-align: center; padding: 20px; color: #666; font-size: 12px; margin-top: 40px; border-top: 2px solid #eee; }
    ul { padding-left: 20px; }
    li { margin: 5px 0; }
    code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-family: monospace; font-size: 13px; }
  </style>
</head>
<body>
  <div class="header">
    <span class="badge badge-blue">Database Modernization Report</span>
    <h1>${synthesis.database_name || triage.database_name || 'Database'} Analysis</h1>
    <div class="subtitle">Comprehensive workload analysis and migration recommendations for ${triage.source_database_type || 'database'} to purpose-built AWS databases</div>
  </div>

  <div class="container">
    <div class="key-value">
      <div>
        <div class="key-value-label">Job ID</div>
        <div class="key-value-value">${jobId || 'N/A'}</div>
      </div>
      <div>
        <div class="key-value-label">Source Database</div>
        <div class="key-value-value">${triage.source_database_type || 'N/A'} - ${triage.database_name || synthesis.database_name || 'N/A'}</div>
      </div>
      <div>
        <div class="key-value-label">Analysis Date</div>
        <div class="key-value-value">${synthesis.timestamp ? new Date(synthesis.timestamp).toLocaleString() : 'N/A'}</div>
      </div>
    </div>
  </div>

  <div class="section-separator">
    <h2>Executive Summary</h2>
    <div class="desc">High-level overview of workload analysis, key findings, and migration recommendations</div>
  </div>

  <div class="container">
    <p>${synthesis.summary || 'No summary available'}</p>
    ${synthesis.summary_deterministic ? html`<p style="margin-top: 15px;"><strong>Key Metrics:</strong> ${synthesis.summary_deterministic}</p>` : ''}
  </div>

  <div class="section-separator">
    <h2>Database Ranking</h2>
    <div class="desc">${t('report-results.ranking.description', { defaultValue: 'AWS database services ranked by share of query patterns; confidence is the fit of the queries routed to each engine' })}</div>
  </div>

  <div class="container">
    <div class="ranking-grid">
      ${ranking.map((item, index) => html`
        <div class="ranking-card">
          <div class="rank">Rank #${index + 1}</div>
          <div class="engine">${item.target}</div>
          <div class="confidence">${confidenceText(item, true)}</div>
          <div class="confidence-label">${hasRoutedConfidence(item)
            ? t('report-results.ranking.routed-confidence', { defaultValue: 'Fit of routed queries' })
            : 'Confidence'}</div>
          ${hasRoutedConfidence(item) && analysisConfidence(item) !== null ? html`<div class="confidence-label">${t('report-results.ranking.analysis-confidence', { score: analysisConfidence(item), defaultValue: 'Analysis average {{score}}%' })}</div>` : ''}
          <div style="margin-top: 15px; font-size: 12px; color: #666;">
            ${item.tables_analyzed || 0} tables · ${item.access_patterns || 0} patterns<br>
            $${fixed(item.monthly_cost_usd, 2, '0')}/mo
          </div>
        </div>
      `)}
    </div>
    ${cacheLayer ? html`
    <div class="ranking-card" style="margin-top: 20px; border-color: #d91515; text-align: left; display: flex; align-items: center; gap: 12px;">
      <span class="badge badge-red">${t('cache-layer.badge-label', { defaultValue: 'Cache layer' })}</span>
      <span>${cacheLayerLine}</span>
    </div>
    ` : ''}
  </div>

  <div class="section-separator">
    <h2>Table Mappings (${tableMappings.length})</h2>
    <div class="desc">Recommended mapping of source tables to target databases with design patterns and confidence scores</div>
  </div>

  <div class="container">
    <table>
      <thead>
        <tr>
          <th>Source Table</th>
          <th>Target Database</th>
          <th>Target Table</th>
          <th>Pattern</th>
          <th>Confidence</th>
        </tr>
      </thead>
      <tbody>
        ${tableMappings.slice(0, 50).map(item => html`
          <tr>
            <td><code>${item.source_table}</code></td>
            <td><span class="badge badge-blue">${item.recommended_database}</span></td>
            <td><code>${item.target_table || 'N/A'}</code></td>
            <td><span class="badge badge-grey">${item.aggregate_pattern}</span></td>
            <td>${item.confidence_score || 0}%</td>
          </tr>
        `)}
      </tbody>
    </table>
    ${tableMappings.length > 50 ? html`<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 50 of ${tableMappings.length} table mappings</p>` : ''}
  </div>

  <div class="section-separator">
    <h2>Risk Assessment — ${risks.length} risks identified (${overallRisk})</h2>
    <div class="desc">Technical, operational, and business risks identified during analysis with recommended mitigation strategies</div>
  </div>

  <div class="container">
    <div class="alert alert-${alertClass}">
      <strong>Overall Risk Level: ${overallRisk}</strong><br>
      The migration has been assessed with an overall ${overallRisk} risk level based on ${risks.length} identified risks.
    </div>

    <h3>High Severity Risks</h3>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Engine</th>
          <th>Type</th>
          <th>Description</th>
          <th>Mitigation</th>
        </tr>
      </thead>
      <tbody>
        ${risks.filter(r => r.severity === 'HIGH').slice(0, 20).map(risk => html`
          <tr>
            <td><code>${risk.risk_id || 'N/A'}</code></td>
            <td>${risk.engine ? html`<span class="badge badge-blue">${risk.engine}</span>` : '-'}</td>
            <td>${risk.risk_type || 'Technical'}</td>
            <td>${risk.description}</td>
            <td>${risk.mitigation}</td>
          </tr>
        `)}
      </tbody>
    </table>
  </div>

  <div class="section-separator">
    <h2>${t('report-results.risk-assessment.resolved-tab', { count: resolvedRisks.length })}</h2>
    <div class="desc">${t('report-results.risk-assessment.resolved-export-description')}</div>
  </div>

  <div class="container">
    ${resolvedRisks.length > 0 ? html`<table>
      <thead>
        <tr>
          <th>${t('report-results.risk-assessment.col-engine')}</th>
          <th>${t('report-results.risk-assessment.col-resolved-on')}</th>
          <th>${t('report-results.risk-assessment.col-severity')}</th>
          <th>${t('report-results.risk-assessment.col-description')}</th>
          <th>${t('report-results.risk-assessment.col-reason')}</th>
        </tr>
      </thead>
      <tbody>
        ${resolvedRisks.map(risk => html`
          <tr>
            <td>${risk.engine ? html`<span class="badge badge-blue">${risk.engine}</span>` : '-'}</td>
            <td>${risk.resolved_on ? html`<span class="badge badge-blue">${risk.resolved_on}</span>` : '-'}</td>
            <td>${risk.severity || ''}</td>
            <td>${risk.description}</td>
            <td>${risk.reason}</td>
          </tr>
        `)}
      </tbody>
    </table>` : html`<p>${t('report-results.risk-assessment.no-resolved-risks')}</p>`}
  </div>

  <div class="section-separator">
    <h2>Trade-offs (${tradeoffs.length})</h2>
    <div class="desc">Key architectural and operational trade-offs to consider when migrating to each target database</div>
  </div>

  <div class="container">
    ${tradeoffs.slice(0, 30).map(item => {
      const to = normalizeTradeoff(item);
      const sourceTables = asArray(to.source_tables);
      const targetTables = asArray(to.target_tables);
      return html`<div style="margin-bottom: 12px; padding: 10px 14px; border-left: 3px solid #0972d3; background: #f2f8fd; border-radius: 4px;">
        <div><span class="badge badge-blue">${to.engine}</span> <strong>${to.description}</strong></div>
        ${to.impact ? html`<div style="margin-top: 4px; color: #5f6b7a; font-size: 13px;">${to.impact}</div>` : ''}
        ${sourceTables.length > 0 || targetTables.length > 0 ? html`<div style="margin-top: 4px; color: #888; font-size: 12px;">${sourceTables.join(', ')}${sourceTables.length > 0 && targetTables.length > 0 ? ' → ' : ''}${targetTables.join(', ')}</div>` : ''}
      </div>`;
    })}
    ${tradeoffs.length > 30 ? html`<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 30 of ${tradeoffs.length} trade-offs</p>` : ''}
  </div>

  ${tcoAnalysis.projected_monthly_cost ? html`
  <div class="section-separator">
    <h2>Total Cost of Ownership</h2>
    <div class="desc">Comparison of current vs. projected monthly costs showing potential savings with AWS managed databases</div>
  </div>

  <div class="container">
    <div class="key-value">
      <div>
        <div class="key-value-label">Current Monthly Cost</div>
        <div style="font-size: 32px; font-weight: 700;">$${fixed(tcoAnalysis.current_monthly_cost, 2, '0.00')}</div>
      </div>
      <div>
        <div class="key-value-label">Projected Monthly Cost</div>
        <div style="font-size: 32px; font-weight: 700; color: #037f0c;">$${fixed(tcoAnalysis.projected_monthly_cost, 2, '0.00')}</div>
      </div>
      <div>
        <div class="key-value-label">Savings</div>
        <div style="font-size: 32px; font-weight: 700; color: ${tcoAnalysis.savings_percent > 0 ? '#037f0c' : '#000'};">${fixed(tcoAnalysis.savings_percent, 1, '0')}%</div>
      </div>
    </div>
  </div>
  ` : ''}

  ${migrationWaves.length > 0 ? html`
  <div class="section-separator">
    <h2>Migration Roadmap</h2>
    <div class="desc">The incremental migration waves computed from this assessment: cache, then key-value and point lookups, then search/analytics read models and document data, then whatever is retained on the source-compatible relational engine</div>
  </div>

  <div class="container">
    ${migrationWaves.map(wave => html`
      <div class="roadmap-phase" style="margin-bottom: 15px;">
        <span class="badge badge-blue">Wave ${wave.wave}</span>
        ${asArray(wave.engines).map(engine => html` <span class="badge badge-grey">${displayEngine(engine)}</span>`)}
        <h3>${wave.title}</h3>
        <div style="font-size: 13px; color: #666; margin: 8px 0;">
          ${wave.query_count || 0} queries ·
          ${wave.share_basis === 'calls'
            ? html`${fixed(wave.workload_share_percent, 1, '0')}% of calls`
            : html`${fixed(wave.workload_share_percent, 1, '0')}% of the workload`}
          ${wave.table_count > 0 ? html` · ${wave.table_count} source tables` : ''}
        </div>
        <p>${wave.rationale}</p>
        ${asArray(wave.tables).length > 0 ? html`
          <div style="font-size: 13px; color: #666;">
            <strong>Tables:</strong> ${tablesSummary(asArray(wave.tables))}
          </div>
        ` : ''}
        ${wave.gate ? html`<div class="timeline">Gate before the next wave: ${wave.gate}</div>` : ''}
      </div>
    `)}
  </div>
  ` : ''}

  ${queryGroups.length > 0 ? html`
  <div class="section-separator">
    <h2>Query Classification (${queryGroups.length} groups)</h2>
    <div class="desc">Source queries grouped by access patterns with target engine recommendations and performance metrics</div>
  </div>

  <div class="container">
    ${queryGroups.slice(0, 10).map(group => {
      const patterns = asArray(group.access_patterns);
      return html`
      <div style="margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid #eee;">
        <h3>${group.group_name} (${patterns.length} patterns)</h3>
        <div style="margin: 15px 0;">
          <strong>Target Engines:</strong>
          ${asArray(group.engines).map((engine, i) => html`${i > 0 ? ' ' : ''}<span class="badge badge-blue">${engine}</span>`)}
        </div>
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin: 15px 0;">
          <div>
            <div class="key-value-label">Total Design RPS</div>
            <div style="font-size: 20px; font-weight: 600;">${fixed(group.total_design_rps, 2, '0')}</div>
          </div>
          <div>
            <div class="key-value-label">Source Queries</div>
            <div style="font-size: 20px; font-weight: 600;">${asArray(group.source_queries).length}</div>
          </div>
        </div>
        ${patterns.length > 0 ? html`
        <table>
          <thead>
            <tr>
              <th>Pattern ID</th>
              <th>Operation</th>
              <th>Table</th>
              <th>Design RPS</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            ${patterns.slice(0, 10).map(pattern => html`
              <tr>
                <td><code>${pattern.pattern_id || 'N/A'}</code></td>
                <td><span class="badge badge-grey">${pattern.operation || 'N/A'}</span></td>
                <td><code>${pattern.table_name || 'N/A'}</code></td>
                <td>${fixed(pattern.design_rps, 2, '0')}</td>
                <td>${pattern.description || 'N/A'}</td>
              </tr>
            `)}
          </tbody>
        </table>
        ${patterns.length > 10 ? html`<p style="margin-top: 10px; color: #666; font-size: 12px;">Showing first 10 of ${patterns.length} access patterns</p>` : ''}
        ` : ''}
      </div>
    `;
    })}
    ${queryGroups.length > 10 ? html`<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 10 of ${queryGroups.length} query groups</p>` : ''}
  </div>
  ` : ''}

  <div class="footer">
    Generated on ${now.toLocaleString()} | AWS Database Modernization Analysis
  </div>
</body>
</html>`;

  return doc.toString();
};
