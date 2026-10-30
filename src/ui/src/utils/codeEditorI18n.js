// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Shared `i18nStrings` for every Cloudscape `CodeEditor` in this app (#478)
 * -- two today: the Results page's Aurora DDL viewer and its
 * query-journey JSON viewer.
 *
 * Cloudscape's `CodeEditor` builds its status bar and ARIA description from
 * `i18nStrings.errorsTab` / `warningsTab` / `cursorPosition` internally
 * (`@cloudscape-design/components/code-editor`): with no `I18nProvider`
 * configured for this app and none of the three supplied, its internal
 * fallback resolves to literal `undefined`, which then lands in the
 * rendered page as `"0 undefined, 0 undefined"` -- a real customer-facing
 * regression (an e2e-llm UI check on a real Bedrock-produced Aurora design
 * caught it: the DDL viewer's hidden ARIA description showed up in
 * `page.inner_text("body")` even collapsed inside an `ExpandableSection`).
 * None of these editors ever have lint errors or warnings to show (they are
 * read-only viewers over DDL/JSON, not an editing surface), but Cloudscape
 * still renders the count+label pair unconditionally, so the labels must
 * always resolve to real text. Every `<CodeEditor` call site must pass this
 * helper's result as its `i18nStrings` -- enforced by
 * `src/ui/src/utils/__tests__/codeEditorI18n.allCallSites.test.js`, which
 * scans the source tree for `<CodeEditor` and fails if a new one skips it.
 */
export function codeEditorI18nStrings(t, extra = {}) {
  return {
    errorsTab: t('common.code-editor.errors-tab'),
    warningsTab: t('common.code-editor.warnings-tab'),
    cursorPosition: (row, column) => t('common.code-editor.cursor-position', { row, column }),
    // Accessible names for the editor's icon-only controls (#987: axe
    // button-name on the preferences cog, aria-input-field-name on the
    // resize handle) and its groups.
    preferencesButtonAriaLabel: t('common.code-editor.preferences'),
    resizeHandleAriaLabel: t('common.code-editor.resize-handle'),
    editorGroupAriaLabel: t('common.code-editor.editor-group'),
    statusBarGroupAriaLabel: t('common.code-editor.status-bar'),
    paneCloseButtonAriaLabel: t('common.actions.close'),
    preferencesModalCloseAriaLabel: t('common.actions.close'),
    ...extra,
  };
}
