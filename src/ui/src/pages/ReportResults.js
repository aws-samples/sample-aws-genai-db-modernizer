import { useState, useEffect, memo, useCallback, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useCollection } from '@cloudscape-design/collection-hooks';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Badge from "@cloudscape-design/components/badge";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Table from "@cloudscape-design/components/table";
import Alert from "@cloudscape-design/components/alert";
import Tabs from "@cloudscape-design/components/tabs";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";

//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import SectionSeparator from "../components/SectionSeparator";



const ENGINE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'blue',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
};



// Helper: normalize a trade-off (structured object or legacy string) into a consistent shape
const normalizeTradeoff = (item, fallbackEngine = 'unknown') => {
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

// Inline trade-offs shown inside access pattern groups
const InlineTradeoffs = memo(({ tradeoffs }) => {
  if (!tradeoffs || tradeoffs.length === 0) return null;
  return (
    <SpaceBetween size="xs">
      {tradeoffs.map((tradeOff, idx) => (
        <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
          className="inline-tradeoff"
          style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
          <SpaceBetween size="xxs">
            <Box fontSize="body-s" fontWeight="bold">
              <Badge color={ENGINE_COLORS[tradeOff.engine] || 'blue'}>{tradeOff.engine}</Badge>
              {' '}{tradeOff.description}
            </Box>
            {tradeOff.impact && (
              <Box fontSize="body-s" color="text-body-secondary">
                {tradeOff.impact}
              </Box>
            )}
            {(tradeOff.source_tables.length > 0 || tradeOff.target_tables.length > 0) && (
              <Box fontSize="body-s" color="text-status-inactive">
                {tradeOff.source_tables.length > 0 && <span>{tradeOff.source_tables.join(', ')}</span>}
                {tradeOff.source_tables.length > 0 && tradeOff.target_tables.length > 0 && <span> → </span>}
                {tradeOff.target_tables.length > 0 && <span>{tradeOff.target_tables.join(', ')}</span>}
              </Box>
            )}
          </SpaceBetween>
        </Box>
      ))}
    </SpaceBetween>
  );
});

// Component for Schema Design tables (DynamoDB/DocumentDB)
const SchemaDesignTable = memo(({ tables }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    tables,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.schema-table.no-tables')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  return (
    <Table
      {...collectionProps} // nosemgrep: react-props-spreading
      columnDefinitions={[
        {
          id: 'target_table',
          header: t('report-results.schema-table.col-target-table'),
          cell: item => <Box fontFamily="monospace" fontWeight="bold">{item.table_name}</Box>
        },
        {
          id: 'design_pattern',
          header: t('report-results.schema-table.col-design-pattern'),
          cell: item => <Badge>{item.aggregate_pattern}</Badge>
        },
        {
          id: 'source_tables',
          header: t('report-results.schema-table.col-source-tables'),
          cell: item => (
            <Box fontSize="body-s">
              {item.source_tables?.join(', ') || 'N/A'}
            </Box>
          )
        },
        {
          id: 'gsis',
          header: t('report-results.schema-table.col-gsis'),
          cell: item => item.gsi_count || 0,
          width: 80
        },
        {
          id: 'est_items',
          header: t('report-results.schema-table.col-est-items'),
          cell: item => (item.item_count || 0).toLocaleString(),
          width: 120
        },
        {
          id: 'avg_item_size',
          header: t('report-results.schema-table.col-avg-item-size'),
          cell: item => `${item.item_size_bytes || 0} B`,
          width: 120
        }
      ]}
      items={items}
      variant="embedded"
      filter={
        <TextFilter
          {...filterProps} // nosemgrep: react-props-spreading
          filteringPlaceholder={t('report-results.schema-table.filter-placeholder')}
          filteringText={filterProps.filteringText}
          countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
        />
      }
      pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
    />
  );
});

// Component for OpenSearch indexes
const OpenSearchIndexTable = memo(({ tables }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    tables,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.opensearch-table.no-indexes')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  return (
    <Table
      {...collectionProps} // nosemgrep: react-props-spreading
      columnDefinitions={[
        {
          id: 'target_index',
          header: t('report-results.opensearch-table.col-target-index'),
          cell: item => <Box fontFamily="monospace" fontWeight="bold">{item.table_name}</Box>
        },
        {
          id: 'design_pattern',
          header: t('report-results.opensearch-table.col-design-pattern'),
          cell: item => <Badge>{item.aggregate_pattern}</Badge>
        },
        {
          id: 'source_tables',
          header: t('report-results.opensearch-table.col-source-tables'),
          cell: item => (
            <Box fontSize="body-s">
              {item.source_tables?.join(', ') || 'N/A'}
            </Box>
          )
        },
        {
          id: 'shards',
          header: t('report-results.opensearch-table.col-shards'),
          cell: item => item.shards || 0,
          width: 80
        },
        {
          id: 'replicas',
          header: t('report-results.opensearch-table.col-replicas'),
          cell: item => item.replicas || 0,
          width: 80
        },
        {
          id: 'fields',
          header: t('report-results.opensearch-table.col-fields'),
          cell: item => item.field_count || 0,
          width: 80
        }
      ]}
      items={items}
      variant="embedded"
      filter={
        <TextFilter
          {...filterProps} // nosemgrep: react-props-spreading
          filteringPlaceholder={t('report-results.opensearch-table.filter-placeholder')}
          filteringText={filterProps.filteringText}
          countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
        />
      }
      pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
    />
  );
});

