import { useState, useMemo, memo } from 'react';
import { useTranslation } from 'react-i18next';

import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Table from "@cloudscape-design/components/table";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Tabs from "@cloudscape-design/components/tabs";
import Button from "@cloudscape-design/components/button";

import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import useLocalAnalysis from "../hooks/useLocalAnalysis";

const ENGINE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  opensearch: 'red',
  elasticache: 'grey',
};

const ENGINE_LABELS = {
  dynamodb: 'DynamoDB',
  documentdb: 'DocumentDB',
  opensearch: 'OpenSearch',
  elasticache: 'ElastiCache',
};




function EngineSummaryCards({ engineStats }) {
  const { t } = useTranslation();
  const engineNames = Object.keys(engineStats);
  if (engineNames.length === 0) return null;

  return (
    <ColumnLayout columns={engineNames.length} variant="default" borders="vertical">
      {engineNames.map(engine => {
        const s = engineStats[engine];
        return (
          <Box key={engine} padding="l">
            <SpaceBetween size="s">
              <Box textAlign="center">
                <Badge color={ENGINE_COLORS[engine] || 'grey'}>{ENGINE_LABELS[engine] || engine}</Badge>
              </Box>
              <ColumnLayout columns={2} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.avg-confidence")}</Box>
                  <Box fontSize="heading-l" fontWeight="bold">{s.avgConfidence}%</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.monthly-cost")}</Box>
                  <Box fontSize="heading-l" fontWeight="bold">${s.monthlyCost.toFixed(2)}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.tables-analyzed")}</Box>
                  <Box>{s.tableCount}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.queries-covered")}</Box>
                  <Box>{s.queriesCovered}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.patterns")}</Box>
                  <Box>{s.patternsDetected}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">{t("engine-analysis.summary.anti-patterns")}</Box>
                  <Box>{s.antiPatterns > 0 ? (
                    <StatusIndicator type="warning">{s.antiPatterns}</StatusIndicator>
                  ) : (
                    <StatusIndicator type="success">0</StatusIndicator>
                  )}</Box>
                </div>
              </ColumnLayout>
            </SpaceBetween>
          </Box>
        );
      })}
    </ColumnLayout>
  );
}


