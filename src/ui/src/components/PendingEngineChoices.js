// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useState } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import {
  buildChoiceRequest,
  choiceTableName,
  formatChoiceScore,
  switchDecisionOverrides,
} from "../utils/engineChoices";

/**
 * #1003: the assignment review's engine choices, one per table. Each pending
 * choice shows its queries, every tied engine's scores and trade-off, why they
 * tie and what "decide for me" would pick; the customer picks one engine per
 * table (it applies to every query listed). Choices the decide-for-me rule settled are marked
 * "decided automatically, review if needed" and can be switched. The server
 * builds every figure here; this only formats it and sends the picks.
 */
const PendingEngineChoices = memo(({
  choices = [],
  decided = [],
  engineLabel,
  onSubmit,
  saving = false,
  t,
}) => {
  const [selections, setSelections] = useState({});
  const [switches, setSwitches] = useState({});
  if (!choices.length && !decided.length) return null;
  const label = (e) => (engineLabel ? engineLabel(e) : e);
  const picked = Object.keys(selections).length;
  const switched = Object.values(switches).filter(Boolean).length;

  const submitPicks = (decideForMe) => {
    const body = buildChoiceRequest(selections, choices, decideForMe);
    const overrides = decided.flatMap((d) => switchDecisionOverrides(d, switches[d.choice_id]));
    onSubmit({ ...body, overrides });
  };

  return (
    <div id="pending-engine-choices">
      <SpaceBetween size="l">
        {choices.length > 0 && (
          <Container
            header={
              <Header
                variant="h2"
                description={t('assignment-gate.choices.description')}
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button onClick={() => submitPicks(true)} loading={saving}>
                      {t('assignment-gate.choices.decide-for-me')}
                    </Button>
                    <Button variant="primary" disabled={!picked} onClick={() => submitPicks(false)} loading={saving}>
                      {t('assignment-gate.choices.save', { count: picked })}
                    </Button>
                  </SpaceBetween>
                }
              >
                {t('assignment-gate.choices.title', { count: choices.length })}
              </Header>
            }
          >
            <SpaceBetween size="m">
              {choices.map((c) => {
                const options = (c.tied_engines || []).map((e) => ({ label: label(e), value: e }));
                const value = selections[c.choice_id];
                return (
                  <div key={c.choice_id} className="engine-choice" data-choice={c.choice_id}>
                    <SpaceBetween size="xs">
                      <Box variant="h3">
                        {t('assignment-gate.choices.table-heading', {
                          table: choiceTableName(c),
                          count: (c.query_ids || []).length,
                        })}
                      </Box>
                      <Box color="text-body-secondary">{c.why_tied}</Box>
                      <ul className="engine-choice__engines">
                        {(c.options || []).map((e) => (
                          <li key={e.engine}>
                            <strong>{label(e.engine)}</strong>: {formatChoiceScore(e, t)}. {e.trade_off}
                          </li>
                        ))}
                      </ul>
                      <Box fontSize="body-s">
                        {t('assignment-gate.choices.decide-for-me-picks', {
                          engine: label(c.decide_for_me_engine),
                          reason: c.decide_for_me_reason,
                        })}
                      </Box>
                      <Select
                        ariaLabel={t('assignment-gate.choices.select-label', { table: choiceTableName(c) })}
                        selectedOption={value ? { label: label(value), value } : null}
                        onChange={({ detail }) =>
                          setSelections((prev) => ({ ...prev, [c.choice_id]: detail.selectedOption.value }))
                        }
                        options={options}
                        placeholder={t('assignment-gate.choices.placeholder', {
                          engine: (c.current_engines && c.current_engines.length ? c.current_engines : [c.current_engine])
                            .map(label)
                            .join(', '),
                        })}
                      />
                      <ExpandableSection headerText={t('assignment-gate.choices.queries', { count: (c.query_ids || []).length })}>
                        <Box fontSize="body-s"><code>{(c.query_ids || []).join(', ')}</code></Box>
                      </ExpandableSection>
                    </SpaceBetween>
                  </div>
                );
              })}
            </SpaceBetween>
          </Container>
        )}
        {decided.length > 0 && (
          <Container
            header={
              <Header
                variant="h2"
                description={t('assignment-gate.choices.decided-description')}
                actions={
                  <Button disabled={!switched} onClick={() => submitPicks(false)} loading={saving}>
                    {t('assignment-gate.choices.save-switches', { count: switched })}
                  </Button>
                }
              >
                {t('assignment-gate.choices.decided-title', { count: decided.length })}
              </Header>
            }
          >
            <SpaceBetween size="m">
              {decided.map((d) => {
                const engines = [d.engine, ...(d.alternatives || [])];
                const value = switches[d.choice_id] || d.engine;
                return (
                  <div key={d.choice_id} className="engine-choice engine-choice--decided" data-decided={d.choice_id}>
                    <SpaceBetween size="xs">
                      <Box variant="h3">
                        {t('assignment-gate.choices.table-heading', {
                          table: choiceTableName(d),
                          count: (d.query_ids || []).length,
                        })}{' '}
                        <Badge color="blue">{t('assignment-gate.choices.decided-badge')}</Badge>
                      </Box>
                      <Box color="text-body-secondary">{d.reason}</Box>
                      <Select
                        ariaLabel={t('assignment-gate.choices.select-label', { table: choiceTableName(d) })}
                        selectedOption={{ label: label(value), value }}
                        onChange={({ detail }) =>
                          setSwitches((prev) => ({
                            ...prev,
                            [d.choice_id]: detail.selectedOption.value === d.engine ? undefined : detail.selectedOption.value,
                          }))
                        }
                        options={engines.map((e) => ({ label: label(e), value: e }))}
                      />
                    </SpaceBetween>
                  </div>
                );
              })}
            </SpaceBetween>
          </Container>
        )}
      </SpaceBetween>
    </div>
  );
});

export default PendingEngineChoices;
