// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import HelpPanel from "@cloudscape-design/components/help-panel";
import AnchorNavigation from "@cloudscape-design/components/anchor-navigation";
import { hashId } from "../utils/resultsTabs";

const TITLE_ID = 'results-on-this-page-title';

/**
 * #2313: "On this page" in the results page's tools panel: the open tab's sections as
 * Cloudscape AnchorNavigation (it marks the section in view as the reader scrolls).
 * `anchors` come from onThisPageAnchors; following one calls `onGo(id)`, the page's
 * own navigation, which focuses the section, instead of the browser's jump.
 */
export default function ResultsOnThisPage({ anchors, onGo, t }) {
  if (!anchors || anchors.length === 0) return null;
  return (
    <HelpPanel header={<h2 id={TITLE_ID}>{t('analysis-results.on-this-page.title')}</h2>}>
      <AnchorNavigation
        ariaLabelledby={TITLE_ID}
        anchors={anchors}
        onFollow={(event) => {
          event.preventDefault();
          onGo(hashId(event.detail.href));
        }}
      />
    </HelpPanel>
  );
}
