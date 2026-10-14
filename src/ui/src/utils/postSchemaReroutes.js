/**
 * #629: rows the post-schema router moved after the customer approved the
 * assignment. The assignment gate marks them, distinct from a reality-check
 * move, from the structured `post_schema_reroutes` list (and, for an older
 * artifact, the router's own `post-schema routing:` reason prefix).
 */
export const POST_SCHEMA_REASON_PREFIX = 'post-schema routing:';

export function reroutedQueryIds(assignment) {
  const rows = (assignment && assignment.post_schema_reroutes) || [];
  return new Set(rows.filter((r) => r && r.query_id).map((r) => r.query_id));
}

// 'rerouted' (moved after approval by the post-schema router), 'moved' (by the
// reality check), or null.
export function rowMoveKind(item, rerouted) {
  if (!item) return null;
  const reason = item.assignment_reason || '';
  if ((rerouted && rerouted.has(item.query_id)) || reason.startsWith(POST_SCHEMA_REASON_PREFIX)) {
    return 'rerouted';
  }
  if (reason.includes('reality check')) return 'moved';
  return null;
}
