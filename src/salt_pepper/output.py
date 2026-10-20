"""
Output formatters for salt-pepper results.

Supports JSON, YAML, table (box-drawn borders), text (space-aligned,
no decorations), and CSV.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def format_result(
    result: Any,
    fmt: str = "json",
    sort_keys: bool = True,
    indent: int = 2,
) -> str:
    """
    Format a salt-api result as a string.

    Args:
        result: The parsed JSON response (dict or list).
        fmt: Output format (``json``, ``yaml``, ``table``, ``text``, ``csv``).
        sort_keys: Whether to sort dict keys (JSON only).
        indent: Indentation level (JSON and YAML).

    Returns:
        Formatted string.

    Raises:
        ValueError: If the format is unknown.
    """
    if fmt == "json":
        return _format_json(result, sort_keys=sort_keys, indent=indent)
    elif fmt == "yaml":
        return _format_yaml(result, indent=indent)
    elif fmt == "table":
        return _format_table(result)
    elif fmt == "text":
        return _format_text(result)
    elif fmt == "csv":
        return _format_csv(result)
    else:
        raise ValueError(f"Unknown output format: {fmt!r}")


def _format_json(result: Any, sort_keys: bool = True, indent: int = 2) -> str:
    return json.dumps(result, sort_keys=sort_keys, indent=indent, default=str)


def _format_yaml(result: Any, indent: int = 2) -> str:
    import yaml

    return yaml.dump(
        result,
        default_flow_style=False,
        indent=indent,
        sort_keys=False,
        allow_unicode=True,
    )


# ── Helpers shared by tabular formats ────────────────────────────────


def _unwrap(data: Any) -> Any:
    """Strip common salt-api response wrappers (return, data)."""
    if isinstance(data, dict):
        if "return" in data:
            data = data["return"]
        if "data" in data:
            data = data["data"]
    return data


def _flatten_mapping(data: Any) -> Any:
    """Convert a name→value mapping into a list of row dicts.

    Salt-api returns minion results as ``[{{name: result, …}}]`` — a list
    containing one dict whose keys are minion IDs.  This helper detects that
    shape and restructures it into tabular rows.

    Examples
    --------
    ``[{{"m1": True, "m2": True}}]``
        → ``[{{"minion": "m1", "return": True}}, {{"minion": "m2", "return": True}}]``

    ``{{"m1": {{"id": 1, "up": True}}, "m2": {{"id": 2, "up": False}}}}``
        → ``[{{"minion": "m1", "id": 1, "up": True}}, …]``
    """
    raw = _unwrap(data)

    # Unwrap single-element list: [{"m1": ..., "m2": ...}] → {"m1": ..., "m2": ...}
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        raw = raw[0]

    if not isinstance(raw, dict):
        return raw  # not a mapping — pass through unchanged

    # Check whether values look like minion results (all same type)
    values = list(raw.values())
    if not values:
        return raw

    # Dict of scalars → two-column table
    if not any(isinstance(v, dict) for v in values):
        return [
            {"minion": k, "return": v}
            for k, v in raw.items()
        ]

    # Dict of dicts → merge key into minion column
    if all(isinstance(v, dict) for v in values):
        return [
            {"minion": k, **v}
            for k, v in raw.items()
        ]

    # Mixed types — pass through
    return raw


def _build_tabular(data: Any) -> list[dict[str, Any]]:
    """Normalise *data* into a list of flat dicts for tabular rendering.

    Passes through lists of plain dicts unchanged.  Detects and restructures
    name→value mappings (the common salt-api minion-result shape).
    """
    rows = _flatten_mapping(data)

    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows

    # Fallback: wrap single item
    if not isinstance(rows, list):
        rows = [rows]

    # Convert non-dict items
    result: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            result.append(item)
        else:
            result.append({"value": item})
    return result


def _collect_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Collect column names from a list of dicts, preserving insertion order."""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            sk = str(key)
            if sk not in seen:
                columns.append(sk)
                seen.add(sk)
    return columns


def _stringify_rows(
    rows: list[dict[str, Any]], columns: list[str]
) -> tuple[list[list[str]], list[int]]:
    """Convert rows to stringified form and compute column widths."""
    str_rows: list[list[str]] = []
    col_widths = [len(c) for c in columns]

    for row in rows:
        srow: list[str] = []
        for col in columns:
            val = row.get(col, "")
            if isinstance(val, (dict, list)):
                val = json.dumps(val, default=str)
            srow.append(str(val))
            col_widths[len(srow) - 1] = max(
                col_widths[len(srow) - 1], len(str(val))
            )
        str_rows.append(srow)

    return str_rows, col_widths


# ── Table format (box-drawn borders) ─────────────────────────────────


def _format_table(result: Any) -> str:
    """Render result as a column-aligned table with box-drawn borders."""
    rows = _build_tabular(result)
    return _render_table(rows)


def _render_table(rows: list[dict[str, Any]]) -> str:
    """Render a list of dicts as a box-drawn table."""
    if not rows:
        return "(empty)"

    columns = _collect_columns(rows)
    if not columns:
        return "(empty)"

    str_rows, col_widths = _stringify_rows(rows, columns)
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

    lines: list[str] = [sep]
    # Header
    header = "| " + " | ".join(
        c.ljust(col_widths[i]) for i, c in enumerate(columns)
    ) + " |"
    lines.append(header)
    lines.append(sep.replace("-", "="))

    # Data rows
    for row_data in str_rows:
        row_str = "| " + " | ".join(
            row_data[i].ljust(col_widths[i]) for i in range(len(columns))
        ) + " |"
        lines.append(row_str)

    lines.append(sep)
    return "\n".join(lines)


# ── Text format (space-aligned, no decorations) ──────────────────────


def _format_text(result: Any) -> str:
    """Render result as a space-aligned text table with no border or
    separator characters."""
    rows = _build_tabular(result)
    return _render_text(rows)


def _render_text(rows: list[dict[str, Any]]) -> str:
    """Render a list of dicts as space-aligned columns with no decorations."""
    if not rows:
        return "(empty)"

    columns = _collect_columns(rows)
    if not columns:
        return "(empty)"

    str_rows, col_widths = _stringify_rows(rows, columns)

    lines: list[str] = []
    # Header — no separator line, just column names
    header = "  ".join(
        c.ljust(col_widths[i]) for i, c in enumerate(columns)
    )
    lines.append(header)

    # Data rows
    for row_data in str_rows:
        row_str = "  ".join(
            row_data[i].ljust(col_widths[i]) for i in range(len(columns))
        )
        lines.append(row_str)

    return "\n".join(lines)


# ── CSV format ───────────────────────────────────────────────────────


def _format_csv(result: Any) -> str:
    """Render result as CSV with a header row."""
    rows = _build_tabular(result)
    return _render_csv(rows)


def _render_csv(rows: list[dict[str, Any]]) -> str:
    """Render a list of dicts as CSV."""
    if not rows:
        return "(empty)"

    columns = _collect_columns(rows)
    if not columns:
        return "(empty)"

    buf = io.StringIO()
    writer = csv.writer(buf)

    # Header
    writer.writerow(columns)

    # Data rows
    for row in rows:
        writer.writerow([
            _csv_cell(row.get(col, ""))
            for col in columns
        ])

    return buf.getvalue().rstrip("\r\n")


def _csv_cell(val: Any) -> str:
    """Convert a cell value to a CSV-safe string."""
    if isinstance(val, (dict, list)):
        return json.dumps(val, default=str)
    return str(val)
