import Badge from '@cloudscape-design/components/badge';
import { engineBadgeStyle } from '../utils/engineColors';
import { displayEngine } from '../utils/engineNames';

/**
 * An engine's badge in the engine catalog's colour (#1246): every page that names an
 * engine in a badge uses this, so the cost cards, the roadmap, the assignment gate and
 * the pattern analysis page show each engine in the colour of its Sankey node and of
 * the executive report. The text is the catalog display name unless `children` is given.
 */
export default function EngineBadge({ engine, children }) {
  return (
    <span data-engine-badge={engine}>
      <Badge style={engineBadgeStyle(engine)}>{children ?? displayEngine(engine)}</Badge>
    </span>
  );
}
