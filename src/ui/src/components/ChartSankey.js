// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useMemo, useRef, useState, useEffect } from 'react';
import { sankey, sankeyCenter, sankeyLinkHorizontal } from 'd3-sankey';
import { displayEngine } from '../utils/engineNames';
import { ENGINE_HEX } from '../utils/engineCatalog.generated';
import { isCacheEngine } from '../utils/cacheLayer';
import { DEFAULT_ENGINE_HEX } from '../utils/engineColors';
import { colorTextBodyDefault } from '@cloudscape-design/design-tokens';
import { isDarkMode, onThemeChange } from '../utils/theme';

const MARGIN_Y = 25;
const MARGIN_X = 5;
const NODE_WIDTH = 26;
const LABEL_GAP = 6;
const LABEL_FONT_SIZE = 12;
const CACHE_EDGE_WIDTH = 1.5;
// #2317: the diagram is laid out at the width its container has, but never narrower
// than this: below it the labels would run into the nodes. A narrower container scales
// the whole drawing down through its viewBox instead, so it never scrolls the page.
const MIN_LAYOUT_WIDTH = 560;
// Under this layout width the source label sits above its node, clear of the owner labels.
const COMPACT_LAYOUT_WIDTH = 720;

// #1245: the cache layer is the diagram's last column, at the right edge, and its
// label used to sit left of the node, on top of the dashed overlay flows converging
// into it (and, with a small node, across the node itself). It now sits right of the
// node, so the layout keeps that much room free on the right: a generous width
// estimate for the longest cache label (about 0.6 em per character), since an SVG
// text's real width is only known after it is drawn.
export const estimateLabelWidth = (text, fontSize = LABEL_FONT_SIZE) => Math.ceil(String(text).length * fontSize * 0.6);

const cacheLabelRoom = (data) => {
  const cacheIds = (data?.nodes || []).map((n) => n.id).filter(isCacheEngine);
  if (cacheIds.length === 0) return 0;
  const widest = Math.max(...cacheIds.map((id) => {
    const total = (data.links || [])
      .filter((l) => (typeof l.target === 'object' ? l.target.id : l.target) === id)
      .reduce((sum, l) => sum + (l.value || 0), 0);
    return estimateLabelWidth(`${displayEngine(id)} (${total})`);
  }));
  return LABEL_GAP + widest;
};

// Node colours: the source node's grey, then every engine's live UI colour from the
// engine catalog (colors.ui, src/catalog/engines, #492). Before #476 the Aurora node
// ids missed this map and fell back to a default blue that read as DynamoDB's colour;
// since #1246 Aurora MySQL and Aurora PostgreSQL differ too.
// Labels in the theme's body text colour (#2311). A design token is a CSS variable, which
// an SVG fill attribute cannot read, so it is set as a style.
const LABEL_STYLE = { fill: colorTextBodyDefault };

export const NODE_COLORS = {
  patterns: DEFAULT_ENGINE_HEX, // the neutral grey for the source
  ...ENGINE_HEX,
};