function TableAnalysisTab({ engines }) {
  const { t } = useTranslation();
  const engineNames = Object.keys(engines);

  // Group by table_id — one row per source table, with per-engine scores
  const tableRows = useMemo(() => {
    const tableMap = {};
    for (const [engineName, analysis] of Object.entries(engines)) {
      (analysis.table_recommendations || []).forEach(rec => {
        if (!tableMap[rec.table_id]) {
          tableMap[rec.table_id] = {
            tableId: rec.table_id,
            tableName: rec.table_id.split('.').pop(),
            engines: {},
            bestEngine: null,
            bestScore: 0,
          };
        }
        tableMap[rec.table_id].engines[engineName] = {
          confidenceScore: rec.confidence_score,
          patternMatch: rec.score_breakdown?.pattern_match_score || 0,
          complexity: rec.score_breakdown?.complexity_score || 0,
          performance: rec.score_breakdown?.performance_score || 0,
          cost: rec.score_breakdown?.cost_score || 0,
          migrationComplexity: rec.migration_complexity,
          patterns: rec.supporting_patterns || [],
          concerns: rec.concerns || [],
          rationale: rec.rationale,
        };
        if (rec.confidence_score > tableMap[rec.table_id].bestScore) {
          tableMap[rec.table_id].bestScore = rec.confidence_score;
          tableMap[rec.table_id].bestEngine = engineName;
        }
      });
    }
    return Object.values(tableMap).sort((a, b) => b.bestScore - a.bestScore);
  }, [engines]);

  const [expandedTable, setExpandedTable] = useState(null);

  return (
    <SpaceBetween size="m">
      <Box color="text-body-secondary">
        {t("engine-analysis.table-tab.description")}
      </Box>
      <Table
        columnDefinitions={[
          {
            id: "table", header: t("engine-analysis.table-tab.col-source-table"),
            cell: item => (
              <Button variant="inline-link" onClick={() => setExpandedTable(expandedTable === item.tableId ? null : item.tableId)}>
                <Box fontFamily="monospace">{item.tableName}</Box>
              </Button>
            ),
            width: 160, sortingField: "tableName",
          },
          ...engineNames.map(eng => ({
            id: `eng_${eng}`, header: ENGINE_LABELS[eng] || eng,
            cell: item => {
              const e = item.engines[eng];
              if (!e || e.confidenceScore === 0) return <Box color="text-status-inactive">—</Box>;
              const isBest = item.bestEngine === eng;
              return (
                <SpaceBetween direction="horizontal" size="xxs">
                  <Box fontWeight={isBest ? "bold" : "normal"}>{e.confidenceScore}%</Box>
                  {isBest && <Badge color={ENGINE_COLORS[eng] || 'grey'}>{t("engine-analysis.table-tab.best-badge")}</Badge>}
                </SpaceBetween>
              );
            },
            width: 140, sortingField: `eng_${eng}`,
          })),
          {
            id: "best", header: t("engine-analysis.table-tab.col-best-engine"),
            cell: item => item.bestEngine ? (
              <Badge color={ENGINE_COLORS[item.bestEngine] || 'grey'}>{ENGINE_LABELS[item.bestEngine] || item.bestEngine}</Badge>
            ) : <Box color="text-status-inactive">—</Box>,
            width: 130,
          },
        ]}
        items={tableRows}
        sortingDisabled={false}
        variant="embedded"
        stickyHeader
        empty={<Box textAlign="center" padding="l">{t("engine-analysis.table-tab.empty")}</Box>}
      />

      {/* Expanded detail for selected table */}
      {expandedTable && (() => {
        const row = tableRows.find(r => r.tableId === expandedTable);
        if (!row) return null;
        return (
          <Container header={<Header variant="h3"><Box fontFamily="monospace">{row.tableName}</Box></Header>}>
            <ColumnLayout columns={engineNames.length} variant="default" borders="vertical">
              {engineNames.map(eng => {
                const e = row.engines[eng];
                if (!e) return (
                  <Box key={eng} padding="s" textAlign="center">
                    <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                    <Box color="text-status-inactive" padding="s">{t("engine-analysis.table-tab.not-evaluated")}</Box>
                  </Box>
                );
                return (
                  <Box key={eng} padding="s">
                    <SpaceBetween size="s">
                      <Box textAlign="center">
                        <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                        <Box fontSize="display-l" fontWeight="bold" textAlign="center">{e.confidenceScore}%</Box>
                      </Box>
                      <ColumnLayout columns={2} variant="text-grid">
                        <div><Box variant="awsui-key-label">{t("engine-analysis.table-tab.pattern-match")}</Box><Box>{e.patternMatch}</Box></div>
                        <div><Box variant="awsui-key-label">{t("engine-analysis.table-tab.complexity")}</Box><Box>{e.complexity}</Box></div>
                        <div><Box variant="awsui-key-label">{t("engine-analysis.table-tab.performance")}</Box><Box>{e.performance}</Box></div>
                        <div><Box variant="awsui-key-label">{t("engine-analysis.table-tab.cost")}</Box><Box>{e.cost}</Box></div>
                      </ColumnLayout>
                      <div>
                        <Box variant="awsui-key-label">{t("engine-analysis.table-tab.migration")}</Box>
                        <StatusIndicator type={e.migrationComplexity === 'LOW' ? 'success' : e.migrationComplexity === 'MEDIUM' ? 'warning' : 'error'}>
                          {e.migrationComplexity}
                        </StatusIndicator>
                      </div>
                      {e.patterns?.length > 0 && (
                        <div>
                          <Box variant="awsui-key-label">{t("engine-analysis.summary.patterns")}</Box>
                          <Box fontSize="body-s">{e.patterns.join(', ')}</Box>
                        </div>
                      )}
                      {e.concerns?.length > 0 && (
                        <div>
                          <Box variant="awsui-key-label">{t("engine-analysis.table-tab.concerns")}</Box>
                          {e.concerns.map((c, i) => <Box key={i} fontSize="body-s" color="text-status-warning">{c}</Box>)}
                        </div>
                      )}
                      {e.rationale && (
                        <div>
                          <Box variant="awsui-key-label">{t("engine-analysis.table-tab.rationale")}</Box>
                          <Box fontSize="body-s" color="text-body-secondary">{e.rationale}</Box>
                        </div>
                      )}
                    </SpaceBetween>
                  </Box>
                );
              })}
            </ColumnLayout>
          </Container>
        );
      })()}
    </SpaceBetween>
  );
}


