// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ExecutiveSummaryStats from './ExecutiveSummaryStats';
import { readinessText, readinessType, waveReadiness } from '../utils/migrationWaves';
import { executiveSummaryOf } from '../utils/reportFacts';

/**
 * The results page's executive summary (#2315): it leads with the decision and five key
 * figures, so the answer survives a five-second look.
 *
 * - The Container header states the decision, the line the facts word for it
 *   (facts.executive_brief.decision_headline: "Approve wave 1: move wordpress to Aurora
 *   MySQL", or the plan when no wave is suggested yet), never a model's text, with the
 *   readiness of that wave (facts.waves[].readiness) as a StatusIndicator. A report with
 *   no headline keeps the "Executive summary" title.
 * - One row of five figures (ExecutiveSummaryStats).
 * - The summary's paragraphs follow under "Why", in body size (`why`, the page's own
 *   prose and notes).
 *
 * It only formats what the facts state (ADR-032). `statsProps` are ExecutiveSummaryStats'
 * own props, passed through; callers pass `t` (the page's own `useTranslation` function).
 */
export default function ExecutiveSummary({ synthesis, t, why, statsProps }) {
  const summary = executiveSummaryOf(synthesis);
  const view = summary.statusWave === null ? null : waveReadiness(synthesis, { wave: summary.statusWave });
  const status = view && readinessText(view, t);
  const title = t('analysis-results.executive-summary.title');
  return (
    <Container
      id="executive-summary-section"
      header={(
        <Header
          variant="h2"
          description={summary.headline ? title : undefined}
          actions={status ? (
            <span data-figure="decision-readiness" data-readiness={view.readiness || ''}>
              <StatusIndicator type={readinessType(view)}>
                {t('analysis-results.executive-summary.wave-readiness', { wave: summary.statusWave, readiness: status })}
              </StatusIndicator>
            </span>
          ) : undefined}
        >
          <span data-figure="decision-headline">{summary.headline || title}</span>
        </Header>
      )}
    >
      <SpaceBetween size="l">
        <ExecutiveSummaryStats t={t} summary={summary} {...statsProps} />
        <ExpandableSection
          headerText={t('analysis-results.executive-summary.why')}
          defaultExpanded
        >
          <div data-section="executive-summary-why">{why}</div>
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
}
