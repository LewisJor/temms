"""Keep the AI-assisted codebase inside an explicit complexity budget."""

import ast
from pathlib import Path

CORE = Path("src/temms")
CORE_FILES = [
    CORE / "engine.py",
    CORE / "errors.py",
    CORE / "runtime.py",
    CORE / "selector.py",
    CORE / "types.py",
]
FORBIDDEN_IMPORTS = {
    "fastapi",
    "httpx",
    "mlflow",
    "onnxruntime",
    "pydantic",
    "prometheus_client",
    "sqlalchemy",
    "sqlite3",
    "uvicorn",
}


def test_core_stays_below_line_budget() -> None:
    line_counts = {path: len(path.read_text().splitlines()) for path in CORE_FILES}
    assert sum(line_counts.values()) <= 600, line_counts
    assert max(line_counts.values()) <= 200, line_counts


def test_core_has_no_platform_dependencies() -> None:
    imported: set[str] = set()
    for path in CORE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(FORBIDDEN_IMPORTS), imported & FORBIDDEN_IMPORTS
