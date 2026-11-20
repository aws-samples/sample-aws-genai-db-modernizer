// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { stepGateOf } from "../utils/reportFacts";
import { formatCount } from "../utils/format";

/**
 * "Step 1 gate evidence" (#1801, ADR-033 Decision 10): what step 1's read-only
 * gate read from DMS and CloudWatch, from report.json's facts.step_gate -- the
 * same block the engineering report's section of that name and the executive
 * report's wave plan print. The result and the cutover message, verbatim, up
 * front; the full load, lag and validation lines; the findings of a check that
 * did not pass; each table and named exception in sections collapsed when the
 * page opens. Nothing when the step's gate never ran.
 *
 * It only formats the facts (ADR-032): every count, line and sentence is
 * stored. Table names, DMS states and reasons are the collection's and DMS's
 * text, rendered as plain React text. Callers pass `t` (the page's own
 * `useTranslation` function).
 */

const STATUS_TYPE = { pass: 'success', pending: 'pending', fail: 'error' };

function Mono({ children }) {
  return <span style={{ fontFamily: 'monospace' }}>{children}</span>;
}

function rowsText(value, t) {
  return typeof value === 'number' ? formatCount(value) : t('analysis-results.step-gate.not-reported');
}

const StepGateEvidence = memo(({ synthesis, t }) => {
  const gate = stepGateOf(synthesis);
  if (!gate) return null;
  const tables = (gate.gate_tables || []).filter(r => r && typeof r === 'object');
  const exceptions = (gate.named_exceptions || []).filter(r => r && typeof r === 'object');
  const findings = (gate.gate_findings || []).filter(r => r && typeof r === 'object');
  const tableColumns = [
    { id: 'table', header: t('analysis-results.step-gate.column-table'), cell: r => <Mono>{r.table}</Mono> },
    { id: 'rows', header: t('analysis-results.step-gate.column-rows'), cell: r => rowsText(r.rows_loaded, t) },
    {
      id: 'state',
      header: t('analysis-results.step-gate.column-state'),
      cell: r => r.validation_state || t('analysis-results.step-gate.not-reported'),
    },
    { id: 'failed', header: t('analysis-results.step-gate.column-failed'), cell: r => rowsText(r.failed_records, t) },
    { id: 'suspended', header: t('analysis-results.step-gate.column-suspended'), cell: r => rowsText(r.suspended_records, t) },
    { id: 'result', header: t('analysis-results.step-gate.column-result'), cell: r => r.status_text },
  ];
  const exceptionColumns = [
    { id: 'table', header: t('analysis-results.step-gate.column-table'), cell: r => <Mono>{r.table}</Mono> },
    { id: 'column', header: t('analysis-results.step-gate.column-column'), cell: r => (r.column ? <Mono>{r.column}</Mono> : '') },
    { id: 'kind', header: t('analysis-results.step-gate.column-exception'), cell: r => r.kind_text },
    { id: 'reason', header: t('analysis-results.step-gate.column-reason'), cell: r => r.reason },
    { id: 'rows', header: t('analysis-results.step-gate.column-rows'), cell: r => rowsText(r.rows_loaded, t) },
  ];
  return (
    <div id="step-gate-section">
      <Container
        header={(
          <Header variant="h2" description={t('analysis-results.step-gate.description', { readAt: gate.read_at_text })}>
            {t('analysis-results.step-gate.title')}
          </Header>
        )}
      >
        <SpaceBetween size="s">
          <Box data-figure="step-gate-result">
            <StatusIndicator type={STATUS_TYPE[gate.status] || 'info'}>{gate.status_text}</StatusIndicator>
            {' '}
            <span data-figure="step-gate-result-line">{gate.result_line}</span>
          </Box>
          <Box data-figure="step-gate-cutover-message"><em>{gate.cutover_message}</em></Box>
          <ul>
            <li data-figure="step-gate-load-line">{gate.load_line}</li>
            <li data-figure="step-gate-lag-line">{gate.lag_line}</li>
            <li data-figure="step-gate-validation-line">{gate.validation_line}</li>
          </ul>
          {findings.length > 0 && (
            <Box data-figure="step-gate-findings">
              <Box variant="h3">{t('analysis-results.step-gate.findings')}</Box>
              <ul>
                {findings.map((f, i) => (
                  <li key={`${f.check}-${i}`}>{`${f.check_text}: `}{f.text}</li>
                ))}
                {gate.gate_findings_not_shown > 0 && (
                  <li>{t('analysis-results.step-gate.more-findings', { count: gate.gate_findings_not_shown })}</li>
                )}
              </ul>
            </Box>
          )}
          {tables.length > 0 && (
            <ExpandableSection
              defaultExpanded={false}
              headerText={t('analysis-results.step-gate.tables', { count: gate.table_count })}
            >
              <Table
                items={tables}
                columnDefinitions={tableColumns}
                trackBy={r => r.table}
                variant="embedded"
                wrapLines
                ariaLabels={{ tableLabel: t('analysis-results.step-gate.title') }}
              />
            </ExpandableSection>
          )}
          {exceptions.length > 0 && (
            <ExpandableSection
              defaultExpanded={false}
              headerText={t('analysis-results.step-gate.exceptions', { count: gate.exception_count })}
            >
              <SpaceBetween size="xs">
                <Box>{t('analysis-results.step-gate.exceptions-intro')}</Box>
                <Table
                  items={exceptions}
                  columnDefinitions={exceptionColumns}
                  trackBy={r => `${r.table}:${r.kind}:${r.column || ''}`}
                  variant="embedded"
                  wrapLines
                  ariaLabels={{ tableLabel: t('analysis-results.step-gate.exceptions', { count: gate.exception_count }) }}
                />
              </SpaceBetween>
            </ExpandableSection>
          )}
        </SpaceBetween>
      </Container>
    </div>
  );
});

export default StepGateEvidence;
