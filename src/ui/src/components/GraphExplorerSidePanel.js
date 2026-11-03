// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useRef } from 'react';
import Button from '@cloudscape-design/components/button';
import Tabs from '@cloudscape-design/components/tabs';
import { PANEL_TABS, clampWidth } from '../utils/explorerLayout';

const RAIL_WIDTH = 44;
const KEY_STEP = 24;
const TAB_ICONS = { queries: 'list-view', walkthrough: 'map', details: 'status-info', questions: 'search' };

/**
 * The workspace's docked left panel: tabs for Queries, Walkthrough, Details
 * and Questions. Resizable by dragging (or with the arrow keys on) its edge, and
 * collapsible to a narrow rail. Every tab stays mounted (hidden when not on
 * show); the query list keeps its own scroll position across tab switches.
 */
export default function GraphExplorerSidePanel({ t, width, collapsed, onToggle, onResize, tab, onTab, panels }) {
  const drag = useRef(null);

  if (collapsed) {
    return (
      <nav
        data-testid="graph-explorer-side-panel"
        aria-label={t('graph-explorer.panel.label')}
        style={{ width: RAIL_WIDTH, flex: `0 0 ${RAIL_WIDTH}px`, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, paddingTop: 8, borderRight: '1px solid #414d5c' }}
      >
        <Button
          variant="icon"
          iconName="angle-right-double"
          ariaLabel={t('graph-explorer.panel.expand')}
          ariaExpanded={false}
          onClick={onToggle}
        />
        {PANEL_TABS.map((id) => (
          <Button
            key={id}
            variant="icon"
            iconName={TAB_ICONS[id]}
            ariaLabel={t(`graph-explorer.panel.tab.${id}`)}
            onClick={() => {
              onTab(id);
              onToggle();
            }}
          />
        ))}
      </nav>
    );
  }

  const onPointerDown = (e) => {
    e.preventDefault();
    drag.current = { x: e.clientX, width };
    const move = (ev) => onResize(clampWidth(drag.current.width + ev.clientX - drag.current.x));
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      drag.current = null;
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

  return (
    <section
      data-testid="graph-explorer-side-panel"
      aria-label={t('graph-explorer.panel.label')}
      style={{ width, flex: `0 0 ${width}px`, position: 'relative', display: 'flex', flexDirection: 'column', minHeight: 0, borderRight: '1px solid #414d5c' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, paddingRight: 4 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Tabs
            activeTabId={tab}
            onChange={({ detail }) => onTab(detail.activeTabId)}
            tabs={PANEL_TABS.map((id) => ({ id, label: t(`graph-explorer.panel.tab.${id}`) }))}
            ariaLabel={t('graph-explorer.panel.label')}
          />
        </div>
        <Button
          variant="icon"
          iconName="angle-left-double"
          ariaLabel={t('graph-explorer.panel.collapse')}
          ariaExpanded
          onClick={onToggle}
        />
      </div>
      <div style={{ position: 'relative', flex: 1, minHeight: 0 }}>
        {PANEL_TABS.map((id) => (
          <div
            key={id}
            role="tabpanel"
            data-testid={`graph-explorer-panel-${id}`}
            aria-hidden={id !== tab}
            style={{
              position: 'absolute',
              inset: 0,
              overflowY: id === 'queries' ? 'hidden' : 'auto',
              padding: '8px 12px 12px 12px',
              display: id === tab ? 'flex' : 'none',
              flexDirection: 'column',
            }}
          >
            {panels[id]}
          </div>
        ))}
      </div>
      {/* The edge: drag it, or focus it and use the arrow keys, to resize the panel. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={t('graph-explorer.panel.resize')}
        aria-valuenow={width}
        tabIndex={0}
        data-testid="graph-explorer-panel-resize"
        onPointerDown={onPointerDown}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft') onResize(clampWidth(width - KEY_STEP));
          if (e.key === 'ArrowRight') onResize(clampWidth(width + KEY_STEP));
        }}
        className="graph-explorer-resize"
        style={{ position: 'absolute', top: 0, right: -4, width: 8, height: '100%', cursor: 'col-resize', zIndex: 3 }}
      />
    </section>
  );
}
