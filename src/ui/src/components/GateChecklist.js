// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { Fragment, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TextFilter from "@cloudscape-design/components/text-filter";

/**
 * A gate as a checklist (#2287): one item per condition, as report.json's facts cut it
 * (facts.waves[].gate_conditions, facts.slice_plan's step_stages[].gate_conditions). A
 * condition to check carries a "not started" status; a note (whose a step is, such as
 * the cutover) is plain text. The table lists a condition names follow its sentence as
 * "Show N tables", a section that opens in place, with a filter on the name for a long
 * list (TableNameList). Nothing here reads the gate's prose; the conditions joined back are the
 * gate the reports print.
 *
 * `figure` is the data-figure of the list (`wave-gate`, `stage-gate`); each item is
 * `gate-condition` with its `data-kind`, each list `gate-names`.
 */

// The longest list shown without a filter and a scroll box of its own.
const LIST_FILTER_MIN = 15;

const NAMES_STYLE = { margin: 0, paddingLeft: '1.2em', overflowWrap: 'anywhere' };
// A list longer than LIST_FILTER_MIN scrolls in its own box, which takes keyboard focus
// so a keyboard can scroll it (WCAG 2.1.1); a shorter list never scrolls and adds no stop.
const SCROLL_STYLE = { ...NAMES_STYLE, maxHeight: '18em', overflowY: 'auto' };

const CHECKLIST_STYLE = { listStyle: 'none', margin: 0, padding: 0 };
const CONDITION_STYLE = { marginBottom: '4px' };

// A list of table names as a real list, with a filter on the name when `filter` is set.
// The filter changes only what is shown; the names are the facts' own, in their order.
export function TableNameList({ names, t, label, figure = 'gate-names', filter = false }) {
  const [text, setText] = useState('');
  const needle = text.trim().toLowerCase();
  const shown = needle ? names.filter(n => String(n).toLowerCase().includes(needle)) : names;
  const scrolls = names.length > LIST_FILTER_MIN;
  return (
    <SpaceBetween size="xs">
      {filter && (
        <TextFilter
          filteringText={text}
          filteringPlaceholder={t('analysis-results.waves.names-filter-placeholder')}
          filteringAriaLabel={t('analysis-results.waves.names-filter-label')}
          countText={needle ? t('analysis-results.waves.names-matches', { count: shown.length }) : undefined}
          onChange={({ detail }) => setText(detail.filteringText)}
        />
      )}
      {shown.length > 0 ? (
        <ul
          style={scrolls ? SCROLL_STYLE : NAMES_STYLE}
          aria-label={label}
          data-figure={figure}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={scrolls ? 0 : undefined}
        >
          {shown.map(name => <li key={name} data-figure="gate-name">{name}</li>)}
        </ul>
      ) : (
        <Box color="text-body-secondary">{t('analysis-results.waves.names-no-match')}</Box>
      )}
    </SpaceBetween>
  );
}

// "Show N tables" for one list of a condition: a section that opens in place, so the
// names are part of the page (no popover: Cloudscape heads one with an h2, which would
// break the wave's heading order, and without a header it is no dialog). A list longer
// than LIST_FILTER_MIN names opens with a filter on the name.
function NamesToggle({ names, t }) {
  const label = t('analysis-results.waves.names-header', { count: names.length });
  return (
    <div data-figure="gate-names-toggle">
      <ExpandableSection variant="inline" headerText={t('analysis-results.waves.names-show', { count: names.length })}>
        <TableNameList names={names} t={t} label={label} filter={names.length > LIST_FILTER_MIN} />
      </ExpandableSection>
    </div>
  );
}

function conditionText(condition) {
  return (condition.parts || []).map(p => String((p && p.text) || '')).join('').trim();
}

function conditionLists(condition) {
  return (condition.parts || []).map(p => (p && Array.isArray(p.items) ? p.items : [])).filter(items => items.length > 0);
}

export default function GateChecklist({ conditions, t, label, figure }) {
  const rows = (conditions || []).filter(c => c && typeof c === 'object');
  if (rows.length === 0) return null;
  return (
    <ul style={CHECKLIST_STYLE} aria-label={label} data-figure={figure}>
      {/* Conditions have no id; their order is the report's. */}
      {rows.map((condition, i) => {
        const kind = condition.kind === 'note' ? 'note' : 'check';
        const lists = conditionLists(condition);
        return (
          <li key={i} style={CONDITION_STYLE} data-figure="gate-condition" data-kind={kind}>
            {kind === 'note' ? (
              <Box variant="span"><span data-figure="gate-condition-text">{conditionText(condition)}</span></Box>
            ) : (
              <StatusIndicator type="not-started" iconAriaLabel={t('analysis-results.waves.gate-to-check')} wrapText>
                <span data-figure="gate-condition-text">{conditionText(condition)}</span>
              </StatusIndicator>
            )}
            {lists.map((items, j) => (
              <Fragment key={j}>{' '}<NamesToggle names={items} t={t} /></Fragment>
            ))}
          </li>
        );
      })}
    </ul>
  );
}
