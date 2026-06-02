import { useMemo, useState, useEffect } from 'react';
import { sankey, sankeyCenter, sankeyLinkHorizontal } from 'd3-sankey';

const MARGIN_Y = 25;
const MARGIN_X = 5;

// Color palette for different nodes
const NODE_COLORS = {
  patterns: '#5f6b7a',      // Gray for source
  dynamodb: '#3184e8',      // Blue
  documentdb: '#1d8102',    // Green
  elasticache: '#d13212',   // Red
  opensearch: '#2ea597',    // Teal
  neptune: '#7d2105',       // Dark red
  keyspaces: '#8b6ccb',     // Purple
  aurora: '#ec7211'         // Orange
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
      .nodeWidth(26)
      .nodePadding(29)
      .extent([
        [MARGIN_X, MARGIN_Y],
        [width - MARGIN_X, height - MARGIN_Y],
      ])
      .nodeId((node) => node.id) // Accessor function: how to retrieve the id that defines each node
      .nodeAlign(sankeyCenter); // Algorithm used to decide node position

    // Compute nodes and links positions
    return sankeyGenerator(data);
  }, [data, width, height]);

  // Calculate total values for each node
  const nodeValues = useMemo(() => {
    const values = {};

    // Calculate totals from links
    links.forEach((link) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;

      values[sourceId] = (values[sourceId] || 0) + link.value;
      values[targetId] = (values[targetId] || 0) + link.value;
    });

    return values;
  }, [links]);

  // Draw the nodes
  const allNodes = useMemo(() => {
    return nodes.map((node) => {
      const nodeValue = nodeValues[node.id] || 0;
      const label = `${node.id} (${nodeValue})`;
      const isSource = node.x0 < width / 2;
      const isClickable = onNodeClick && !isSource;
      const nodeColor = NODE_COLORS[node.id] || '#0972D3';

      return (
        <g
          key={node.index}
          onClick={isClickable ? () => onNodeClick(node.id) : undefined}
          style={{ cursor: isClickable ? 'pointer' : 'default', pointerEvents: 'all' }}
        >
          <rect
            height={node.y1 - node.y0}
            width={26}
            x={node.x0}
            y={node.y0}
            stroke={nodeColor}
            fill={nodeColor}
            fillOpacity={isClickable ? 0.8 : 0.6}
            rx={0.9}
          />
          <text
            x={node.x0 < width / 2 ? node.x1 + 6 : node.x0 - 6}
            y={(node.y1 + node.y0) / 2}
            dy="0.35em"
            textAnchor={node.x0 < width / 2 ? 'start' : 'end'}
            fontSize={12}
            fill={textFill}
          >
            {label}
          </text>
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
      const linkColor = NODE_COLORS[targetId] || '#0972D3';

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
