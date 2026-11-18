// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from '@cloudscape-design/components/box';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Popover from '@cloudscape-design/components/popover';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import EngineBadge from './EngineBadge';
import ProseText from './ProseText';
import {
  mitigationRepeatsDescription,
  riskSeverityLabel,
  riskSeverityStatus,
  riskTitle,
  splitRiskDescription,
} from '../utils/riskAssessment';

/**
 * One open risk (#2322), open or closed as its parent says (`expanded`, changed through
 * `onToggle(id, expanded)`), in the pattern of a design decision (#2421). The header, a
 * Cloudscape button that Enter and Space operate, holds the severity in sentence case,
 * the engine, the risk id and a one-line title. The body holds the full description, the
 * mitigation, the affected tables in a popover, and the design notes in a nested
 * expandable. `headingLevel` is the heading the header takes, one under the section's.
 * It only formats the risk (ADR-032), and no count is derived here: the severity counts
 * on the tabs come from the facts. Every field is untrusted text, rendered as text.
 */
function RiskCard({
  id, risk, severity, expanded, onToggle, headingLevel, engineLabel, mitigationRef, copySync, t,
}) {
  const { engine, body } = splitRiskDescription(risk.description);
  const title = riskTitle(risk);
  const showMitigation = risk.mitigation && !mitigationRepeatsDescription(risk.mitigation, body);
  const affectedTables = Array.isArray(risk.affected_tables) ? risk.affected_tables : [];
  const notes = Array.isArray(risk.details) ? risk.details : [];
  const hasSync = Boolean(copySync);
  const status = riskSeverityStatus(severity);
  const items = [];
  if (showMitigation) {
    items.push({
      label: t('analysis-results.risk-assessment.mitigation'),
      value: mitigationRef
        ? t('analysis-results.risk-assessment.mitigation-as-for', { risk: mitigationRef })
        : <ProseText text={risk.mitigation} />,
    });
  }
  if (affectedTables.length > 0) {
    items.push({
      label: t('analysis-results.risk-assessment.affected-tables'),
      value: (
        <Popover
          header={t('analysis-results.risk-assessment.affected-tables-header')}
          size="large"
          dismissAriaLabel={t('analysis-results.explorer.dismiss-aria-label')}
          content={(
            <ul className="risk-affected-tables" style={{ margin: 0, paddingLeft: '1.25em' }}>
              {affectedTables.map((name, i) => <li key={i}>{String(name)}</li>)}
            </ul>
          )}
        >
          {t('analysis-results.risk-assessment.affected-tables-trigger')}
        </Popover>
      ),
    });
  }
  const header = (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      <StatusIndicator type={status}>{riskSeverityLabel(severity, t)}</StatusIndicator>
      {engine !== '(general)' && <EngineBadge engine={engine}>{engineLabel || engine}</EngineBadge>}
      {risk.risk_id && <Box fontSize="body-s" color="text-body-secondary">{risk.risk_id}</Box>}
      <span>{title}</span>
    </SpaceBetween>
  );
  return (
    <div className={`dbm-card dbm-card--${status}`} data-risk-card="" data-risk-id={risk.risk_id || undefined}>
      <Box padding={{ vertical: 'xs', horizontal: 's' }}>
        <ExpandableSection
          variant="inline"
          headingTagOverride={headingLevel}
          expanded={expanded}
          onChange={({ detail }) => onToggle(id, detail.expanded)}
          headerText={header}
        >
          <SpaceBetween size="s">
            {body !== title && <Box fontSize="body-s"><ProseText text={body} /></Box>}
            {items.length > 0 && <KeyValuePairs columns={1} items={items} />}
            {(hasSync || notes.length > 0) && (
              <ExpandableSection
                variant="footer"
                headingTagOverride={`h${Math.min(6, Number(headingLevel.slice(1)) + 1)}`}
                headerText={t('analysis-results.risk-assessment.design-notes')}
              >
                <ul className="risk-details" style={{ margin: 0, paddingLeft: '1.25em' }}>
                  {hasSync && copySync.lines.map((line, i) => (
                    <li key={`s${i}`}><Box fontSize="body-s" color="text-body-secondary"><ProseText text={line} /></Box></li>
                  ))}
                  {hasSync && copySync.basis && (
                    <li key="basis"><Box fontSize="body-s" color="text-body-secondary">{t('analysis-results.risk-assessment.sync-basis')}: <ProseText text={copySync.basis} /></Box></li>
                  )}
                  {notes.map((detail, i) => (
                    <li key={`d${i}`}><Box fontSize="body-s" color="text-body-secondary"><ProseText text={detail} /></Box></li>
                  ))}
                </ul>
              </ExpandableSection>
            )}
          </SpaceBetween>
        </ExpandableSection>
      </Box>
    </div>
  );
}

export default memo(RiskCard);
