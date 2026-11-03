// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from 'react';
import Button from '@cloudscape-design/components/button';
import { IconLegend, Legend, LinkLegend } from './GraphExplorerViewBar';

/** The legend as a small collapsible card in the graph's corner, with how much the view draws. */
export default function GraphExplorerLegendCard({ t, mode, traffic, cost, status, engines, placeholder, wave = null, result = false }) {
  const [open, setOpen] = useState(true);
  return (
    <div
      data-testid="graph-explorer-legend"
      style={{
        position: 'absolute',
        right: 10,
        bottom: 10,
        zIndex: 3,
        maxWidth: 'min(460px, 34%)',
        background: 'rgba(15, 20, 26, 0.9)',
        border: '1px solid #414d5c',
        borderRadius: 8,
        padding: '4px 8px',
        fontSize: 12,
      }}
    >
      <Button
        variant="inline-link"
        iconName={open ? 'caret-down-filled' : 'caret-right-filled'}
        ariaExpanded={open}
        onClick={() => setOpen(!open)}
      >
        {t('graph-explorer.legend.title')}
      </Button>
      {status && (
        <span data-testid="graph-explorer-status" style={{ marginLeft: 8, color: '#8d99a8' }}>
          {status}
        </span>
      )}
      {open && (
        <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <IconLegend t={t} engines={engines} placeholder={placeholder} />
          <LinkLegend t={t} wave={wave} result={result} />
          <Legend mode={mode} traffic={traffic} cost={cost} t={t} />
        </div>
      )}
    </div>
  );
}
