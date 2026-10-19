import { memo, useCallback } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import NoOpenRisksNote from "./NoOpenRisksNote";

/**
 * #996: the minimal risk section for a report with nothing else to say about
 * risk (no open or resolved risk, no overall level): the engineering report
 * still prints the no-open-risks sentence there, so the results page does too.
 */
const NO_SHOWN_RISKS = [];

const NoOpenRisksSection = memo(({ synthesis, caveat, title }) => {
  // Stable between renders, so NoOpenRisksNote's memo can skip.
  const wrap = useCallback(
    (note) => <Container header={<Header variant="h2">{title}</Header>}>{note}</Container>,
    [title],
  );
  return <NoOpenRisksNote synthesis={synthesis} shownRisks={NO_SHOWN_RISKS} caveat={caveat} wrap={wrap} />;
});

export default NoOpenRisksSection;
