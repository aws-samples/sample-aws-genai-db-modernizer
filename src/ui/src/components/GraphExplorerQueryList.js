// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useLayoutEffect, useRef } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { engineIcon } from '../utils/nodeIcons';
import { waveTags } from '../utils/graphExplorer';

/**
 * A wave tag: outlined rather than filled, one style for every wave tag, so it
 * never reads as an engine badge (those are filled with the engine's colour).
 * Text on the page background, so it keeps the page's own contrast.
 */
function WaveTag({ children }) {
  return (
    <span
      style={{
        display: 'inline-block',
        border: '1px solid currentColor',
        borderRadius: 4,
        padding: '0 6px',
        fontSize: 12,
        lineHeight: '18px',
        fontWeight: 700,
      }}
    >
      {children}
    </span>
  );
}

/** The engine as its AWS service icon beside its catalog name (the icon is decoration). */
export function EngineTag({ engine }) {
  const { src, label } = engineIcon(engine);
  return (
    <span
      data-testid="graph-explorer-engine-tag"
      style={{ display: 'inline-flex', alignItems: 'center', gap: 4, border: '1px solid #5f6b7a', borderRadius: 4, padding: '0 6px 0 2px', fontSize: 12, lineHeight: '18px', fontWeight: 700 }}
    >
      <img src={src} alt="" width={16} height={16} style={{ borderRadius: 2 }} />
      {label}
    </span>
  );
}

/**
 * A query's tags, always as text: the engine with its service icon, wave tags
 * outlined, the rest neutral grey.
 */
function Tags({ t, q, placeholderTables }) {
  const neutral = [
    q.access ? t(`graph-explorer.access.${q.access}`) : null,
    q.unit_size ? t('graph-explorer.tags.unit', { count: q.unit_size }) : null,
    ...(q.tables || []).map((table) => (placeholderTables.includes(table) ? t('graph-explorer.node.no-table') : table)),
  ].filter(Boolean);
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
      {q.operation && <Badge>{q.operation}</Badge>}
      {q.engine && <EngineTag engine={q.engine} />}
      {waveTags(q, t).map((tag) => (
        <WaveTag key={tag}>{tag}</WaveTag>
      ))}
      {neutral.map((tag, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <Badge key={`${tag}-${i}`}>{tag}</Badge>
      ))}
    </div>
  );
}

/**
 * The explorer's query list: one compact card per query drawn (or in the
 * selected bundle), busiest first. Hovering a card lights its edges; clicking
 * it selects the query, centres the graph on it and opens its details (the
 * Details tab). Selecting an edge in the graph scrolls its card into view. Figures are
 * the API's; customer text is rendered as text.
 */
export default function GraphExplorerQueryList({
  t,
  items,
  filter,
  onFilter,
  selectedQid,
  onHover,
  onSelect,
  bundleLabel,
  onClearBundle,
  placeholderTables = [],
  active = true,
}) {
  const cards = useRef({});
  const scroller = useRef(null);
  const scrollTop = useRef(0);
  // Coming back to the list puts it where the user left it.
  useLayoutEffect(() => {
    if (active && scroller.current) scroller.current.scrollTop = scrollTop.current;
  }, [active]);
  // Bring the selected card into view (once the list is on show).
  useEffect(() => {
    const el = selectedQid && active ? cards.current[selectedQid] : null;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selectedQid, active]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <SpaceBetween size="xs">
        <Input
          type="search"
          value={filter}
          onChange={({ detail: d }) => onFilter(d.value)}
          placeholder={t('graph-explorer.list.filter')}
          ariaLabel={t('graph-explorer.list.filter')}
        />
        {bundleLabel && (
          <Box variant="small">
            {t('graph-explorer.list.bundle', { label: bundleLabel })}{' '}
            <Button variant="inline-link" onClick={onClearBundle}>
              {t('graph-explorer.list.all')}
            </Button>
          </Box>
        )}
      </SpaceBetween>
      <div
        ref={scroller}
        onScroll={(e) => {
          scrollTop.current = e.currentTarget.scrollTop;
        }}
        data-testid="graph-explorer-query-list"
        style={{ overflowY: 'auto', flex: 1, minHeight: 0, marginTop: 8, paddingRight: 4 }}>
        {items.length === 0 && <Box color="text-body-secondary">{t('graph-explorer.list.empty')}</Box>}
        {items.map(({ query: q, bundleKey }) => {
          const selected = q.query_id === selectedQid;
          return (
            <div
              key={q.query_id}
              ref={(el) => {
                cards.current[q.query_id] = el;
              }}
              role="button"
              tabIndex={0}
              aria-pressed={selected}
              data-qid={q.query_id}
              onMouseEnter={() => onHover(q.query_id)}
              onMouseLeave={() => onHover(null)}
              onClick={() => onSelect(q, bundleKey)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelect(q, bundleKey);
                }
              }}
              style={{
                cursor: 'pointer',
                border: `${selected ? 2 : 1}px solid ${selected ? '#ff9900' : '#5f6b7a'}`,
                borderRadius: 8,
                padding: '8px 10px',
                marginBottom: 8,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' }}>
                <span style={{ fontFamily: 'monospace', fontSize: 12 }} title={q.query_id}>
                  {q.short_id}
                </span>
                <Box fontWeight="bold" textAlign="right">
                  {q.calls_per_second == null ? '-' : t('graph-explorer.hover.cps', { value: q.calls_per_second.toLocaleString() })}
                </Box>
              </div>
              {q.sql_preview && (
                <div
                  style={{
                    fontFamily: 'monospace',
                    fontSize: 12,
                    marginTop: 4,
                    display: '-webkit-box',
                    WebkitLineClamp: selected ? 'unset' : 2,
                    WebkitBoxOrient: 'vertical',
                    overflow: 'hidden',
                    wordBreak: 'break-word',
                  }}
                >
                  {q.sql_preview}
                </div>
              )}
              <Tags t={t} q={q} placeholderTables={placeholderTables} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
