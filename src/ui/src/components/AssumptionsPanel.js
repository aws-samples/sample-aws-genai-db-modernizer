// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useCallback, useEffect, useState } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ApiManager from '../classes/ApiManager';
import RequestError from './RequestError';
import AssumptionsTable from './AssumptionsTable';
import { failureMessageKey, failureReason, reportFailure } from '../utils/requestFailure';

/**
 * #1991: the results page's assumptions panel. Each assumption a customer can set
 * (growth and the peak) with the value and origin the job's report states, as
 * GET /assessments/{job_id}/assumptions returns them: the report's own line, its range
 * (the assumptions file's, src/shared/assumptions.py) and whether this server can make
 * the change. A change is POSTed in the assumptions file's own form ({"growth": 3});
 * the server checks it with the file's parser, records it with origin `ui` and re-runs
 * only the phases it affects. Nothing is checked or computed here: a refusal shows the
 * server's own reason, and `onChanged` reloads the results. #2316: the assumptions are a
 * table (AssumptionsTable); when this server can change none of them they are read-only
 * under one note in plain words for the kind of refusal the server names (`refusal_kind`);
 * the server's own reason text (which names internals) is not shown. The report's items
 * are the rows, so they show even when this request fails; the request only adds edits.
 */
const FIXED_KINDS = ['no_results', 'not_finished', 'needs_model_designs', 'needs_model_summary'];

const AssumptionsPanel = memo(({ jobId, t, onChanged, items }) => {
  const [rows, setRows] = useState([]);
  const [failure, setFailure] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState(null);
  const [message, setMessage] = useState(null);

  const load = useCallback(async () => {
    const api = new ApiManager();
    const res = (await api.execute([
      { id: 'assumptions', path: `assessments/${jobId}/assumptions`, method: 'GET', params: {} },
    ]))['assumptions'];
    if (res?.success) {
      setRows(res.assumptions || []);
      setFailure(null);
    } else {
      // The fixed message for the kind of failure; the table still shows the report's values.
      setFailure(reportFailure(res, 'assumptions'));
    }
  }, [jobId]);

  useEffect(() => { if (jobId) load(); }, [jobId, load]);

  const apply = useCallback(async (name) => {
    const draft = drafts[name];
    if (draft === undefined || draft === '') return;
    setSaving(name);
    setMessage(null);
    try {
      const api = new ApiManager();
      // The text as typed, as a number; the server checks it (the assumptions file's rules).
      const res = (await api.execute([
        {
          id: 'change',
          path: `assessments/${jobId}/assumptions`,
          method: 'POST',
          params: { [name]: Number(draft) },
        },
      ]))['change'];
      if (res?.success) {
        setMessage({
          type: 'success',
          text: t('analysis-results.assumptions-panel.applied', {
            phases: (res.reran || []).map(p => t(`analysis-results.assumptions-panel.phase.${p}`)).join(', '),
          }),
          notes: res.notes || [],
        });
        setDrafts(prev => ({ ...prev, [name]: undefined }));
        await load();
        if (onChanged) onChanged();
      } else {
        setMessage({ type: 'error', text: failureReason(res, t) || `${t('analysis-results.assumptions-panel.failed')} ${t(failureMessageKey(reportFailure(res, 'assumptions'), 'form'))}`, notes: [] });
      }
    } finally {
      setSaving(null);
    }
  }, [drafts, jobId, load, onChanged, t]);

  const byName = Object.fromEntries(rows.map(row => [row.name, row]));
  // The report's own items; else the panel's rows (a report written before the block).
  const shown = items && items.length
    ? items
    : rows.map(row => ({ key: row.name, setting: row.value, origin: row.origin || 'default', line: row.line || '' }));
  if (!shown.length && !failure) return null;
  const refused = rows.filter(row => !row.available);
  const readOnly = rows.length > 0 && refused.length === rows.length;
  const kindOf = (row) => (FIXED_KINDS.includes(row.refusal_kind) ? row.refusal_kind : 'other');
  // One note for the whole table: the kind that stops the page first, else the shared kind.
  const kinds = [...new Set(refused.map(kindOf))];
  const fixedKind = ['no_results', 'not_finished'].find(k => kinds.includes(k))
    || (kinds.length === 1 ? kinds[0] : 'needs_model_summary');

  // #2316: the control under a value, only where this server can make the change.
  const renderEdit = (item) => {
    const row = byName[item.key];
    if (!row) return null;
    if (!row.available) {
      // A note on the row only when other rows can change; else the one note says it.
      return readOnly ? null : (
        <Box fontSize="body-s" color="text-body-secondary" data-figure="assumption-refused">
          {t(`analysis-results.assumptions-panel.refused.${kindOf(row)}`)}
        </Box>
      );
    }
    return (
      <SpaceBetween size="xs">
        <SpaceBetween direction="horizontal" size="xs">
          <Input
            type="number"
            inputMode="decimal"
            ariaLabel={t(`analysis-results.assumptions-panel.name.${row.name}`)}
            value={drafts[row.name] ?? ''}
            placeholder={row.value === null || row.value === undefined ? '' : String(row.value)}
            onChange={({ detail }) => setDrafts(prev => ({ ...prev, [row.name]: detail.value }))}
          />
          <Button
            onClick={() => apply(row.name)}
            loading={saving === row.name}
            disabled={saving !== null || drafts[row.name] === undefined || drafts[row.name] === ''}
          >
            {t('analysis-results.assumptions-panel.apply')}
          </Button>
        </SpaceBetween>
        <Box fontSize="body-s" color="text-body-secondary" data-figure="assumption-range">
          {t(row.min_included
            ? 'analysis-results.assumptions-panel.range-included'
            : 'analysis-results.assumptions-panel.range-excluded', { min: row.min, max: row.max })}
          {' '}
          {t('analysis-results.assumptions-panel.reruns', {
            phases: (row.reruns || []).map(p => t(`analysis-results.assumptions-panel.phase.${p}`)).join(', '),
          })}
        </Box>
        {(row.notes || []).map(note => (
          <Box key={note} fontSize="body-s" color="text-body-secondary">{note}</Box>
        ))}
      </SpaceBetween>
    );
  };

  return (
    <Container
      id="assumptions-panel"
      header={
        <Header variant="h2" description={t(readOnly ? 'analysis-results.assumptions-panel.description-fixed' : 'analysis-results.assumptions-panel.description')}>
          {t('analysis-results.assumptions-panel.title')}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {readOnly && (
          <Alert type="info" data-figure="assumptions-fixed">
            {t(`analysis-results.assumptions-panel.fixed.${fixedKind}`)}
          </Alert>
        )}
        {failure && (
          <div data-figure="assumptions-load-failed">
            <RequestError failure={failure} lead={t('analysis-results.assumptions-panel.load-failed')} onRetry={load} t={t} />
          </div>
        )}
        {shown.length > 0 && <AssumptionsTable items={shown} t={t} renderEdit={renderEdit} />}
        {message && (
          <div data-figure="assumption-message">
            <Box color={message.type === 'error' ? 'text-status-error' : 'text-status-success'}>{message.text}</Box>
            {message.notes.map(note => <Box key={note} fontSize="body-s">{note}</Box>)}
          </div>
        )}
      </SpaceBetween>
    </Container>
  );
});

export default AssumptionsPanel;
