# Lucide icons

Generic node-type icons of the experimental graph explorer (#308), from Lucide
(ISC licence, see `LICENSE` in this directory).

- Source: the `lucide-static` npm package, version 1.66.0, retrieved 2026-11-02
- Change: the stroke colour is set (`#d1d5db`, and `#7d8998` for the muted
  table) instead of `currentColor`, because the graph draws each icon as an
  image, which cannot inherit a colour from the page

| File | Lucide icon | Node type |
|---|---|---|
| `database.svg` | `database` | the source database, and an engine with no icon of its own |
| `table.svg` | `table` | a source table |
| `table-muted.svg` | `table` | the "No table" placeholder |
| `square-function.svg` | `square-function` | stored procedure (reserved for later) |
| `zap.svg` | `zap` | trigger (reserved for later) |
| `file-code.svg` | `file-code` | source-code class or module (reserved for later) |
