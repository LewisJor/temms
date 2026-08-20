#!/usr/bin/env python3
"""Fail if a module defines the same top-level name twice.

Python resolves a redefinition by source order, so the later definition silently
wins. That is invisible to tests when both happen to behave alike, and a live
bug when they do not: hub_lite carried two `_record_timestamp` implementations
checking different key sets, and every caller silently got the narrower one.

Ruff's F811 does not reliably catch this across long files, so this runs as its
own gate.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

ROOTS = (Path("src"), Path("scripts"))


def duplicates(path: Path) -> dict[str, int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    names = collections.Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    return {name: count for name, count in names.items() if count > 1}


def main() -> int:
    failures = 0
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            for name, count in duplicates(path).items():
                print(f"{path}: '{name}' defined {count} times (the last one silently wins)")
                failures += 1
    if failures:
        print(f"\n{failures} duplicate top-level definition(s).")
        return 1
    print("no duplicate top-level definitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
