#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Update legacy module-level logger assignments in the maintenance scripts."""

import ast
from pathlib import Path


def _legacy_assignment(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    return (
        node.col_offset == 0
        and isinstance(target, ast.Name)
        and target.id == "logger"
        and ast.unparse(node.value) == "config.logger"
    )


def _has_logging_import(node: ast.stmt) -> bool:
    if not isinstance(node, ast.ImportFrom) or node.module != "src.display":
        return False
    return any(name.name == "configure_logging" and name.asname is None for name in node.names)


def _rewrite(content: str) -> str:
    body = ast.parse(content).body
    assignments = [node for node in body if _legacy_assignment(node)]
    if not assignments:
        return content
    # Replace only whole assignment lines. Keep surrounding imports, comments,
    # module docstrings and future directives in their original positions.
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    for node in reversed(assignments):
        end = node.end_lineno or node.lineno
        last = lines[end - 1]
        suffix = last.encode("utf-8")[node.end_col_offset :].decode("utf-8")
        lines[node.lineno - 1 : end] = ['logger = configure_logging("INFO", None)' + suffix]
    first = assignments[0].lineno - 1
    if not any(_has_logging_import(node) for node in body if node.lineno <= first):
        lines.insert(first, "from src.display import configure_logging" + newline)
    return "".join(lines)


def fix_logger_imports():
    root = Path(__file__).parent
    excluded = {".git", "__pycache__", ".venv", "venv", ".pytest_cache"}
    fixed = []
    for path in sorted(root.rglob("*.py")):
        if excluded.intersection(path.relative_to(root).parts):
            continue
        try:
            content = path.read_bytes().decode("utf-8")
            updated = _rewrite(content)
            if updated != content:
                path.write_bytes(updated.encode("utf-8"))
                fixed.append(path)
                print(f"Fixed: {path}")
        except (OSError, UnicodeError, SyntaxError) as exc:
            print(f"Error processing {path}: {exc}")
    print(f"\nFixed {len(fixed)} files:")


if __name__ == "__main__":
    fix_logger_imports()