// Component for Query Classification access patterns with inline trade-offs
const AccessPatternTable = memo(({ accessPatterns, tradeoffs = [] }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    accessPatterns,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.access-pattern-table.no-patterns')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Build a lookup: query_id -> list of trade-offs that reference it
  const tradeoffsByQuery = useMemo(() => {
    const map = {};
    tradeoffs.forEach(tradeOff => {
      (tradeOff.query_ids || []).forEach(qid => {
        if (!map[qid]) map[qid] = [];
        map[qid].push(tradeOff);
      });
    });
    return map;
  }, [tradeoffs]);

  return (
    <SpaceBetween size="s">
      <Table
        {...collectionProps} // nosemgrep: react-props-spreading
        columnDefinitions={[
          {
            id: 'pattern_id',
            header: t('report-results.access-pattern-table.col-pattern-id'),
            cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.pattern_id || 'N/A'}</Box>
          },
          {
            id: 'operation',
            header: t('report-results.access-pattern-table.col-operation'),
            cell: item => <Badge>{item.operation || 'N/A'}</Badge>
          },
          {
            id: 'table',
            header: t('report-results.access-pattern-table.col-table'),
            cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.table_name || 'N/A'}</Box>
          },
          {
            id: 'rps',
            header: t('report-results.access-pattern-table.col-design-rps'),
            cell: item => item.design_rps?.toFixed(2) || '0'
          },
          {
            id: 'description',
            header: t('report-results.access-pattern-table.col-description'),
            cell: item => {
              const matched = tradeoffsByQuery[item.source_query_id] || tradeoffsByQuery[item.pattern_id] || [];
              return (
                <SpaceBetween size="xs">
                  <Box fontSize="body-s">{item.description || 'N/A'}</Box>
                  {matched.length > 0 && matched.map((tradeOff, i) => (
                    <Box key={i} padding={{ left: 's' }} style={{ borderLeft: '2px solid #0972d3' }}>
                      <Box fontSize="body-s" color="text-status-info" fontWeight="bold">{tradeOff.description}</Box>
                      {tradeOff.impact && <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>}
                    </Box>
                  ))}
                </SpaceBetween>
              );
            }
          }
        ]}
        items={items}
        variant="embedded"
        wrapLines
        filter={
          <TextFilter
            {...filterProps} // nosemgrep: react-props-spreading
            filteringPlaceholder={t('report-results.access-pattern-table.filter-placeholder')}
            filteringText={filterProps.filteringText}
            countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
          />
        }
        pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
      />
    </SpaceBetween>
  );
});



