import { memo } from 'react';
import Box from "@cloudscape-design/components/box";

/**
 * #1389: the sentence the engineering and executive reports print for a report
 * written before utility pins left the mean fit. The text is the /results
 * payload's `pin_rule_note` (one definition, src/shared/ranking.py); this only
 * shows it when the payload sets it.
 */
const PinRuleNote = memo(({ note }) => {
  if (!note) return null;
  return (
    <div id="pin-rule-note">
      <Box variant="small" color="text-body-secondary">{note}</Box>
    </div>
  );
});

export default PinRuleNote;
