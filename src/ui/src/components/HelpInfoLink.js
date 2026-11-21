// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useContext } from 'react';
import Link from "@cloudscape-design/components/link";
import { ResultsHelp } from "../hooks/useResultsDrawers";
import { helpPanelContent } from "../utils/resultsHelp";

/**
 * #2512: a section header's Cloudscape "Info" link. It opens `topic` in the results
 * page's help panel; Link renders it as a button, so Enter and Space work as a click.
 * Its label names the section, or is `ariaLabel` when the link says something
 * narrower. Outside the results page (no ResultsHelp provider) it renders nothing.
 */
export default function HelpInfoLink({ topic, t, ariaLabel }) {
  const openHelp = useContext(ResultsHelp);
  if (!openHelp) return null;
  const { title } = helpPanelContent(topic, t);
  return (
    <Link
      variant="info"
      ariaLabel={ariaLabel || t('analysis-results.help.info-label', { section: title })}
      nativeAttributes={{ 'data-help-topic': topic }}
      onFollow={(event) => {
        event.preventDefault();
        openHelp(topic);
      }}
    >
      {t('analysis-results.help.info')}
    </Link>
  );
}
