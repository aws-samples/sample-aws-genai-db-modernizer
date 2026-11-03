// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Autosuggest from '@cloudscape-design/components/autosuggest';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import Table from '@cloudscape-design/components/table';
import ApiManager from '../classes/ApiManager';
import {
  catalogErrorKey,
  catalogPath,
  catalogRunPath,
  cellText,
  RESULT_START_LIMIT,
  groupEntries,
  hasStarts,
  resultPlan,
} from '../utils/graphExplorer';

async function call(request) {
  const results = await new ApiManager().execute([request]);
  return results[request.id];
}

/**
 * The explorer's Questions tab (#308): the graph query catalog's fixed
 * questions (#1789). A question's parameters are picked from the values the
 * job's graph holds (typed, when the API listed only some of them; the API
 * checks a typed value). Run asks the API; the rows show as a table and the
 * result's projection can be drawn on the graph. There is no Cypher here:
 * every question is a fixed, read-only statement on the server.
 *
 * Every figure (the row count, the bound, the result's values) is the API's;
 * customer text renders as text.
 */
export default function GraphExplorerQuestions({ t, jobId, active, onShowOnGraph }) {
  const [catalog, setCatalog] = useState({ entries: null, maxChoices: null, loading: false, error: null });
  const [filter, setFilter] = useState('');
  const [entry, setEntry] = useState(null);
  const [values, setValues] = useState({});
  const [run, setRun] = useState({ result: null, loading: false, error: null });
  const [shown, setShown] = useState(null);

  const live = useRef(true);
  useEffect(() => () => {
    live.current = false;
  }, []);
  // Bumped whenever the question or a value changes, and by each run: an
  // answer that comes back for an older one is dropped.
  const seq = useRef(0);

  // Read the catalog the first time the tab is opened (its choices are graph reads).
  const requested = useRef(false);
  const load = useCallback(() => {
    requested.current = true;
    setCatalog({ entries: null, maxChoices: null, loading: true, error: null });
    call({ id: 'catalog', path: catalogPath(jobId), method: 'GET', params: {} }).then((result) => {
      if (!live.current) return;
      if (result && result.success) {
        setCatalog({ entries: result.entries || [], maxChoices: result.max_choices ?? null, loading: false, error: null });
      } else setCatalog({ entries: null, maxChoices: null, loading: false, error: catalogErrorKey(result) });
    });
  }, [jobId]);
  useEffect(() => {
    if (active && !requested.current) load();
  }, [active, load]);

  const pick = (next) => {
    seq.current += 1;
    setEntry(next);
    setValues({});
    setRun({ result: null, loading: false, error: null });
    setShown(null);
  };
  const setValue = (name, value) => {
    seq.current += 1;
    setValues((v) => ({ ...v, [name]: value }));
    setRun({ result: null, loading: false, error: null });
    setShown(null);
  };

  const ready = entry && entry.params.every((p) => (values[p.name] || '').trim() !== '');
  const onRun = async () => {
    if (!ready) return;
    seq.current += 1;
    const mine = seq.current;
    setRun({ result: null, loading: true, error: null });
    setShown(null);
    const params = Object.fromEntries(entry.params.map((p) => [p.name, values[p.name].trim()]));
    const result = await call({ id: 'catalog-run', path: catalogRunPath(jobId, entry.id), method: 'POST', params: { params } });
    if (!live.current || mine !== seq.current) return;
    if (result && result.success) setRun({ result, loading: false, error: null });
    else setRun({ result: null, loading: false, error: catalogErrorKey(result) });
  };

  const result = run.result;
  const plan = result ? resultPlan(result.projection) : null;
  const onShow = async () => {
    const mine = seq.current;
    setShown({ pending: true });
    const out = (await onShowOnGraph(result)) || {};
    if (!live.current || mine !== seq.current) return;
    // Superseded on the page (Explore, a tour step, Clear graph): not pending any more, nothing to say.
    setShown(out.stale ? null : out);
  };
  const kindLabel = (label) => t(`graph-explorer.questions.kind.${label}`, { defaultValue: label });

  if (catalog.error) {
    return (
      <Alert
        type="error"
        action={<Button onClick={load}>{t('graph-explorer.questions.retry')}</Button>}
      >
        {t(catalog.error)}
      </Alert>
    );
  }
  if (!catalog.entries) {
    return (
      <Box textAlign="center" padding="m">
        <Spinner /> {t('graph-explorer.questions.loading')}
      </Box>
    );
  }

  if (!entry) {
    const groups = groupEntries(catalog.entries, filter);
    return (
      <SpaceBetween size="s">
        <Box variant="small">{t('graph-explorer.questions.intro')}</Box>
        <Input
          type="search"
          value={filter}
          onChange={({ detail }) => setFilter(detail.value)}
          placeholder={t('graph-explorer.questions.filter')}
          ariaLabel={t('graph-explorer.questions.filter')}
        />
        {groups.length === 0 && <Box color="text-status-inactive">{t('graph-explorer.questions.none')}</Box>}
        {groups.map((group) => (
          <section key={group.section} data-testid={`questions-section-${group.section}`}>
            <Box variant="h4">{t(`graph-explorer.questions.section.${group.section}`)}</Box>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {group.entries.map((e) => (
                <li key={e.id} style={{ margin: '4px 0' }}>
                  <Button variant="inline-link" onClick={() => pick(e)}>
                    {e.question}
                  </Button>
                  <Box variant="small" display="block" color="text-body-secondary">
                    {e.title}
                  </Box>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="s">
      <Button variant="inline-link" iconName="arrow-left" onClick={() => pick(null)}>
        {t('graph-explorer.questions.back')}
      </Button>
      <Box variant="h4">{entry.question}</Box>
      <Box variant="small" color="text-body-secondary">
        {entry.title}
      </Box>
      {entry.note && <Box variant="small">{entry.note}</Box>}
      {entry.params.map((p) => (
        <FormField
          key={p.name}
          label={p.name}
          description={p.description}
          constraintText={p.choices_truncated ? t('graph-explorer.questions.choices-truncated', { count: catalog.maxChoices }) : undefined}
        >
          {p.choices_truncated ? (
            <Autosuggest
              value={values[p.name] || ''}
              onChange={({ detail }) => setValue(p.name, detail.value)}
              options={p.choices.map((c) => ({ value: c }))}
              enteredTextLabel={(value) => t('graph-explorer.questions.use-value', { value })}
              placeholder={p.example}
              ariaLabel={p.name}
              empty={t('common.labels.no-data')}
            />
          ) : (
            <Select
              selectedOption={values[p.name] ? { value: values[p.name], label: values[p.name] } : null}
              onChange={({ detail }) => setValue(p.name, detail.selectedOption.value)}
              options={p.choices.map((c) => ({ value: c, label: c }))}
              filteringType="auto"
              placeholder={p.example}
              ariaLabel={p.name}
              empty={t('graph-explorer.questions.no-choices')}
            />
          )}
        </FormField>
      ))}
      <Button variant="primary" disabled={!ready} loading={run.loading} onClick={onRun}>
        {t('graph-explorer.questions.run')}
      </Button>
      {run.error && <Alert type="error">{t(run.error)}</Alert>}
      {result && (
        <SpaceBetween size="xs">
          <Box variant="small">
            {t('graph-explorer.questions.rows', { count: result.row_count })}
            {result.truncated && ` ${t('graph-explorer.questions.truncated', { max: result.max_rows })}`}
          </Box>
          {/* The catalog's own wording, which names each stage the job lacks once. */}
          {result.row_count === 0 && result.explanation && <Alert type="info">{result.explanation}</Alert>}
          <Button iconName="share" disabled={!hasStarts(plan)} loading={Boolean(shown && shown.pending)} onClick={onShow}>
            {t('graph-explorer.questions.show-on-graph')}
          </Button>
          {shown && shown.failed && <Alert type="error">{t('graph-explorer.questions.show-failed')}</Alert>}
          {shown && shown.drawn && (
            <SpaceBetween size="xxs">
              {shown.partial && <Box variant="small">{t('graph-explorer.questions.show-partial')}</Box>}
              {plan.undrawn.length > 0 && (
                <Box variant="small">{t('graph-explorer.questions.undrawn', { kinds: plan.undrawn.map(kindLabel).join(', ') })}</Box>
              )}
              {shown.more && <Box variant="small">{t('graph-explorer.questions.more-starts', { limit: RESULT_START_LIMIT })}</Box>}
              {shown.capped && <Box variant="small">{t('graph-explorer.graph.capped', { max: shown.max })}</Box>}
            </SpaceBetween>
          )}
          {result.row_count > 0 && (
            <Table
              variant="embedded"
              items={result.rows}
              columnDefinitions={result.columns.map((c) => ({
                id: c,
                header: c,
                // Long ids (query hashes) keep one line per row: the table scrolls
                // sideways, a cut-off value ends in an ellipsis and its title shows it whole.
                minWidth: 160,
                cell: (row) => <span title={cellText(row[c])}>{cellText(row[c])}</span>,
              }))}
              empty={t('graph-explorer.questions.no-rows')}
            />
          )}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
