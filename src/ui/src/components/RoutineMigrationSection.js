// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { routineKey, routinesOf, routineToDoText } from "../utils/reportFacts";

/**
 * "Routines" (#1939, ADR-034 Decision 7): how the job's routines migrate, from
 * report.json's facts.routines (#1938) -- the same facts the engineering
 * report's Routines section, the executive report and the chat summary print.
 * The headline and the to-do are shown up front, as the executive report and
 * the chat summary give them; the section below, collapsed when the page
 * opens, holds the routines to migrate yourself, each listed routine (to
 * migrate yourself, that would behave differently, or with no code collected)
 * by id, kind and status with why in plain words and the routines it names,
 * and the carried routines to check rolled up once per note (maintainer
 * decision 2 on #1938). It only formats the facts (ADR-032): every count,
 * list and sentence is stored. Never a routine's code: the facts hold none.
 *
 * The section's sub-headings are h3, under the expandable container's h2 header, so the
 * page's heading order never skips a level (axe heading-order; a regression from #2108).
 *
 * Rows are keyed on kind and id (routineKey). Callers pass `t` (the page's own
 * `useTranslation` function).
 */

function Mono({ children }) {
  return <span style={{ fontFamily: 'monospace' }}>{children}</span>;
}

function refList(refs) {
  return (refs || []).map((r, i) => (
    <span key={routineKey(r)}>
      {i > 0 && ', '}
      {r.kind} <Mono>{r.routine_id}</Mono>
    </span>
  ));
}

const RoutineMigrationSection = memo(({ synthesis, t }) => {
  const routines = routinesOf(synthesis);
  if (!routines) return null;
  const rows = (routines.routines || []).filter(r => r && typeof r === 'object' && r.listed);
  const notes = (routines.check_notes || []).filter(n => n && typeof n === 'object');
  const toDo = routineToDoText(routines);
  const toDoRoutines = routines.to_do_routines || [];
  const step = routines.migrate_yourself_step;
  const columns = [
    { id: 'routine', header: t('analysis-results.routines.column-routine'), cell: r => <Mono>{r.routine_id}</Mono> },
    { id: 'kind', header: t('analysis-results.routines.column-kind'), cell: r => r.kind },
    { id: 'status', header: t('analysis-results.routines.column-status'), cell: r => r.status_text || '-' },
    { id: 'why', header: t('analysis-results.routines.column-why'), cell: r => (r.why || []).join(' ') || '-' },
    {
      id: 'names',
      header: t('analysis-results.routines.column-names'),
      cell: r => ((r.blocked_by || []).length > 0 ? refList(r.blocked_by) : '-'),
    },
    {
      id: 'tables',
      header: t('analysis-results.routines.column-tables'),
      cell: r => ((r.tables || []).length > 0 ? (r.tables || []).map((tb, i) => (
        <span key={tb}>{i > 0 && ', '}<Mono>{tb}</Mono></span>
      )) : '-'),
    },
  ];
  const others = routines.others_line;
  return (
    <div id="routines-section">
      <SpaceBetween size="s">
        <Box data-figure="routines-headline">
          <div data-figure="routines-line">{routines.line}</div>
          {toDo && <div data-figure="routines-to-do-line">{toDo}</div>}
        </Box>
        <ExpandableSection
          variant="container"
          defaultExpanded={false}
          headerText={t('analysis-results.routines.header', { count: routines.routine_count })}
        >
          <SpaceBetween size="s">
            {toDo && toDoRoutines.length > 0 && (
              <Box data-figure="routines-to-do">
                <Box variant="h3">{t('analysis-results.routines.to-do')}</Box>
                <ul>
                  {toDoRoutines.map(r => (
                    <li key={routineKey(r)}>
                      {r.kind} <Mono>{r.routine_id}</Mono>
                    </li>
                  ))}
                </ul>
                {step && <div>{`${step.charAt(0).toUpperCase()}${step.slice(1)}.`}</div>}
              </Box>
            )}
            {rows.length > 0 && (
              <Box data-figure="routines-table">
                <Box variant="h3">{t('analysis-results.routines.each')}</Box>
                <div>{t('analysis-results.routines.intro')}</div>
                <Table
                  items={rows}
                  columnDefinitions={columns}
                  trackBy={routineKey}
                  variant="embedded"
                  wrapLines
                  ariaLabels={{ tableLabel: t('analysis-results.routines.each') }}
                />
              </Box>
            )}
            {notes.length > 0 && (
              <Box data-figure="routines-notes">
                <Box variant="h3">{t('analysis-results.routines.carried')}</Box>
                <ul>
                  {notes.map(n => (
                    <li key={`${n.note}:${n.rule_id}`} data-note={n.rule_id}>
                      {`${n.label} (${n.note_count}): `}
                      {(n.routines || []).map((r, i) => (
                        <span key={routineKey(r)}>{i > 0 && ', '}<Mono>{r.routine_id}</Mono></span>
                      ))}
                      {n.more_count > 0 && t('analysis-results.routines.more', { count: n.more_count })}
                      {'.'}
                      {n.text ? ` ${n.text}` : ''}
                    </li>
                  ))}
                </ul>
              </Box>
            )}
            {others && <Box data-figure="routines-others">{others}</Box>}
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </div>
  );
});

export default RoutineMigrationSection;
