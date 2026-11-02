// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ButtonGroup from '@cloudscape-design/components/button-group';
import Input from '@cloudscape-design/components/input';
import Popover from '@cloudscape-design/components/popover';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Select from '@cloudscape-design/components/select';
import Spinner from '@cloudscape-design/components/spinner';

const card = {
  background: 'rgba(15, 20, 26, 0.92)',
  border: '1px solid #414d5c',
  borderRadius: 10,
  boxShadow: '0 2px 10px rgba(0,0,0,0.35)',
  padding: '6px 8px',
};

/**
 * The toolbar floating over the top of the graph: where to start, the view,
 * the query search, the Filters popover (a count shows how many filters are
 * on) and the graph actions as icon buttons with tooltips.
 */
export default function GraphExplorerToolbar({
  t,
  kinds,
  startKind,
  onStartKind,
  choices,
  startOption,
  onStartOption,
  optionsReady,
  choicesLimit,
  onExplore,
  exploring,
  views,
  mode,
  onMode,
  searchText,
  onSearchText,
  search,
  searching,
  onPickSearch,
  filters,
  activeFilters,
  actions,
  onAction,
}) {
  return (
    <div
      data-testid="graph-explorer-toolbar"
      role="toolbar"
      aria-label={t('graph-explorer.toolbar.label')}
      style={{ position: 'absolute', top: 10, left: 10, right: 10, zIndex: 4, display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', pointerEvents: 'none' }}
    >
      <div style={{ ...card, display: 'flex', gap: 6, alignItems: 'center', pointerEvents: 'auto' }}>
        <SegmentedControl
          label={t('graph-explorer.picker.kind')}
          selectedId={startKind}
          onChange={({ detail }) => onStartKind(detail.selectedId)}
          options={kinds.map((kind) => ({ id: kind, text: t(`graph-explorer.start.${kind}`) }))}
        />
        <div style={{ width: 240 }}>
          <Select
            selectedOption={startOption}
            onChange={({ detail }) => onStartOption(detail.selectedOption)}
            options={choices}
            filteringType="auto"
            statusType={optionsReady ? 'finished' : 'loading'}
            finishedText={choicesLimit ? t('graph-explorer.picker.truncated', { limit: choicesLimit }) : undefined}
            loadingText={t('common.labels.loading')}
            placeholder={t(`graph-explorer.start.${startKind}`)}
            empty={t('common.labels.no-data')}
            ariaLabel={t(`graph-explorer.start.${startKind}`)}
          />
        </div>
        <Button variant="primary" disabled={!startOption} loading={exploring} onClick={onExplore}>
          {t('graph-explorer.actions.explore')}
        </Button>
      </div>
      <div style={{ ...card, pointerEvents: 'auto' }}>
        <SegmentedControl
          label={t('graph-explorer.views.label')}
          selectedId={mode}
          onChange={({ detail }) => onMode(detail.selectedId)}
          options={views.map((v) => ({ id: v, text: t(`graph-explorer.views.${v}`) }))}
        />
      </div>
      <div style={{ ...card, position: 'relative', width: 280, pointerEvents: 'auto' }}>
        <Input
          type="search"
          value={searchText}
          onChange={({ detail }) => onSearchText(detail.value)}
          placeholder={t('graph-explorer.search.placeholder')}
          ariaLabel={t('graph-explorer.search.label')}
        />
        {(searching || search) && (
          <div
            data-testid="graph-explorer-search-results"
            style={{ ...card, position: 'absolute', top: '100%', left: 0, right: 0, marginTop: 4, maxHeight: 360, overflowY: 'auto' }}
          >
            {searching && <Spinner />}
            {search && !searching && search.results.length === 0 && <Box color="text-body-secondary">{t('graph-explorer.search.none')}</Box>}
            {search &&
              !searching &&
              search.results.map((q) => (
                <div key={q.query_id} style={{ marginBottom: 6 }}>
                  <Button variant="inline-link" onClick={() => onPickSearch(q)}>
                    {q.label}
                  </Button>
                  <Box variant="small" display="block" color="text-body-secondary">
                    {[q.engine_label, q.sql_preview].filter(Boolean).join(' · ')}
                  </Box>
                </div>
              ))}
            {search && !searching && search.truncated && <Box variant="small">{t('graph-explorer.search.truncated')}</Box>}
          </div>
        )}
      </div>
      <div style={{ ...card, display: 'flex', gap: 4, alignItems: 'center', pointerEvents: 'auto' }}>
        <Popover
          triggerType="custom"
          position="bottom"
          size="large"
          fixedWidth
          header={t('graph-explorer.filters.title')}
          content={filters}
          dismissAriaLabel={t('common.actions.close')}
        >
          <Button iconName="filter" ariaLabel={t('graph-explorer.filters.open', { count: activeFilters })}>
            {t('graph-explorer.filters.button')}
            {activeFilters > 0 && (
              <span data-testid="graph-explorer-filters-active" style={{ marginLeft: 6 }}>
                {t('graph-explorer.filters.active', { count: activeFilters })}
              </span>
            )}
          </Button>
        </Popover>
        <ButtonGroup variant="icon" ariaLabel={t('graph-explorer.toolbar.actions')} items={actions} onItemClick={({ detail }) => onAction(detail.id)} />
      </div>
    </div>
  );
}
