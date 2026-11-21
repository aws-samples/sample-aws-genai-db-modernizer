// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import HelpPanel from "@cloudscape-design/components/help-panel";
import Link from "@cloudscape-design/components/link";
import { helpPanelContent } from "../utils/resultsHelp";

export const RESULTS_HELP_PANEL_ID = 'results-help-panel';

/**
 * #2512: one results page help topic in the AppLayout's help drawer: its title, the
 * explanation its section's Info link points to, and a link to the matching section of
 * the help site, opened in a new tab. `context` is resultsHelpContext(synthesis).
 */
export default function ResultsHelpPanel({ topic, context, t }) {
  const content = helpPanelContent(topic, t, context);
  return (
    <div id={RESULTS_HELP_PANEL_ID} data-help-topic={content.id}>
      <HelpPanel
        header={<h2>{content.title}</h2>}
        footer={(
          <div>
            <h3>{t('analysis-results.help.learn-more')}</h3>
            <Link
              external
              href={content.url}
              externalIconAriaLabel={t('common.header.opens-new-tab')}
            >
              {t('analysis-results.help.site-link', { title: content.title })}
            </Link>
          </div>
        )}
      >
        {content.paragraphs.map(text => <p key={text}>{text}</p>)}
      </HelpPanel>
    </div>
  );
}