const SIGNAL_DESCRIPTIONS = {
  junction_tables: 'Tables acting as many-to-many join tables (composite FK primary keys). Candidates for graph databases or denormalization.',
  key_value_lookups: 'Simple primary key reads returning few rows. Ideal for key-value stores with single-digit millisecond latency.',
  write_heavy: 'High-frequency write operations (inserts/updates ≥5 calls/sec). Engines optimized for sustained write throughput.',
  time_series: 'Queries with timestamp-based ordering or range filters. Natural fit for time-series optimized storage.',
  metadata_config: 'Small reference/config tables with frequent reads. Low-cost on most engines due to small footprint.',
  text_search: 'Full-text or wildcard search queries (LIKE, MATCH). Search engines provide inverted indexes for scalable text matching.',
  leaderboard_pattern: 'Sorted result sets with LIMIT (top-N queries). Sorted sets or materialized views avoid full scans.',
  high_frequency_reads: 'Very high read throughput queries (≥10 calls/sec). Caching layers or read-optimized engines reduce latency.',
  low_frequency_writes: 'Infrequent write operations (INSERT/UPDATE/DELETE below 5 cps). Low frequency does not mean low importance — batch loads, admin operations, and periodic jobs must still be supported.',
  low_frequency_reads: 'Infrequent read operations (SELECT below 0.1 cps). May include admin queries, health checks, COUNT(*) dashboards, or reporting. Must still be supported after migration.',
};

