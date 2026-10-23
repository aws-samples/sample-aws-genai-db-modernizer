#!/usr/bin/env python3
"""
Shared transform engine (initial-load path). Mirrors tools/go-sink/transform.go.

Processing order per row: filter (on original fields) -> computed -> column
selection -> rename. Both engines must pass data-plane/transforms-fixtures.json.

apply(spec, table, row) -> (out_fields: dict|None, keep: bool)
  keep == False means the row must not be cached (loader skips it; the sink
  DELETEs the key). With no spec/table entry it's a passthrough (keep all).
"""
import ast
import json
import operator

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def load_spec(path_or_bytes):
    if path_or_bytes is None:
        return None
    if isinstance(path_or_bytes, (bytes, str)) and str(path_or_bytes).strip().startswith("{"):
        return json.loads(path_or_bytes)
    with open(path_or_bytes) as f:
        return json.load(f)


def _to_float(s):
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return 0.0


def _format_num(v):
    # %.6f then strip trailing zeros and a trailing dot (matches Go).
    s = f"{v:.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _eval_expr(expr, row):
    """Safe arithmetic over numeric row fields; identifiers -> float(field) or 0."""
    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value) if isinstance(node.value, (int, float)) else 0.0
        if isinstance(node, ast.Name):
            return _to_float(row.get(node.id, 0))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            r = _ev(node.right)
            if isinstance(node.op, ast.Div) and r == 0:
                return 0.0
            return _OPS[type(node.op)](_ev(node.left), r)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.operand))
        return 0.0
    try:
        return _ev(ast.parse(expr, mode="eval"))
    except Exception:
        return 0.0


def _eval_filter(f, row):
    got = row.get(f["field"], "")
    op, val = f["op"], f["value"]
    if op == "=":
        return str(got) == val
    if op == "!=":
        return str(got) != val
    if op == ">":
        return _to_float(got) > _to_float(val)
    if op == ">=":
        return _to_float(got) >= _to_float(val)
    if op == "<":
        return _to_float(got) < _to_float(val)
    if op == "<=":
        return _to_float(got) <= _to_float(val)
    return True


def apply(spec, table, row):
    """row: dict[str,str]. Returns (out_fields|None, keep)."""
    if not spec:
        return dict(row), True
    ts = spec.get("tables", {}).get(table)
    if not ts:
        return dict(row), True

    if ts.get("filter") and not _eval_filter(ts["filter"], row):
        return None, False

    computed = {}
    for cf in ts.get("computed", []):
        computed[cf["name"]] = _format_num(_eval_expr(cf["expr"], row))

    cols = ts.get("columns")
    if cols:
        out = {c: row[c] for c in cols if c in row}
    else:
        out = dict(row)
    out.update(computed)

    rename = ts.get("rename")
    if rename:
        out = {rename.get(k, k): v for k, v in out.items()}
    return out, True
