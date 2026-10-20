import { useMemo, useState, useEffect } from 'react';
import { sankey, sankeyCenter, sankeyLinkHorizontal } from 'd3-sankey';
import { displayEngine } from '../utils/engineNames';
import { ENGINE_HEX } from '../utils/engineCatalog.generated';
import { isCacheEngine } from '../utils/cacheLayer';
import { DEFAULT_ENGINE_HEX } from '../utils/engineColors';

const MARGIN_Y = 25;
const MARGIN_X = 5;
const NODE_WIDTH = 26;
const LABEL_GAP = 6;
const LABEL_FONT_SIZE = 12;

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
// ids missed this map and fell back to a default blue that read as DynamoDB's #3184e8;
// since #1246 Aurora MySQL (#9c5700) and Aurora PostgreSQL (#c33d69) differ too.
export const NODE_COLORS = {
  patterns: '#5f6b7a',      // Gray for source
  ...ENGINE_HEX,
};

const ChartSankey = ({ width = 800, height = 400, data, onNodeClick }) => {
  // Theme detection for text color
  const [isDark, setIsDark] = useState(() => document.body.classList.contains('awsui-dark-mode'));
  useEffect(() => {
    const check = () => setIsDark(document.body.classList.contains('awsui-dark-mode'));
    window.addEventListener('dbm-theme-change', check);
    return () => window.removeEventListener('dbm-theme-change', check);
  }, []);

  const textFill = isDark ? '#ffffff' : '#16191f';

  // Compute nodes and links positions
  const { nodes, links } = useMemo(() => {
    // Set the sankey diagram properties
    const sankeyGenerator = sankey()
      .nodeWidth(NODE_WIDTH)
      .nodePadding(29)
      .extent([
        [MARGIN_X, MARGIN_Y],
        [width - MARGIN_X - cacheLabelRoom(data), height - MARGIN_Y],
      ])
      .nodeId((node) => node.id) // Accessor function: how to retrieve the id that defines each node
      .nodeAlign(sankeyCenter); // Algorithm used to decide node position

    // Compute nodes and links positions
    return sankeyGenerator(data);
  }, [data, width, height]);

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
      const labelX = labelOutside ? node.x1 + LABEL_GAP : node.x0 - LABEL_GAP;
      const labelAnchor = labelOutside ? 'start' : 'end';

      return (
        <g
          key={node.index}
          data-sankey-node={node.id}
          onClick={isClickable ? () => onNodeClick(node.id) : undefined}
          style={{ cursor: isClickable ? 'pointer' : 'default', pointerEvents: 'all' }}
        >
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
            y={(node.y1 + node.y0) / 2}
            dy="0.35em"
            textAnchor={labelAnchor}
            fontSize={LABEL_FONT_SIZE}
            fill={textFill}
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
              fill={textFill}
              opacity={0.65}
            >
              cache layer
            </text>
          )}
        </g>
      );
    });
  }, [nodes, width, nodeValues, onNodeClick, textFill]);

  // Draw the links
  const allLinks = useMemo(() => {
    const linkGenerator = sankeyLinkHorizontal();

    return links.map((link, i) => {
      const path = linkGenerator(link);
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const linkColor = NODE_COLORS[targetId] || DEFAULT_ENGINE_HEX;
      // #330: a link into the cache layer is a cached-reads overlay on top of
      // its owner engine's flow, not a flow from the query source -- dash it
      // so it reads as a second, overlaid layer rather than another owner flow.
      const isCacheLink = isCacheEngine(targetId);

      return (
        <path
          key={i}
          d={path}
          stroke={linkColor}
          fill="none"
          strokeOpacity={isDark ? 0.3 : 0.4}
          strokeWidth={link.width}
          strokeDasharray={isCacheLink ? '6,4' : undefined}
        />
      );
    });
  }, [links, isDark]);

  return (
    <div style={{ width: '100%', display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
      <svg width={width} height={height}>
        {allLinks}
        {allNodes}
      </svg>
    </div>
  );
};

export default ChartSankey;
