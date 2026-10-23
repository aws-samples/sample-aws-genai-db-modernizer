import React from 'react';

// A backtick span in model or report prose (#1321): synthesis writes a query
// cited by id as its label in a code span ("query `8c66eaee`"), and the
// engineering report renders it as inline code; the UI does the same here, so
// the deliverables agree. The text is untrusted: it is split into plain
// strings and <code> elements, which React escapes, never parsed as HTML.
const CODE_SPAN = /`([^`\n]+)`/;

// The React nodes for ``text``: plain strings, with each `...` span as <code>.
export function proseNodes(text) {
  if (text === null || text === undefined) return [];
  const parts = String(text).split(CODE_SPAN);
  // split with one capture group: even indices are prose, odd ones span content.
  return parts
    .map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : part))
    .filter((node) => node !== '');
}

// ``paragraphs`` (#1403): one <p> per line of ``text``, for prose written as
// paragraphs (the executive summary's recommendation, risk and decision).
export default function ProseText({ text, paragraphs = false }) {
  if (!paragraphs) return <>{proseNodes(text)}</>;
  const parts = String(text ?? '').split(/\n+/).map((p) => p.trim()).filter(Boolean);
  return <>{parts.map((p, i) => <p key={i}>{proseNodes(p)}</p>)}</>;
}
