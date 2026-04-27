"""File operation tools — read, write, edit."""

from __future__ import annotations

import os
from typing import Any


def run_read_file(input_data: dict[str, Any]) -> dict[str, Any]:
    """Read a text file from the workspace."""
    path = input_data.get("path", "")
    if not path:
        return {"error": "missing required field 'path'"}

    offset = input_data.get("offset", 0)
    limit = input_data.get("limit", None)

    # Expand ~ and make relative paths relative to cwd
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)

    if not os.path.isfile(path):
        return {"error": f"file not found: {path}"}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total_lines = len(all_lines)

        # 1-indexed offset like the Rust version
        start = max(0, offset - 1) if offset > 0 else 0
        end = start + limit if limit else total_lines
        selected = all_lines[start:end]

        # Format with line numbers (cat -n style)
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:>6}\t{line.rstrip()}")

        content = "\n".join(numbered)

        return {
            "path": path,
            "content": content,
            "total_lines": total_lines,
            "shown_lines": f"{start + 1}-{min(end, total_lines)}",
        }
    except Exception as e:
        return {"error": str(e)}


def run_write_file(input_data: dict[str, Any]) -> dict[str, Any]:
    """Write a text file in the workspace."""
    path = input_data.get("path", "")
    content = input_data.get("content", "")

    if not path:
        return {"error": "missing required field 'path'"}

    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
            "status": "ok",
        }
    except Exception as e:
        return {"error": str(e)}


def run_edit_file(input_data: dict[str, Any]) -> dict[str, Any]:
    """Replace text in a workspace file."""
    path = input_data.get("path", "")
    old_string = input_data.get("old_string", "")
    new_string = input_data.get("new_string", "")
    replace_all = input_data.get("replace_all", False)

    if not path:
        return {"error": "missing required field 'path'"}
    if not old_string:
        return {"error": "missing required field 'old_string'"}

    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)

    if not os.path.isfile(path):
        return {"error": f"file not found: {path}"}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        count = content.count(old_string)
        if count == 0:
            return {"error": f"old_string not found in {path}"}
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"old_string appears {count} times in {path}. "
                    f"Use replace_all=true to replace all occurrences."
                )
            }

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return {
            "path": path,
            "replacements": count if replace_all else 1,
            "status": "ok",
        }
    except Exception as e:
        return {"error": str(e)}