function SignalGroupCard({ signal, queryRows, engines, engineNames }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const signalQueryIds = new Set(signal.query_ids || []);
  const signalQueries = queryRows.filter(r => signalQueryIds.has(r.queryId));
  const queryCount = signal.query_count || signalQueries.length;
  const signalLabel = signal.signal.replace(/_/g, ' ');
  const description = SIGNAL_DESCRIPTIONS[signal.signal] || signal.evidence;

  // If 0 queries, render a minimal card with just header info
  if (signalQueries.length === 0) {
    return (
      <Container
        header={
          <Header variant="h3" counter={t("engine-analysis.signal.zero-queries")}>
            <SpaceBetween direction="horizontal" size="xs">
              <Box textTransform="capitalize" fontWeight="bold">{signalLabel}</Box>
              {signal.targets?.map((tgt, i) => (
                <Badge key={i} color={ENGINE_COLORS[tgt] || 'grey'}>{ENGINE_LABELS[tgt] || tgt}</Badge>
              ))}
            </SpaceBetween>
          </Header>
        }
      >
        <SpaceBetween size="xs">
          <Box color="text-body-secondary" fontSize="body-s">{description}</Box>
          {signal.table_ids?.length > 0 && (
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="awsui-key-label">{t("engine-analysis.signal.related-tables")}</Box>
              {signal.table_ids.map((tbl, i) => <Badge key={i} color="grey">{tbl.split('.').pop()}</Badge>)}
            </SpaceBetween>
          )}
        </SpaceBetween>
      </Container>
    );
  }

  // Compute avg engine score for queries in this signal
  const engineScores = {};
  engineNames.forEach(eng => {
    const scores = signalQueries.map(q => q.engines[eng]?.score || 0).filter(s => s > 0);
    engineScores[eng] = scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 0;
  });

  const totalRps = signalQueries.reduce((sum, q) => sum + (q.rps || 0), 0);
  const recommendedEngines = new Set(signal.targets || []);

  let bestEng = null;
  let bestScore = 0;
  engineNames.forEach(eng => {
    if (engineScores[eng] > bestScore) { bestScore = engineScores[eng]; bestEng = eng; }
  });

  return (
    <Container
      header={
        <Header
          variant="h3"
          counter={`(${queryCount} queries)`}
          actions={
            signalQueries.length > 0 ? (
              <Button variant="icon" iconName={expanded ? "treeview-collapse" : "treeview-expand"} onClick={() => setExpanded(!expanded)} ariaLabel={t("engine-analysis.signal.toggle")} />
            ) : null
          }
        >
          <SpaceBetween direction="horizontal" size="xs">
            <Box textTransform="capitalize" fontWeight="bold">{signalLabel}</Box>
            {signal.targets?.map((tgt, i) => (
              <Badge key={i} color={ENGINE_COLORS[tgt] || 'grey'}>{ENGINE_LABELS[tgt] || tgt}</Badge>
            ))}
            <Box color="text-body-secondary" fontSize="body-s">{totalRps.toFixed(1)} rps</Box>
          </SpaceBetween>
        </Header>
      }
    >
      <SpaceBetween size="m">
        {/* Signal description */}
        <Box color="text-body-secondary" fontSize="body-s">{description}</Box>

        {/* Engine score comparison */}
        <ColumnLayout columns={engineNames.length} variant="default" borders="vertical">
          {engineNames.map(eng => {
            const score = engineScores[eng];
            const isRecommended = recommendedEngines.has(eng);
            const isBest = eng === bestEng && bestScore > 0;
            return (
              <Box key={eng} padding="s" textAlign="center">
                <SpaceBetween size="xxs">
                  <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                  <Box fontSize="display-l" fontWeight="bold" color={score === 0 ? "text-status-inactive" : undefined}>
                    {score || '—'}
                  </Box>
                  {isBest && <Badge color={ENGINE_COLORS[eng] || 'grey'}>{t("engine-analysis.signal.best-fit")}</Badge>}
                  {isRecommended && !isBest && score > 0 && (
                    <Box fontSize="body-s" color="text-status-success">{t("engine-analysis.signal.recommended")}</Box>
                  )}
                  {isRecommended && score === 0 && (
                    <Box fontSize="body-s" color="text-status-warning">{t("engine-analysis.signal.recommended-no-score")}</Box>
                  )}
                </SpaceBetween>
              </Box>
            );
          })}
        </ColumnLayout>

        {/* Table-level info */}
        {signal.table_ids?.length > 0 && (
          <div>
            <Box variant="awsui-key-label">{t("engine-analysis.signal.related-tables")}</Box>
            <SpaceBetween direction="horizontal" size="xs">
              {signal.table_ids.map((tbl, i) => <Badge key={i} color="grey">{tbl.split('.').pop()}</Badge>)}
            </SpaceBetween>
          </div>
        )}

        {/* Expanded: individual queries */}
        {expanded && signalQueries.length > 0 && (
          <Table
            columnDefinitions={[
              { id: "qid", header: t("engine-analysis.table.col-query-id"), cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.queryId.substring(0, 10)}</Box>, width: 100 },
              { id: "type", header: t("common.labels.type"), cell: item => <Badge>{item.queryType}</Badge>, width: 80 },
              { id: "sql", header: t("engine-analysis.table.col-sql"), cell: item => (
                <Box fontFamily="monospace" fontSize="body-s" color="text-body-secondary" whiteSpace="normal">
                  {item.queryText.length > 150 ? item.queryText.substring(0, 150) + '…' : item.queryText}
                </Box>
              )},
              { id: "tables", header: t("engine-analysis.table.col-tables"), cell: item => item.tablesAccessed.map(tbl => tbl.split('.').pop()).join(', '), width: 130 },
              { id: "rps", header: t("engine-analysis.table.col-rps"), cell: item => item.rps?.toFixed(1) || '—', width: 70 },
              ...engineNames.map(eng => ({
                id: `e_${eng}`,
                header: ENGINE_LABELS[eng] || eng,
                cell: item => {
                  const s = item.engines[eng]?.score || 0;
                  return s > 0 ? <Box fontWeight={item.bestEngine === eng ? "bold" : "normal"}>{s}</Box> : <Box color="text-status-inactive">—</Box>;
                },
                width: 90,
              })),
            ]}
            items={signalQueries.sort((a, b) => (b.rps || 0) - (a.rps || 0))}
            variant="embedded"
            wrapLines
          />
        )}
      </SpaceBetween>
    </Container>
  );
}