const ReportResultsPage = memo(() => {

  const { t } = useTranslation();
  const { jobId } = useParams();

  //--|#######################| State Management Section  |#######################

  const [navigationOpen, setNavigationOpen] = useState(false);
  const [resultsData, setResultsData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [flashbarItems, setFlashbarItems] = useState([]);
  const [exporting, setExporting] = useState(false);



  //--|#######################| Handle Section  |#######################

  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);

  // Export to HTML with all sections
  const exportToHTML = useCallback(() => {
    setExporting(true);

    try {
      const synthesis = resultsData?.synthesis || {};
      const triage = resultsData?.triage_summary || {};
      const ranking = synthesis?.ranking || [];
      const tableMappings = synthesis?.table_mappings || [];
      const riskAssessment = synthesis?.risk_assessment || {};
      const risks = riskAssessment.risks || [];
      const tradeoffs = synthesis?.trade_offs || [];
      const tcoAnalysis = synthesis?.tco_analysis || {};
      const schemaDesigns = synthesis?.schema_designs || {};

      // nosemgrep: html-in-template-string
      let html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Database Modernization Report - ${synthesis.database_name || 'Database'}</title>${''/* nosemgrep: missing-template-string-indicator */}
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
    ${synthesis.summary_deterministic ? `<p style="margin-top: 15px;"><strong>Key Metrics:</strong> ${synthesis.summary_deterministic}</p>` /* nosemgrep: html-in-template-string */ : ''}
  </div>

  <div class="section-separator">
    <h2>Database Ranking</h2>
    <div class="desc">AWS database services ranked by confidence score based on workload analysis, access patterns, and migration complexity</div>
  </div>

  <div class="container">
    <div class="ranking-grid">
      ${ranking.map((item, index) => /* nosemgrep: html-in-template-string */ `
        <div class="ranking-card">
          <div class="rank">Rank #${index + 1}</div>
          <div class="engine">${item.target}</div>
          <div class="confidence">${item.confidence_score}%</div>
          <div class="confidence-label">Confidence</div>
          <div style="margin-top: 15px; font-size: 12px; color: #666;">
            ${item.tables_analyzed || 0} tables · ${item.access_patterns || 0} patterns<br>
            $${item.monthly_cost_usd?.toFixed(2) || '0'}/mo
          </div>
        </div>
      `).join('')}
    </div>
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
        ${tableMappings.slice(0, 50).map(item => /* nosemgrep: html-in-template-string */ `
          <tr>
            <td><code>${item.source_table}</code></td>
            <td><span class="badge badge-blue">${item.recommended_database}</span></td>
            <td><code>${item.target_table || 'N/A'}</code></td>
            <td><span class="badge badge-grey">${item.aggregate_pattern}</span></td>
            <td>${item.confidence_score || 0}%</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
    ${tableMappings.length > 50 ? `<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 50 of ${tableMappings.length} table mappings</p>` /* nosemgrep: html-in-template-string */ : ''}
  </div>

  <div class="section-separator">
    <h2>Risk Assessment — ${risks.length} risks identified (${riskAssessment.overall_risk_level || 'MEDIUM'})</h2>
    <div class="desc">Technical, operational, and business risks identified during analysis with recommended mitigation strategies</div>
  </div>

  <div class="container">
    <div class="alert alert-${riskAssessment.overall_risk_level === 'HIGH' ? 'error' : riskAssessment.overall_risk_level === 'LOW' ? 'info' : 'warning'}">
      <strong>Overall Risk Level: ${riskAssessment.overall_risk_level || 'MEDIUM'}</strong><br>
      The migration has been assessed with an overall ${riskAssessment.overall_risk_level || 'MEDIUM'} risk level based on ${risks.length} identified risks.
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
        ${risks.filter(r => r.severity === 'HIGH').slice(0, 20).map(risk => /* nosemgrep: html-in-template-string */ `
          <tr>
            <td><code>${risk.risk_id || 'N/A'}</code></td>
            <td>${risk.engine ? `<span class="badge badge-blue">${risk.engine}</span>` /* nosemgrep: html-in-template-string */ : '-'}</td>
            <td>${risk.risk_type || 'Technical'}</td>
            <td>${risk.description}</td>
            <td>${risk.mitigation}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  </div>

  <div class="section-separator">
    <h2>Trade-offs (${tradeoffs.length})</h2>
    <div class="desc">Key architectural and operational trade-offs to consider when migrating to each target database</div>
  </div>

  <div class="container">
    ${tradeoffs.slice(0, 30).map(item => {
      const t = normalizeTradeoff(item);
      // nosemgrep: html-in-template-string
      return `<div style="margin-bottom: 12px; padding: 10px 14px; border-left: 3px solid #0972d3; background: #f2f8fd; border-radius: 4px;">
        <div><span class="badge badge-blue">${t.engine}</span> <strong>${t.description}</strong></div>
        ${t.impact ? `<div style="margin-top: 4px; color: #5f6b7a; font-size: 13px;">${t.impact}</div>` /* nosemgrep: html-in-template-string */ : ''}
        ${t.source_tables.length > 0 || t.target_tables.length > 0 ? `<div style="margin-top: 4px; color: #888; font-size: 12px;">${t.source_tables.join(', ')}${t.source_tables.length > 0 && t.target_tables.length > 0 ? ' → ' : ''}${t.target_tables.join(', ')}</div>` /* nosemgrep: html-in-template-string */ : ''}
      </div>`;
    }).join('')}
    ${tradeoffs.length > 30 ? `<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 30 of ${tradeoffs.length} trade-offs</p>` /* nosemgrep: html-in-template-string */ : ''}
  </div>

  ${tcoAnalysis && tcoAnalysis.projected_monthly_cost ? /* nosemgrep: html-in-template-string */ `
  <div class="section-separator">
    <h2>Total Cost of Ownership</h2>
    <div class="desc">Comparison of current vs. projected monthly costs showing potential savings with AWS managed databases</div>
  </div>

  <div class="container">
    <div class="key-value">
      <div>
        <div class="key-value-label">Current Monthly Cost</div>
        <div style="font-size: 32px; font-weight: 700;">$${tcoAnalysis.current_monthly_cost?.toFixed(2) || '0.00'}</div>
      </div>
      <div>
        <div class="key-value-label">Projected Monthly Cost</div>
        <div style="font-size: 32px; font-weight: 700; color: #037f0c;">$${tcoAnalysis.projected_monthly_cost?.toFixed(2) || '0.00'}</div>
      </div>
      <div>
        <div class="key-value-label">Savings</div>
        <div style="font-size: 32px; font-weight: 700; color: ${tcoAnalysis.savings_percent > 0 ? '#037f0c' : '#000'};">${tcoAnalysis.savings_percent?.toFixed(1) || '0'}%</div>
      </div>
    </div>
  </div>
  ` : ''}

  <div class="section-separator">
    <h2>Migration Roadmap</h2>
    <div class="desc">Four-phase migration strategy from quick wins to full production deployment with timeline estimates</div>
  </div>

  <div class="container">
    <div class="roadmap-grid">
      <div class="roadmap-phase">
        <span class="badge badge-green">Phase 1</span>
        <h3>Quick Wins</h3>
        <ul>
          <li>Deploy caching layer</li>
          <li>Optimize indexes</li>
          <li>Add monitoring</li>
          <li>Connection pooling</li>
        </ul>
        <div class="timeline">Weeks 1-4</div>
      </div>

      <div class="roadmap-phase">
        <span class="badge badge-blue">Phase 2</span>
        <h3>POCs</h3>
        <ul>
          <li>Build prototypes</li>
          <li>Load testing</li>
          <li>Validate patterns</li>
          <li>CDC pipeline</li>
        </ul>
        <div class="timeline">Weeks 5-8</div>
      </div>

      <div class="roadmap-phase">
        <span class="badge badge-grey">Phase 3</span>
        <h3>Migration</h3>
        <ul>
          <li>Dual-write setup</li>
          <li>Data backfill</li>
          <li>Gradual cutover</li>
          <li>Monitoring</li>
        </ul>
        <div class="timeline">Weeks 9-16</div>
      </div>

      <div class="roadmap-phase">
        <span class="badge badge-green">Phase 4</span>
        <h3>Validation</h3>
        <ul>
          <li>Load testing</li>
          <li>Validate SLAs</li>
          <li>Documentation</li>
          <li>Decommission</li>
        </ul>
        <div class="timeline">Weeks 17-20</div>
      </div>
    </div>
  </div>

  ${synthesis.query_groups && synthesis.query_groups.length > 0 ? /* nosemgrep: html-in-template-string */ `
  <div class="section-separator">
    <h2>Query Classification (${synthesis.query_groups.length} groups)</h2>
    <div class="desc">Source queries grouped by access patterns with target engine recommendations and performance metrics</div>
  </div>

  <div class="container">
    ${synthesis.query_groups.slice(0, 10).map((group, index) => /* nosemgrep: html-in-template-string */ `
      <div style="margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid #eee;">
        <h3>${group.group_name} (${group.access_patterns?.length || 0} patterns)</h3>
        <div style="margin: 15px 0;">
          <strong>Target Engines:</strong>
          ${(group.engines || []).map(engine => /* nosemgrep: html-in-template-string */ `<span class="badge badge-blue">${engine}</span>`).join(' ')}
        </div>
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin: 15px 0;">
          <div>
            <div class="key-value-label">Total Design RPS</div>
            <div style="font-size: 20px; font-weight: 600;">${group.total_design_rps?.toFixed(2) || '0'}</div>
          </div>
          <div>
            <div class="key-value-label">Source Queries</div>
            <div style="font-size: 20px; font-weight: 600;">${group.source_queries?.length || 0}</div>
          </div>
        </div>
        ${group.access_patterns && group.access_patterns.length > 0 ? /* nosemgrep: html-in-template-string */ `
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
            ${group.access_patterns.slice(0, 10).map(pattern => /* nosemgrep: html-in-template-string */ `
              <tr>
                <td><code>${pattern.pattern_id || 'N/A'}</code></td>
                <td><span class="badge badge-grey">${pattern.operation || 'N/A'}</span></td>
                <td><code>${pattern.table_name || 'N/A'}</code></td>
                <td>${pattern.design_rps?.toFixed(2) || '0'}</td>
                <td>${pattern.description || 'N/A'}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
        ${group.access_patterns.length > 10 ? `<p style="margin-top: 10px; color: #666; font-size: 12px;">Showing first 10 of ${group.access_patterns.length} access patterns</p>` /* nosemgrep: html-in-template-string */ : ''}
        ` : ''}
      </div>
    `).join('')}
    ${synthesis.query_groups.length > 10 ? `<p style="margin-top: 15px; color: #666; font-size: 14px;">Showing first 10 of ${synthesis.query_groups.length} query groups</p>` /* nosemgrep: html-in-template-string */ : ''}
  </div>
  ` : ''}

  <div class="footer">
    Generated on ${new Date().toLocaleString()} | AWS Database Modernization Analysis
  </div>
</body>
</html>`;

      const dataBlob = new Blob([html], { type: 'text/html' });
      const url = URL.createObjectURL(dataBlob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `database-modernization-report-${jobId}.html`;
      link.click();
      URL.revokeObjectURL(url);

      addFlashbarMessage({
        type: 'success',
        header: t('report-results.export.success-header'),
        content: t('report-results.export.success-content'),
        dismissible: true,
        id: `success-${Date.now()}`
      });
    } catch (error) {
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.export.error-header'),
        content: t('report-results.export.error-content', { message: error.message }),
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setExporting(false);
    }
  }, [resultsData, jobId, addFlashbarMessage, t]);



  //--|#######################| Gather Information Section  |#######################

  const gatherResults = useCallback(async () => {
    if (!jobId) {
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.error.invalid-job-id'),
        content: t('report-results.error.no-job-id'),
        dismissible: true,
        id: `error-${Date.now()}`
      });
      setLoading(false);
      return;
    }

    setLoading(true);

    try {
      const apiManager = new ApiManager();

      const apiCalls = [{
        id: 'get-results',
        path: `assessments/${jobId}/results`,
        method: 'GET',
        params: {}
      }];

      const results = await apiManager.execute(apiCalls);

      if (results['get-results']?.error) {
        const result = results['get-results'];
        const errorMessage = result.error?.message || 'Failed to load results';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/results`;

        addFlashbarMessage({
          type: 'error',
          header: t('report-results.error.api-error-header', { statusCode }),
          content: t('report-results.error.api-error-content', { apiUrl, errorMessage }),
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['get-results']?.success) {
        setResultsData(results['get-results']);
        setFlashbarItems([]);
      }

    } catch (error) {
      console.error('Error loading results:', error);
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.error.unexpected-header'),
        content: t('report-results.error.unexpected-content', { message: error.message }),
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlashbarMessage, t]);



  //--|#######################| Initialization Section  |#######################

  useEffect(() => {
    gatherResults();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);



  //--|#######################| Data Processing Section  |#######################

  const synthesis = resultsData?.synthesis || {};
  const triage = resultsData?.triage_summary || {};
  const ranking = synthesis?.ranking || [];
  const queryGroups = synthesis?.query_groups || [];
  const tableMappings = synthesis?.table_mappings || [];
  const riskAssessment = synthesis?.risk_assessment || {};
  const tradeoffs = synthesis?.trade_offs || [];
  const tcoAnalysis = synthesis?.tco_analysis || {};
  const schemaDesigns = synthesis?.schema_designs || {};

  // Process risks from API - extract engine from description if available
  const processRisks = (apiRisks) => {
    if (!apiRisks || apiRisks.length === 0) return [];

    return apiRisks.map(risk => {
      // Extract engine from description (format: [engine] type: description)
      const engineMatch = risk.description?.match(/^\[(\w+)\]/);
      const engine = engineMatch ? engineMatch[1] : null;

      // Remove engine prefix from description if present
      const cleanDescription = risk.description?.replace(/^\[\w+\]\s+\w+:\s+/, '') || '';

      return {
        id: risk.risk_id,
        engine: engine,
        severity: risk.severity,
        risk_type: risk.risk_type,
        description: cleanDescription,
        mitigation: risk.mitigation?.replace(/^Implement as application logic:\s+/, '') || '',
        affected_tables: risk.affected_tables || []
      };
    });
  };

  const risks = processRisks(riskAssessment.risks);
  const totalRisks = risks.length;
  const highSeverityRisks = risks.filter(r => r.severity === 'HIGH');
  const mediumSeverityRisks = risks.filter(r => r.severity === 'MEDIUM');
  const lowSeverityRisks = risks.filter(r => r.severity === 'LOW');



  //--|#######################| Breadcrumb Section  |#######################

  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('report-results.breadcrumb.home') },
    { href: "/dashboard", text: t('report-results.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: jobId || t('report-results.breadcrumb.job') },
    { href: `/analysis/report/${jobId}`, text: t('report-results.breadcrumb.report') }
  ], [jobId, t]);



  //--|#######################| Utility Functions  |#######################

  const scrollToSection = useCallback((sectionId) => {
    const element = document.getElementById(sectionId);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, []);

  const formatPatternName = useCallback((name) => {
    if (!name) return 'N/A';
    return name
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
      .join(' ');
  }, []);

  // Normalize trade-offs (handles both structured objects and legacy strings)
  const processedTradeoffs = useMemo(() => {
    return tradeoffs.map(item => normalizeTradeoff(item));
  }, [tradeoffs]);

  const uniqueEngines = useMemo(() => {
    const engines = [...new Set(processedTradeoffs.map(item => item.engine))];
    return engines.sort();
  }, [processedTradeoffs]);

  // Group trade-offs by engine
  const tradeoffsByEngine = useMemo(() => {
    const grouped = {};
    uniqueEngines.forEach(engine => {
      grouped[engine] = processedTradeoffs.filter(item => item.engine === engine);
    });
    return grouped;
  }, [processedTradeoffs, uniqueEngines]);

  // Table Mappings collection with pagination and filtering
  const { items: tableMappingItems, collectionProps: tableMappingCollectionProps, paginationProps: tableMappingPaginationProps, filterProps: tableMappingFilterProps } = useCollection(
    tableMappings,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: (
          <Box textAlign="center" color="inherit">
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {t('report-results.table-mappings.no-mappings')}
            </Box>
          </Box>
        ),
        noMatch: (
          <Box textAlign="center" color="inherit">
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {t('common.labels.no-matches')}
            </Box>
          </Box>
        )
      }
    }
  );

  // High Severity Risks collection
  const { items: highRiskItems, collectionProps: highRiskCollectionProps, paginationProps: highRiskPaginationProps, filterProps: highRiskFilterProps } = useCollection(
    highSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-high-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Medium Severity Risks collection
  const { items: mediumRiskItems, collectionProps: mediumRiskCollectionProps, paginationProps: mediumRiskPaginationProps, filterProps: mediumRiskFilterProps } = useCollection(
    mediumSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-medium-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Low Severity Risks collection
  const { items: lowRiskItems, collectionProps: lowRiskCollectionProps, paginationProps: lowRiskPaginationProps, filterProps: lowRiskFilterProps } = useCollection(
    lowSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-low-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );



  //--|#######################| Render Section  |#######################

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
        navigation={
          <SideNavigation
            activeHref={`/analysis/report/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          <SpaceBetween size="l">

            {flashbarItems.length > 0 && (
              <Flashbar
                items={flashbarItems.map(item => ({
                  ...item,
                  onDismiss: () => handleFlashbarDismiss(item.id)
                }))}
              />
            )}

            {loading ? (
              <Container>
                <Box textAlign="center" padding="xxl">
                  <Box variant="p" color="text-body-secondary">{t('report-results.states.loading')}</Box>
                </Box>
              </Container>
            ) : (
              <SpaceBetween size="l">

                {/* Hero Header */}
                <Container>
                  <SpaceBetween size="m">
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <Box>
                        <Box fontSize="display-l" fontWeight="bold">
                          {t('report-results.hero.title')}
                        </Box>
                        <Box variant="p" color="text-body-secondary">
                          {t('report-results.hero.subtitle', { sourceType: triage.source_database_type || t('report-results.hero.subtitle-database-fallback') })}
                        </Box>
                      </Box>
                      <Button
                        variant="primary"
                        iconName="download"
                        onClick={exportToHTML}
                        loading={exporting}
                      >
                        {t('report-results.hero.export-report')}
                      </Button>
                    </div>

                    <ColumnLayout columns={3} variant="text-grid" borders="vertical">
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.job-id')}</Box>
                        <Box fontFamily="monospace">{jobId || 'N/A'}</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.source-database')}</Box>
                        <Box>{triage.database_name || synthesis.database_name || 'N/A'}</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.analysis-date')}</Box>
                        <Box>{synthesis.timestamp ? new Date(synthesis.timestamp).toLocaleString() : 'N/A'}</Box>
                      </div>
                    </ColumnLayout>
                  </SpaceBetween>
                </Container>

                {/* Table of Contents */}
                <Container
                  id="report-contents"
                  header={
                    <SectionSeparator
                      title={t('report-results.toc.title')}
                      description={t('report-results.toc.description')}
                      onTopClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
                    />
                  }
                >
                  <ColumnLayout columns={2} variant="text-grid">
                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('executive-summary')}>
                        {t('report-results.toc.executive-summary')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.executive-summary-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('database-ranking')}>
                        {t('report-results.toc.database-ranking')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.database-ranking-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('table-mappings')}>
                        {t('report-results.toc.table-mappings')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.table-mappings-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('target-database-details')}>
                        {t('report-results.toc.target-database-details')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.target-database-details-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('schema-designs')}>
                        {t('report-results.toc.schema-designs')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.schema-designs-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('risk-assessment')}>
                        {t('report-results.toc.risk-assessment')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.risk-assessment-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('tradeoffs')}>
                        {t('report-results.toc.tradeoffs')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.tradeoffs-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('tco-analysis')}>
                        {t('report-results.toc.tco-analysis')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.tco-analysis-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('migration-roadmap')}>
                        {t('report-results.toc.migration-roadmap')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.migration-roadmap-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('query-classification')}>
                        {t('report-results.toc.query-classification')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.query-classification-desc')}
                      </Box>
                    </Box>
                  </ColumnLayout>
                </Container>

                {/* Executive Summary */}
                <Container
                  id="executive-summary"
                  header={
                    <SectionSeparator
                      title={t('report-results.executive-summary.title')}
                      description={t('report-results.executive-summary.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="m">
                    <Box>{synthesis.summary || t('report-results.executive-summary.no-summary')}</Box>

                    {synthesis.summary_deterministic && (
                      <Box>
                        <Box variant="awsui-key-label">{t('report-results.executive-summary.key-metrics')}</Box>
                        <Box fontSize="body-s" color="text-body-secondary">
                          {synthesis.summary_deterministic}
                        </Box>
                      </Box>
                    )}

                    {triage.source_database_type && (
                      <ColumnLayout columns={3} variant="text-grid">
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.source-engine')}</Box>
                          <Box>{triage.source_database_type}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.database-name')}</Box>
                          <Box>{triage.database_name || 'N/A'}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.analysis-date')}</Box>
                          <Box>{new Date().toLocaleDateString()}</Box>
                        </Box>
                      </ColumnLayout>
                    )}
                  </SpaceBetween>
                </Container>

                {/* Database Ranking */}
                <Container
                  id="database-ranking"
                  header={
                    <SectionSeparator
                      title={t('report-results.ranking.title')}
                      description={t('report-results.ranking.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <ColumnLayout columns={3} variant="default" borders="vertical">
                    {ranking.map((item, index) => (
                      <Box key={index} padding="l">
                        <SpaceBetween size="m" alignItems="center">
                          <Box textAlign="center">
                            <Box variant="awsui-key-label">{t('report-results.ranking.rank', { rank: index + 1 })}</Box>
                          </Box>

                          <Box textAlign="center">
                            <Badge color={ENGINE_COLORS[item.target] || 'grey'}>{item.target}</Badge>
                          </Box>

                          <Box textAlign="center">
                            <Box fontSize="display-l" fontWeight="bold">{item.confidence_score}%</Box>
                            <Box variant="small" color="text-body-secondary">{t('report-results.ranking.confidence')}</Box>
                          </Box>

                          <Box textAlign="center">
                            <Box variant="awsui-key-label">{t('report-results.ranking.weight')}</Box>
                            <Box variant="small" color="text-body-secondary">{(item.weight * 100).toFixed(0)}%</Box>
                          </Box>

                          <Box textAlign="center">
                            {item.migration_complexity_avg === 'LOW' ? (
                              <Box color="text-status-success">
                                <Box fontSize="body-s">{t('report-results.ranking.schema-ready-check')}</Box>
                              </Box>
                            ) : (
                              <Box color="text-status-info">
                                <Box fontSize="body-s">{t('report-results.ranking.schema-ready')}</Box>
                              </Box>
                            )}
                          </Box>

                          <Box textAlign="center">
                            <Box variant="small" color="text-body-secondary">
                              {item.tables_analyzed || 0} tables · {item.access_patterns || 0} patterns · ${item.monthly_cost_usd?.toFixed(2) || '0'}/mo
                            </Box>
                          </Box>
                        </SpaceBetween>
                      </Box>
                    ))}
                  </ColumnLayout>
                </Container>

                {/* Table Mappings */}
                <Container
                  id="table-mappings"
                  header={
                    <SectionSeparator
                      title={t('report-results.table-mappings.title', { count: tableMappings.length })}
                      description={t('report-results.table-mappings.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Table
                    {...tableMappingCollectionProps} // nosemgrep: react-props-spreading
                    columnDefinitions={[
                      {
                        id: 'source',
                        header: t('report-results.table-mappings.col-source-table'),
                        cell: item => <Box fontFamily="monospace">{item.source_table}</Box>
                      },
                      {
                        id: 'target_db',
                        header: t('report-results.table-mappings.col-target-database'),
                        cell: item => <Badge color={ENGINE_COLORS[item.recommended_database] || 'grey'}>{item.recommended_database}</Badge>
                      },
                      {
                        id: 'target_table',
                        header: t('report-results.table-mappings.col-target-table'),
                        cell: item => <Box fontFamily="monospace">{item.target_table || 'N/A'}</Box>
                      },
                      {
                        id: 'pattern',
                        header: t('report-results.table-mappings.col-pattern'),
                        cell: item => <Badge color="blue">{formatPatternName(item.aggregate_pattern)}</Badge>
                      },
                      {
                        id: 'confidence',
                        header: t('report-results.table-mappings.col-confidence'),
                        cell: item => (
                          <ProgressBar
                            value={item.confidence_score || 0}
                            variant="standalone"
                            status={item.confidence_score >= 70 ? 'success' : 'in-progress'}
                          />
                        )
                      }
                    ]}
                    items={tableMappingItems}
                    filter={
                      <TextFilter
                        {...tableMappingFilterProps} // nosemgrep: react-props-spreading
                        filteringPlaceholder={t('report-results.table-mappings.filter-placeholder')}
                        filteringText={tableMappingFilterProps.filteringText}
                        countText={`${tableMappingItems.length} ${tableMappingItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                      />
                    }
                    pagination={<Pagination {...tableMappingPaginationProps} />} // nosemgrep: react-props-spreading
                  />
                </Container>

                {/* Target Database Details */}
                <Container
                  id="target-database-details"
                  header={
                    <SectionSeparator
                      title={t('report-results.target-db-mapping.title')}
                      description={t('report-results.target-db-mapping.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Tabs
                    tabs={ranking.map((item, index) => ({
                      id: item.target,
                      label: item.target,
                      content: (
                        <SpaceBetween size="m">
                          <Alert type="info">
                            {t('report-results.target-db-mapping.confidence-alert', { score: item.confidence_score })}
                          </Alert>

                          <ColumnLayout columns={2} variant="text-grid">
                            <Box>
                              <Box variant="h4">{t('report-results.target-db-mapping.use-cases')}</Box>
                              <Box padding={{ top: 's' }}>
                                {item.target === 'dynamodb' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'documentdb' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'elasticache' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'aurora' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-4')}</li>
                                  </ul>
                                )}
                              </Box>
                            </Box>

                            <Box>
                              <Box variant="h4">{t('report-results.target-db-mapping.migration-considerations')}</Box>
                              <Box padding={{ top: 's' }}>
                                <Box variant="awsui-key-label">{t('report-results.target-db-mapping.complexity')}</Box>
                                <Badge color={item.migration_complexity_avg === 'LOW' ? 'green' : item.migration_complexity_avg === 'MEDIUM' ? 'blue' : 'red'}>
                                  {item.migration_complexity_avg || 'UNKNOWN'}
                                </Badge>
                                <Box padding={{ top: 's' }}>
                                  <Box variant="awsui-key-label">{t('report-results.target-db-mapping.estimated-monthly-cost')}</Box>
                                  <Box fontSize="heading-l">${item.monthly_cost_usd?.toFixed(2) || '0.00'}</Box>
                                </Box>
                              </Box>
                            </Box>
                          </ColumnLayout>
                        </SpaceBetween>
                      )
                    }))}
                  />
                </Container>

                {/* Schema Designs */}
                <Container
                  id="schema-designs"
                  header={
                    <SectionSeparator
                      title={t('report-results.schema-designs.title')}
                      description={t('report-results.schema-designs.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Tabs
                    tabs={Object.keys(schemaDesigns)
                      .filter(engine => schemaDesigns[engine]?.status === 'completed')
                      .map(engine => {
                        const design = schemaDesigns[engine];
                        return {
                          id: engine,
                          label: engine,
                          content: (
                            <SpaceBetween size="m">
                              {/* Validation Status */}
                              <Box>
                                {design.validation_passed ? (
                                  <Badge color="green">{t('report-results.schema-designs.validated')}</Badge>
                                ) : (
                                  <Badge color="red">{t('report-results.schema-designs.not-available')}</Badge>
                                )}
                              </Box>

                              {/* Summary Stats */}
                              <ColumnLayout columns={4} variant="text-grid">
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.source-tables')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.reduce((sum, tbl) => sum + (tbl.source_tables?.length || 0), 0) || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.target-tables')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.length || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.total-gsis')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.reduce((sum, tbl) => sum + (tbl.gsi_count || 0), 0) || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.access-patterns')}</Box>
                                  <Box fontSize="heading-l">{design.access_pattern_count || 0}</Box>
                                </Box>
                              </ColumnLayout>

                              {/* Tables */}
                              {design.tables && design.tables.length > 0 && engine !== 'opensearch' && (
                                <SchemaDesignTable tables={design.tables} />
                              )}

                              {/* For OpenSearch - show indexes with shards/replicas */}
                              {engine === 'opensearch' && design.tables && design.tables.length > 0 && (
                                <OpenSearchIndexTable tables={design.tables} />
                              )}

                              {/* Unsupported Patterns */}
                              {design.unsupported_patterns && design.unsupported_patterns.length > 0 && (
                                <Alert type="warning" header={t('report-results.schema-designs.unsupported-patterns', { count: design.unsupported_patterns.length })}>
                                  <SpaceBetween size="s">
                                    {design.unsupported_patterns.map((pattern, idx) => (
                                      <Box key={idx} fontSize="body-s">
                                        <Box fontWeight="bold">{pattern.pattern_type || 'Unknown'}</Box>
                                        <Box>{pattern.recommendation}</Box>
                                      </Box>
                                    ))}
                                  </SpaceBetween>
                                </Alert>
                              )}
                            </SpaceBetween>
                          )
                        };
                      })}
                  />
                </Container>

                {/* Risk Assessment */}
                <Container
                  id="risk-assessment"
                  header={
                    <SectionSeparator
                      title={t('report-results.risk-assessment.title', { count: totalRisks, level: riskAssessment.overall_risk_level || 'MEDIUM' })}
                      description={t('report-results.risk-assessment.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="l">
                    {/* Overall Risk Summary */}
                    <Alert
                      type={riskAssessment.overall_risk_level === 'HIGH' ? 'error' : riskAssessment.overall_risk_level === 'LOW' ? 'success' : 'warning'}
                      header={t('report-results.risk-assessment.overall-risk-header', { level: riskAssessment.overall_risk_level || 'MEDIUM' })}
                    >
                      {t('report-results.risk-assessment.overall-risk-body', { level: riskAssessment.overall_risk_level || 'MEDIUM', count: totalRisks })}
                    </Alert>

                    {/* Detailed Risks - Tabs by Severity */}
                    <Tabs
                      tabs={[
                        {
                          id: 'high-severity',
                          label: t('report-results.risk-assessment.high-severity-tab', { count: highSeverityRisks.length }),
                          content: (
                            <Table
                              {...highRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="red">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.description}
                                      {item.affected_tables && item.affected_tables.length > 0 && (
                                        <Box padding={{ top: 'xs' }}>
                                          <Box variant="awsui-key-label">{t('report-results.risk-assessment.affected-tables')}</Box>
                                          <SpaceBetween direction="horizontal" size="xs">
                                            {item.affected_tables.map((table, idx) => (
                                              <Badge key={idx} color="grey">{table}</Badge>
                                            ))}
                                          </SpaceBetween>
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={highRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...highRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-high')}
                                  filteringText={highRiskFilterProps.filteringText}
                                  countText={`${highRiskItems.length} ${highRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...highRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        },
                        {
                          id: 'medium-severity',
                          label: t('report-results.risk-assessment.medium-severity-tab', { count: mediumSeverityRisks.length }),
                          content: (
                            <Table
                              {...mediumRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="blue">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.title && <Box fontWeight="bold" padding={{ bottom: 'xs' }}>{item.title}</Box>}
                                      {item.description}
                                      {item.impact && (
                                        <Box padding={{ top: 'xs' }} color="text-status-error" fontSize="body-s">
                                          {t('report-results.risk-assessment.impact-label')}{item.impact}
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={mediumRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...mediumRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-medium')}
                                  filteringText={mediumRiskFilterProps.filteringText}
                                  countText={`${mediumRiskItems.length} ${mediumRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...mediumRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        },
                        {
                          id: 'low-severity',
                          label: t('report-results.risk-assessment.low-severity-tab', { count: lowSeverityRisks.length }),
                          content: (
                            <Table
                              {...lowRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="grey">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.title && <Box fontWeight="bold" padding={{ bottom: 'xs' }}>{item.title}</Box>}
                                      {item.description}
                                      {item.impact && (
                                        <Box padding={{ top: 'xs' }} color="text-status-error" fontSize="body-s">
                                          {t('report-results.risk-assessment.impact-label')}{item.impact}
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={lowRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...lowRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-low')}
                                  filteringText={lowRiskFilterProps.filteringText}
                                  countText={`${lowRiskItems.length} ${lowRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...lowRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        }
                      ]}
                    />
                  </SpaceBetween>
                </Container>

                {/* Trade-offs and Design Decisions */}
                {processedTradeoffs.length > 0 && (
                <Container
                  id="tradeoffs"
                  header={
                    <SectionSeparator
                      title={t('report-results.tradeoffs.title', { count: processedTradeoffs.length })}
                      description={t('report-results.tradeoffs.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="l">
                    {uniqueEngines.map(engine => {
                      const engineTradeoffs = tradeoffsByEngine[engine] || [];
                      // Separate PE notes from regular trade-offs
                      const peNotes = engineTradeoffs.filter(item => item.description.startsWith('[PE note]'));
                      const designDecisions = engineTradeoffs.filter(item => !item.description.startsWith('[PE note]'));
                      return (
                        <SpaceBetween key={engine} size="s">
                          <Box>
                            <Badge color={ENGINE_COLORS[engine] || 'blue'}>{engine}</Badge>
                            <Box variant="small" display="inline" padding={{ left: 'xs' }} color="text-body-secondary">
                              {t('report-results.tradeoffs.count', { count: engineTradeoffs.length })}
                            </Box>
                          </Box>

                          {/* Design decisions */}
                          {designDecisions.map((tradeOff, idx) => (
                            <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                              style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
                              <SpaceBetween size="xxs">
                                <Box fontSize="body-s" fontWeight="bold">{tradeOff.description}</Box>
                                {tradeOff.impact && (
                                  <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>
                                )}
                                {(tradeOff.source_tables.length > 0 || tradeOff.target_tables.length > 0) && (
                                  <Box fontSize="body-s" color="text-status-inactive">
                                    {tradeOff.source_tables.length > 0 && <span>{tradeOff.source_tables.join(', ')}</span>}
                                    {tradeOff.source_tables.length > 0 && tradeOff.target_tables.length > 0 && <span> → </span>}
                                    {tradeOff.target_tables.length > 0 && <span>{tradeOff.target_tables.join(', ')}</span>}
                                  </Box>
                                )}
                                {tradeOff.query_ids.length > 0 && (
                                  <Box fontSize="body-s" color="text-status-inactive">
                                    {t('report-results.tradeoffs.queries-label')}{tradeOff.query_ids.join(', ')}
                                  </Box>
                                )}
                              </SpaceBetween>
                            </Box>
                          ))}

                          {/* PE notes in a collapsible section */}
                          {peNotes.length > 0 && (
                            <ExpandableSection
                              headerText={t('report-results.tradeoffs.pe-notes-header', { count: peNotes.length })}
                              variant="footer"
                              defaultExpanded={false}
                            >
                              <SpaceBetween size="xs">
                                {peNotes.map((tradeOff, idx) => (
                                  <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                                    style={{ borderLeft: '3px solid #ff9900', backgroundColor: '#fff8e6', borderRadius: '4px' }}>
                                    <SpaceBetween size="xxs">
                                      <Box fontSize="body-s">{tradeOff.description.replace(/^\[PE note\]\s*/, '')}</Box>
                                      {tradeOff.impact && (
                                        <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>
                                      )}
                                    </SpaceBetween>
                                  </Box>
                                ))}
                              </SpaceBetween>
                            </ExpandableSection>
                          )}
                        </SpaceBetween>
                      );
                    })}
                  </SpaceBetween>
                </Container>
                )}

                {/* TCO Analysis */}
                {tcoAnalysis && (
                    <Container
                      id="tco-analysis"
                      header={
                        <SectionSeparator
                          title={t('report-results.tco.title')}
                          description={t('report-results.tco.description')}
                          onTopClick={() => scrollToSection('report-contents')}
                        />
                      }
                    >
                      <ColumnLayout columns={3} variant="text-grid">
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.current-monthly-cost')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold">
                            ${tcoAnalysis.current_monthly_cost?.toFixed(2) || '0.00'}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.projected-monthly-cost')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold" color="text-status-success">
                            ${tcoAnalysis.projected_monthly_cost?.toFixed(2) || '0.00'}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.savings')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold" color={tcoAnalysis.savings_percent > 0 ? 'text-status-success' : 'inherit'}>
                            {tcoAnalysis.savings_percent?.toFixed(1) || '0'}%
                          </Box>
                        </Box>
                      </ColumnLayout>
                    </Container>
                )}

                {/* Migration Roadmap */}
                <Container
                  id="migration-roadmap"
                  header={
                    <SectionSeparator
                      title={t('report-results.roadmap.title')}
                      description={t('report-results.roadmap.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <ColumnLayout columns={4} variant="text-grid">
                    <Box>
                      <Badge color="green">{t('report-results.roadmap.phase-1-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-1-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-1-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-success">{t('report-results.roadmap.phase-1-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge color="blue">{t('report-results.roadmap.phase-2-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-2-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-2-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-info">{t('report-results.roadmap.phase-2-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge>{t('report-results.roadmap.phase-3-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-3-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-3-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small">{t('report-results.roadmap.phase-3-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge color="green">{t('report-results.roadmap.phase-4-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-4-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-4-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-success">{t('report-results.roadmap.phase-4-timeline')}</Box>
                    </Box>
                  </ColumnLayout>
                </Container>

                {/* Query Classification */}
                {queryGroups.length > 0 && (
                    <Container
                      id="query-classification"
                      header={
                        <SectionSeparator
                          title={t('report-results.query-classification.title', { count: queryGroups.length })}
                          description={t('report-results.query-classification.description')}
                          onTopClick={() => scrollToSection('report-contents')}
                        />
                      }
                    >
                    <Tabs
                      tabs={queryGroups.map((group, index) => {
                        // Collect query IDs in this group to match trade-offs
                        const groupQueryIds = new Set([
                          ...(group.source_queries || []),
                          ...(group.access_patterns || []).map(ap => ap.source_query_id).filter(Boolean),
                          ...(group.access_patterns || []).map(ap => ap.pattern_id).filter(Boolean),
                        ]);
                        // Trade-offs that reference at least one query in this group
                        const groupTradeoffs = processedTradeoffs.filter(t =>
                          t.query_ids.length > 0 && t.query_ids.some(qid => groupQueryIds.has(qid))
                        );
                        // Trade-offs for this group's engines that have no query_ids (general trade-offs)
                        const groupEngines = new Set(group.engines || []);
                        const generalTradeoffs = processedTradeoffs.filter(t =>
                          t.query_ids.length === 0 && groupEngines.has(t.engine)
                        );

                        return {
                        id: `group-${index}`,
                        label: `${group.group_name} (${group.access_patterns?.length || 0})`,
                        content: (
                          <SpaceBetween size="m">
                            <ColumnLayout columns={3} variant="text-grid">
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.target-engines')}</Box>
                                <SpaceBetween direction="horizontal" size="xs">
                                  {(group.engines || []).map((engine, idx) => (
                                    <Badge key={idx} color={ENGINE_COLORS[engine] || 'grey'}>{engine}</Badge>
                                  ))}
                                </SpaceBetween>
                              </Box>
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.total-design-rps')}</Box>
                                <Box fontSize="heading-m">{group.total_design_rps?.toFixed(2) || '0'}</Box>
                              </Box>
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.source-queries')}</Box>
                                <Box fontSize="heading-m">{group.source_queries?.length || 0}</Box>
                              </Box>
                            </ColumnLayout>

                            {group.access_patterns && group.access_patterns.length > 0 && (
                              <AccessPatternTable
                                accessPatterns={group.access_patterns}
                                tradeoffs={groupTradeoffs}
                              />
                            )}

                            {generalTradeoffs.length > 0 && (
                              <ExpandableSection
                                headerText={t('report-results.query-classification.general-tradeoffs', { count: generalTradeoffs.length })}
                                variant="footer"
                                defaultExpanded={false}
                              >
                                <InlineTradeoffs tradeoffs={generalTradeoffs} />
                              </ExpandableSection>
                            )}
                          </SpaceBetween>
                        )
                      };
                      })}
                    />
                  </Container>
                )}

              </SpaceBetween>
            )}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />
    </>
  );
});

export default ReportResultsPage;
