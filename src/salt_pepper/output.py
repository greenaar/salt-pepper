"""
Output formatters for salt-pepper results.

Supports JSON, YAML, and a simple table format for list-style returns.
"""

from __future__ import annotations

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
        fmt: Output format (``json``, ``yaml``, ``table``).
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


def _format_table(result: Any) -> str:
    """
    Render result as a simple aligned table.

    Best-effort: only renders list-of-dict or dict-of-dict
    structures. Falls back to YAML for anything else.
    """
    # Unwrap common salt-api response wrappers
    data = result
    if isinstance(data, dict):
        if "return" in data:
            data = data["return"]
        if "data" in data:
            data = data["data"]

    # List of dicts → table
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _dict_list_table(data)

    # Dict of dicts (minion → result) → table
    if isinstance(data, dict):
        values = list(data.values())
        if values and isinstance(values[0], dict):
            return _dict_list_table(
                [{**v, "_key": k} for k, v in data.items()]
            )

    # Fallback
    return _format_yaml(result)


def _dict_list_table(rows: list[dict[str, Any]]) -> str:
    """Render a list of dicts as a column-aligned table."""
    if not rows:
        return "(empty)"

    # Collect columns in insertion order
    columns: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(str(key))
                seen.add(key)

    if not columns:
        return "(empty)"

    # Build stringified rows
    str_rows: list[list[str]] = []
    for row in rows:
        srow: list[str] = [str(row.get(col, "")) for col in columns]
        str_rows.append(srow)

    # Compute column widths
    col_widths = [len(c) for c in columns]
    for srow in str_rows:
        for i, val in enumerate(srow):
            col_widths[i] = max(col_widths[i], len(val))

    # Build separator
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

    lines = [sep]
    # Header
    header = "| " + " | ".join(
        c.ljust(col_widths[i]) for i, c in enumerate(columns)
    ) + " |"
    lines.append(header)
    lines.append(sep.replace("-", "="))

    # Rows
    for row_data in str_rows:
        row_str: str = "| " + " | ".join(
            row_data[i].ljust(col_widths[i]) for i in range(len(columns))
        ) + " |"
        lines.append(row_str)

    lines.append(sep)
    return "\n".join(lines)
