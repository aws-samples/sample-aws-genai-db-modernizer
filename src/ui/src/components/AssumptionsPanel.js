// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useCallback, useEffect, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ApiManager from '../classes/ApiManager';

/**
 * #1991: the results page's assumptions panel. Each assumption a customer can set
 * (growth and the peak) with the value and origin the job's report states, as
 * GET /assessments/{job_id}/assumptions returns them: the report's own line, its range
 * (the assumptions file's, src/shared/assumptions.py) and whether this server can make
 * the change. A change is POSTed in the assumptions file's own form ({"growth": 3});
 * the server checks it with the file's parser, records it with origin `ui` and re-runs
 * only the phases it affects. Nothing is checked or computed here: a refusal shows the
 * server's own reason, and `onChanged` reloads the results.
 */
const AssumptionsPanel = memo(({ jobId, t, onChanged }) => {
  const [rows, setRows] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState(null);
  const [message, setMessage] = useState(null);

  const load = useCallback(async () => {
    const api = new ApiManager();
    const res = (await api.execute([
      { id: 'assumptions', path: `assessments/${jobId}/assumptions`, method: 'GET', params: {} },
    ]))['assumptions'];
    if (res?.success) setRows(res.assumptions || []);
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
        setMessage({ type: 'error', text: res?.error?.message || t('analysis-results.assumptions-panel.failed'), notes: [] });
      }
    } finally {
      setSaving(null);
    }
  }, [drafts, jobId, load, onChanged, t]);

  if (!rows.length) return null;
  return (
    <Container
      id="assumptions-panel"
      header={
        <Header variant="h2" description={t('analysis-results.assumptions-panel.description')}>
          {t('analysis-results.assumptions-panel.title')}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {rows.map(row => (
          <div key={row.name} data-assumption={row.name}>
            <SpaceBetween size="xs">
              <Box variant="h3">{t(`analysis-results.assumptions-panel.name.${row.name}`)}</Box>
              {row.line && <Box data-figure="assumption-line">{row.line}.</Box>}
              {row.available ? (
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
              ) : (
                <Box color="text-status-warning" data-figure="assumption-unavailable">{row.unavailable_reason}</Box>
              )}
              <Box fontSize="body-s" color="text-body-secondary" data-figure="assumption-range">
                {t(row.min_included
                  ? 'analysis-results.assumptions-panel.range-included'
                  : 'analysis-results.assumptions-panel.range-excluded', { min: row.min, max: row.max })}
                {' '}
                {t('analysis-results.assumptions-panel.reruns', {
                  phases: (row.reruns || []).map(p => t(`analysis-results.assumptions-panel.phase.${p}`)).join(', '),
                })}
              </Box>
              {row.available && (row.notes || []).map(note => (
                <Box key={note} fontSize="body-s" color="text-body-secondary">{note}</Box>
              ))}
            </SpaceBetween>
          </div>
        ))}
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