// `width` is the layout width used until the container's own width is known (and where
// there is no layout, as in a test). `ariaLabel` names the drawing as one image for a
// screen reader (#2317); QueryFlow gives the same numbers as a table under it, so the
// nodes here stay a mouse shortcut and the table holds the keyboard-reachable links.
const ChartSankey = ({ width = 800, height = 400, data, onNodeClick, unavailableTitle, ariaLabel }) => {
  const containerRef = useRef(null);
  const [measured, setMeasured] = useState(0);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;
    const read = () => setMeasured(Math.floor(el.clientWidth));
    read();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const layoutWidth = Math.max(measured > 0 ? measured : width, MIN_LAYOUT_WIDTH);
  const compact = layoutWidth < COMPACT_LAYOUT_WIDTH;

  // The theme, for the links' opacity (a fainter link reads better on the dark page).
  const [isDark, setIsDark] = useState(isDarkMode);
  useEffect(() => onThemeChange(() => setIsDark(isDarkMode())), []);

  // Compute nodes and links positions
  const { nodes, links } = useMemo(() => {
    // Set the sankey diagram properties
    const sankeyGenerator = sankey()
      .nodeWidth(NODE_WIDTH)
      .nodePadding(29)
      .extent([
        [MARGIN_X, MARGIN_Y],
        [layoutWidth - MARGIN_X - cacheLabelRoom(data), height - MARGIN_Y],
      ])
      .nodeId((node) => node.id) // Accessor function: how to retrieve the id that defines each node
      .nodeAlign(sankeyCenter); // Algorithm used to decide node position

    // Compute nodes and links positions
    return sankeyGenerator(data);
  }, [data, layoutWidth, height]);

  // Calculate total values for each node. Before #330 every node was either
  // a pure source (the "queries"/"patterns" root) or a pure target (an owner
  // engine), so summing a node's value across every link touching it and
  // taking the max of its incoming/outgoing totals gave the same number. Now
  // an owner engine can be *both* -- a target of the "queries -> owner" flow
  // and a source of the "owner -> cache" overlay flow (addCacheOverlayNode in
  // src/ui/src/utils/cacheLayer.js) -- so summing both directions would double
  // count it (e.g. 11 owned + 11 also cached showing as "22" instead of 11).
  // max(incoming, outgoing) keeps every owner's displayed total equal to the
  // query count it owns, cache overlay or not.
  const nodeValues = useMemo(() => {
    const incoming = {};
    const outgoing = {};

    links.forEach((link) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;

      outgoing[sourceId] = (outgoing[sourceId] || 0) + link.value;
      incoming[targetId] = (incoming[targetId] || 0) + link.value;
    });

    const values = {};
    new Set([...Object.keys(incoming), ...Object.keys(outgoing)]).forEach((id) => {
      values[id] = Math.max(incoming[id] || 0, outgoing[id] || 0);
    });

    return values;
  }, [links]);

  // Draw the nodes
  const allNodes = useMemo(() => {
    return nodes.map((node) => {
      const nodeValue = nodeValues[node.id] || 0;
      const label = `${displayEngine(node.id)} (${nodeValue})`;
      // The root ("queries"/"patterns") is always depth 0. x0 < width / 2 was
      // an equivalent stand-in for "is the root" while the diagram only ever
      // had two columns; #330 adds a third column (the cache node), whose x0
      // can also land left of the midpoint on a narrow chart, so depth is the
      // only check that stays correct regardless of column count.
      const isSource = node.depth === 0;
      // #330: the cache layer is fed by its owner engines' flows, never a flow
      // source itself -- it's never clickable as a target the way an owner
      // engine is, and its node/label are styled to read as an overlay (not a
      // peer owner) via the dashed outline and secondary caption below.
      const isCache = isCacheEngine(node.id);
      const isClickable = onNodeClick && !isSource && !isCache;
      const nodeColor = NODE_COLORS[node.id] || DEFAULT_ENGINE_HEX;
      // The source and the cache layer (the first and last columns) are labelled on
      // their outer side, an owner engine on its left (#1245).
      const labelOutside = isSource || isCache;
      // A narrow drawing puts the source label above its node: beside it, it would run
      // into the owner labels.
      const labelAbove = isSource && compact;
      const labelX = labelAbove ? node.x0 : labelOutside ? node.x1 + LABEL_GAP : node.x0 - LABEL_GAP;
      const labelAnchor = labelOutside ? 'start' : 'end';
      const labelY = labelAbove ? node.y0 - LABEL_GAP * 1.5 : (node.y1 + node.y0) / 2;

      return (
        <g
          key={node.index}
          data-sankey-node={node.id}
          onClick={isClickable ? () => onNodeClick(node.id) : undefined}
          style={{ cursor: isClickable ? 'pointer' : 'default', pointerEvents: 'all' }}
        >
          {/* #2300: why an engine node is not a link (no results page yet). */}
          {!isClickable && !isSource && !isCache && unavailableTitle && <title>{unavailableTitle}</title>}
          <rect
            height={node.y1 - node.y0}
            width={NODE_WIDTH}
            x={node.x0}
            y={node.y0}
            stroke={nodeColor}
            fill={nodeColor}
            fillOpacity={isClickable ? 0.8 : 0.6}
            strokeDasharray={isCache ? '3,2' : undefined}
            rx={0.9}
          />
          <text
            x={labelX}
            y={labelY}
            dy={labelAbove ? undefined : '0.35em'}
            textAnchor={labelAnchor}
            fontSize={LABEL_FONT_SIZE}
            style={LABEL_STYLE}
          >
            {label}
          </text>
          {isCache && (
            <text
              x={labelX}
              y={(node.y1 + node.y0) / 2}
              dy="2em"
              textAnchor={labelAnchor}
              fontSize={10}
              style={LABEL_STYLE}
              opacity={0.65}
            >
              cache layer
            </text>
          )}
        </g>
      );
    });
    // d3-sankey lays out in place, so `nodes` keeps its identity when the width changes:
    // the width is a dependency of its own (#2317).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, layoutWidth, compact, nodeValues, onNodeClick, unavailableTitle]);

  // Draw the links
  const allLinks = useMemo(() => {
    const linkGenerator = sankeyLinkHorizontal();

    return links.map((link, i) => {
      const path = linkGenerator(link);
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const linkColor = NODE_COLORS[targetId] || DEFAULT_ENGINE_HEX;
      // #330: a link into the cache layer is a cached-reads overlay on top of
      // its owner engine's flow, not a flow from the query source. #2317: it is drawn
      // as the two thin dashed edges of its band, so it reads as a second layer and
      // never outweighs the owner flows it crosses.
      if (isCacheEngine(targetId)) {
        const half = link.width / 2;
        return [-half, half].map((offset) => (
          <path
            key={`${i}-${offset}`}
            d={linkGenerator({ ...link, y0: link.y0 + offset, y1: link.y1 + offset })}
            stroke={linkColor}
            fill="none"
            strokeOpacity={0.9}
            strokeWidth={CACHE_EDGE_WIDTH}
            strokeDasharray="6,4"
          />
        ));
      }

      return (
        <path
          key={i}
          d={path}
          stroke={linkColor}
          fill="none"
          strokeOpacity={isDark ? 0.3 : 0.4}
          strokeWidth={link.width}
        />
      );
    });
    // The same in-place layout as above: a new width moves the links too.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [links, layoutWidth, isDark]);

  return (
    <div ref={containerRef} style={{ width: '100%', minWidth: 0 }}>
      <svg
        viewBox={`0 0 ${layoutWidth} ${height}`}
        width="100%"
        style={{ display: 'block', height: 'auto', maxWidth: '100%' }}
        role={ariaLabel ? 'img' : undefined}
        aria-label={ariaLabel || undefined}
      >
        {allLinks}
        {allNodes}
      </svg>
    </div>
  );
};

export default ChartSankey;
