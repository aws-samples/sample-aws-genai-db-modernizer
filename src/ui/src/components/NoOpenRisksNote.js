import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import { openRiskCountOf } from "../utils/reportFacts";

/**
 * #996: the sentence the engineering and executive reports print when no risk is
 * open: "no open risks" is not "no migration risk". The text is the /results
 * payload's `no_open_risks_caveat` (one definition, src/report/risks.py); this
 * only decides whether to show it, from facts.open_risk_count (ADR-032).
 */
const NoOpenRisksNote = memo(({ synthesis, shownRisks, caveat, wrap }) => {
  if (!caveat || openRiskCountOf(synthesis, shownRisks) !== 0) return null;
  const note = (
    <div id="no-open-risks-note">
      <Box variant="small" color="text-body-secondary">{caveat}</Box>
    </div>
  );
  return wrap ? wrap(note) : note;
});

export default NoOpenRisksNote;
