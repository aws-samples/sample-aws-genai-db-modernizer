import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import Icon from "@cloudscape-design/components/icon";
import ExpandableSection from "@cloudscape-design/components/expandable-section";

/**
 * Notice shown when a generated executive summary was replaced by its
 * deterministic fallback (#269). Shared by the Assignment review page
 * (AssignmentGate.js, the reality-check summary) and the Results page
 * (AnalysisResults-02.js, the synthesis summary) -- the two differ only in
 * their notice text and in whether there is a warnings list to expand:
 *
 * - The synthesis summary's `text` already names the check-specific reason
 *   (`fallbackWithheldReason`, ported from `formatting.fallback_withheld_reason`)
 *   and passes no `warnings`, so no expandable section renders.
 * - The reality-check summary has only one check, so there is no comparable
 *   reason to name; its raw `warnings` render in an expandable section
 *   instead.
 *
 * Callers pass already-translated text (each page's own `t(...)` call) --
 * this component carries no i18n dependency of its own.
 */
const SummaryFallbackNotice = memo(({ text, warnings = [], detailsHeaderText, className }) => (
  <div className={className}>
    <Box fontSize="body-s" color="text-status-warning" padding={{ top: 'xs' }}>
      <Icon name="status-warning" variant="warning" /> {text}
    </Box>
    {warnings.length > 0 && (
      <ExpandableSection headerText={detailsHeaderText} variant="footer">
        <ul style={{ margin: 0, paddingLeft: '20px' }}>
          {warnings.map((warning, idx) => (
            <li key={idx}>
              <Box fontSize="body-s" color="text-body-secondary">{warning}</Box>
            </li>
          ))}
        </ul>
      </ExpandableSection>
    )}
  </div>
));

SummaryFallbackNotice.displayName = 'SummaryFallbackNotice';

export default SummaryFallbackNotice;