function QueryComparisonTab({ queryRows, engines, triage }) {
  const { t } = useTranslation();
  const engineNames = Object.keys(engines);
  const signals = triage?.signals || [];

  // Queries not covered by any signal
  const coveredIds = new Set(signals.flatMap(s => s.query_ids || []));
  const uncoveredQueries = queryRows.filter(r => !coveredIds.has(r.queryId) && r.bestScore > 0);

  return (
    <SpaceBetween size="l">
      <Box color="text-body-secondary">
        {t("engine-analysis.query-tab.summary", { count: signals.length })}
      </Box>

      {signals.map((signal, i) => (
        <SignalGroupCard
          key={signal.signal}
          signal={signal}
          queryRows={queryRows}
          engines={engines}
          engineNames={engineNames}
        />
      ))}

      {uncoveredQueries.length > 0 && (
        <ExpandableSection headerText={t("engine-analysis.query-tab.uncategorized-queries", { count: uncoveredQueries.length })} variant="container" defaultExpanded={false}>
          <Table
            columnDefinitions={[
              { id: "qid", header: t("engine-analysis.table.col-query-id"), cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.queryId.substring(0, 10)}</Box>, width: 100 },
              { id: "type", header: t("common.labels.type"), cell: item => <Badge>{item.queryType}</Badge>, width: 80 },
              { id: "tables", header: t("engine-analysis.table.col-tables"), cell: item => item.tablesAccessed.map(tbl => tbl.split('.').pop()).join(', '), width: 130 },
              { id: "rps", header: t("engine-analysis.table.col-rps"), cell: item => item.rps?.toFixed(1) || '—', width: 70 },
              ...engineNames.map(eng => ({
                id: `e_${eng}`,
                header: ENGINE_LABELS[eng] || eng,
                cell: item => {
                  const s = item.engines[eng]?.score || 0;
                  return s > 0 ? <Box>{s}</Box> : <Box color="text-status-inactive">—</Box>;
                },
                width: 90,
              })),
            ]}
            items={uncoveredQueries}
            variant="embedded"
            wrapLines
          />
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
}


function WorkloadPatternsTab({ engines }) {
  const { t } = useTranslation();
  const engineNames = Object.keys(engines);

  // Group patterns by type across engines. Same pattern_type from different engines → one row.
  // Also track which queries appear in multiple pattern types.
  const { patternGroups, antiPatternGroups } = useMemo(() => {
    const pMap = {};
    const apMap = {};

    for (const [engineName, analysis] of Object.entries(engines)) {
      (analysis.workload_analysis?.patterns_detected || []).forEach(p => {
        const type = p.pattern_type;
        if (!pMap[type]) pMap[type] = { patternType: type, engines: {}, allQueryIds: new Set(), allTableIds: new Set() };
        pMap[type].engines[engineName] = {
          patternId: p.pattern_id,
          confidence: p.confidence,
          description: p.description,
          queryIds: p.query_ids || [],
          tableIds: p.table_ids || [],
        };
        (p.query_ids || []).forEach(q => pMap[type].allQueryIds.add(q));
        (p.table_ids || []).forEach(t => pMap[type].allTableIds.add(t));
      });

      (analysis.workload_analysis?.anti_patterns_detected || []).forEach(ap => {
        const type = ap.anti_pattern_type;
        if (!apMap[type]) apMap[type] = { patternType: type, engines: {}, allQueryIds: new Set(), allTableIds: new Set(), severityWeight: ap.severity_weight };
        apMap[type].engines[engineName] = {
          patternId: ap.anti_pattern_id,
          severityWeight: ap.severity_weight,
          description: ap.description,
          recommendation: ap.recommendation,
          queryIds: ap.query_ids || [],
          tableIds: ap.table_ids || [],
        };
        (ap.query_ids || []).forEach(q => apMap[type].allQueryIds.add(q));
        (ap.table_ids || []).forEach(t => apMap[type].allTableIds.add(t));
      });
    }

    return {
      patternGroups: Object.values(pMap).sort((a, b) => b.allQueryIds.size - a.allQueryIds.size),
      antiPatternGroups: Object.values(apMap).sort((a, b) => (b.severityWeight || 0) - (a.severityWeight || 0)),
    };
  }, [engines]);

  return (
    <SpaceBetween size="l">
      <Box color="text-body-secondary">
        {t("local-analysis.patterns-tab.description")}
      </Box>

      {patternGroups.map(group => (
        <Container key={group.patternType} header={
          <Header variant="h3" counter={t("local-analysis.patterns-tab.counter", { queries: group.allQueryIds.size, tables: group.allTableIds.size })}>
            <SpaceBetween direction="horizontal" size="xs">
              <Box textTransform="capitalize">{group.patternType.replace(/-/g, ' ')}</Box>
              {Object.keys(group.engines).map(eng => (
                <Badge key={eng} color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
              ))}
            </SpaceBetween>
          </Header>
        }>
          <ColumnLayout columns={engineNames.length} variant="default" borders="vertical">
            {engineNames.map(eng => {
              const e = group.engines[eng];
              if (!e) return (
                <Box key={eng} padding="s" textAlign="center">
                  <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                  <Box color="text-status-inactive" padding="s">{t("local-analysis.patterns-tab.not-detected")}</Box>
                </Box>
              );
              return (
                <Box key={eng} padding="s">
                  <SpaceBetween size="xs">
                    <Box textAlign="center">
                      <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                    </Box>
                    <div>
                      <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.pattern-id")}</Box>
                      <Box fontFamily="monospace" fontSize="body-s">{e.patternId}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.confidence")}</Box>
                      <StatusIndicator type={e.confidence === 'HIGH' ? 'success' : e.confidence === 'MEDIUM' ? 'warning' : 'info'}>
                        {e.confidence}
                      </StatusIndicator>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.queries")}</Box>
                      <Box fontSize="body-s">{e.queryIds.length}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.tables")}</Box>
                      <Box fontSize="body-s">{e.tableIds.map(tbl => tbl.split('.').pop()).join(', ') || '—'}</Box>
                    </div>
                    <Box fontSize="body-s" color="text-body-secondary">{e.description}</Box>
                  </SpaceBetween>
                </Box>
              );
            })}
          </ColumnLayout>
        </Container>
      ))}

      {antiPatternGroups.length > 0 && (
        <>
          <Header variant="h2">{t("local-analysis.patterns-tab.anti-patterns-title")}</Header>
          {antiPatternGroups.map(group => (
            <Container key={group.patternType} header={
              <Header variant="h3" counter={t("local-analysis.patterns-tab.anti-pattern-counter", { count: group.allQueryIds.size })}>
                <SpaceBetween direction="horizontal" size="xs">
                  <StatusIndicator type="warning">{group.patternType.replace(/-/g, ' ')}</StatusIndicator>
                  {Object.keys(group.engines).map(eng => (
                    <Badge key={eng} color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                  ))}
                </SpaceBetween>
              </Header>
            }>
              <ColumnLayout columns={engineNames.length} variant="default" borders="vertical">
                {engineNames.map(eng => {
                  const e = group.engines[eng];
                  if (!e) return (
                    <Box key={eng} padding="s" textAlign="center">
                      <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                      <Box color="text-status-inactive" padding="s">{t("local-analysis.patterns-tab.not-flagged")}</Box>
                    </Box>
                  );
                  return (
                    <Box key={eng} padding="s">
                      <SpaceBetween size="xs">
                        <Box textAlign="center">
                          <Badge color={ENGINE_COLORS[eng] || 'grey'}>{ENGINE_LABELS[eng] || eng}</Badge>
                        </Box>
                        <div>
                          <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.severity")}</Box>
                          <Box>{e.severityWeight}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.queries-affected")}</Box>
                          <Box fontSize="body-s">{e.queryIds.length}</Box>
                        </div>
                        <Box fontSize="body-s" color="text-body-secondary">{e.description}</Box>
                        {e.recommendation && (
                          <div>
                            <Box variant="awsui-key-label">{t("local-analysis.patterns-tab.recommendation")}</Box>
                            <Box fontSize="body-s">{e.recommendation}</Box>
                          </div>
                        )}
                      </SpaceBetween>
                    </Box>
                  );
                })}
              </ColumnLayout>
            </Container>
          ))}
        </>
      )}
    </SpaceBetween>
  );
}

function CostComparisonTab({ engines }) {
  const { t } = useTranslation();
  const rows = useMemo(() => {
    return Object.entries(engines).map(([engineName, analysis]) => {
      const cost = analysis.cost_estimate || {};
      return {
        engine: engineName,
        monthlyCost: cost.monthly_cost_usd || 0,
        components: cost.cost_components || {},
        assumptions: cost.pricing_assumptions || [],
      };
    }).sort((a, b) => a.monthlyCost - b.monthlyCost);
  }, [engines]);

  return (
    <ColumnLayout columns={rows.length} variant="default" borders="vertical">
      {rows.map(r => (
        <Container key={r.engine} header={
          <Header variant="h3">
            <Badge color={ENGINE_COLORS[r.engine] || 'grey'}>{ENGINE_LABELS[r.engine] || r.engine}</Badge>
          </Header>
        }>
          <SpaceBetween size="m">
            <Box textAlign="center">
              <Box fontSize="display-l" fontWeight="bold">${r.monthlyCost.toFixed(2)}</Box>
              <Box color="text-body-secondary">{t("engine-analysis.cost-tab.per-month")}</Box>
            </Box>
            <ExpandableSection headerText={t("engine-analysis.cost-tab.cost-breakdown")} variant="footer">
              <SpaceBetween size="xs">
                {Object.entries(r.components).map(([k, v]) => (
                  <Box key={k}>
                    <Box variant="awsui-key-label">{k.replace(/_/g, ' ')}</Box>
                    <Box>{typeof v === 'number' ? `$${v.toFixed(2)}` : String(v)}</Box>
                  </Box>
                ))}
              </SpaceBetween>
            </ExpandableSection>
            <ExpandableSection headerText={t("engine-analysis.cost-tab.pricing-assumptions")} variant="footer">
              <SpaceBetween size="xxs">
                {r.assumptions.map((a, i) => <Box key={i} fontSize="body-s" color="text-body-secondary">{a}</Box>)}
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        </Container>
      ))}
    </ColumnLayout>
  );
}


const LocalAnalysisPage = memo(() => {
  const { t } = useTranslation();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const { queryRows, engines, engineStats, collector, triage, loading, error } = useLocalAnalysis();

  const engineNames = Object.keys(engines);
  const dbName = collector?.metadata?.source_database?.database_name || 'Unknown';
  const sourceEngine = collector?.metadata?.source_database?.engine || 'unknown';
  const totalQueries = queryRows.length;
  const coveredQueries = queryRows.filter(r => r.bestScore > 0).length;

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={
          <BreadcrumbGroup items={[
            { href: "/", text: t("dashboard.breadcrumb.home") },
            { href: "/dashboard", text: t("dashboard.breadcrumb.dashboard") },
            { href: "/analysis/local", text: t("local-analysis.breadcrumb.local-analysis") },
          ]} />
        }
        navigation={
          <SideNavigation
            activeHref="/analysis/local"
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          loading ? (
            <Box textAlign="center" padding={{ top: "xxxl" }}>
              <StatusIndicator type="loading">{t("local-analysis.states.loading")}</StatusIndicator>
            </Box>
          ) : error ? (
            <Box textAlign="center" padding={{ top: "xxxl" }}>
              <StatusIndicator type="error">{t("engine-analysis.states.error", { error })}</StatusIndicator>
            </Box>
          ) : (
            <SpaceBetween size="l">
              {/* Page header */}
              <Header
                variant="h1"
                description={t("engine-analysis.header.description", { dbName, sourceEngine, engineCount: engineNames.length, queryCount: totalQueries })}
              >
                {t("local-analysis.header.title", { dbName })}
              </Header>

              {/* Engine summary cards */}
              <Container header={<Header variant="h2" description={t("engine-analysis.overview.description")}>{t("engine-analysis.overview.title")}</Header>}>
                <EngineSummaryCards engineStats={engineStats} />
              </Container>

              {/* Coverage bar */}
              <Container>
                <ColumnLayout columns={3} variant="text-grid">
                  <div>
                    <Box variant="awsui-key-label">{t("engine-analysis.stats.query-coverage")}</Box>
                    <ProgressBar
                      value={totalQueries > 0 ? Math.round((coveredQueries / totalQueries) * 100) : 0}
                      additionalInfo={t("engine-analysis.stats.query-coverage-detail", { covered: coveredQueries, total: totalQueries })}
                    />
                  </div>
                  <div>
                    <Box variant="awsui-key-label">{t("engine-analysis.stats.source-database")}</Box>
                    <Box fontSize="heading-m">{sourceEngine} {collector?.metadata?.source_database?.version}</Box>
                    <Box color="text-body-secondary">{collector?.metadata?.source_database?.database_size_gb?.toFixed(2)} GB</Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">{t("engine-analysis.stats.triage-confidence")}</Box>
                    <Box fontSize="heading-m">{triage?.confidence_score || 'N/A'}%</Box>
                    <Box color="text-body-secondary">
                      {t("engine-analysis.stats.triage-agents", { selected: triage?.selected_agents?.length || 0, skipped: triage?.skipped_agents?.length || 0 })}
                    </Box>
                  </div>
                </ColumnLayout>
              </Container>

              {/* Main tabs */}
              <Tabs
                tabs={[
                  {
                    id: "queries",
                    label: t("engine-analysis.tabs.query-comparison", { count: totalQueries }),
                    content: <QueryComparisonTab queryRows={queryRows} engines={engines} triage={triage} />,
                  },
                  {
                    id: "tables",
                    label: t("engine-analysis.tabs.table-recommendations"),
                    content: <TableAnalysisTab engines={engines} />,
                  },
                  {
                    id: "patterns",
                    label: t("local-analysis.tabs.workload-patterns"),
                    content: <WorkloadPatternsTab engines={engines} />,
                  },
                  {
                    id: "cost",
                    label: t("engine-analysis.tabs.cost-comparison"),
                    content: <CostComparisonTab engines={engines} />,
                  },
                ]}
              />
            </SpaceBetween>
          )
        }
      />
    </>
  );
});

export default LocalAnalysisPage;
