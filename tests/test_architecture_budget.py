"""Keep the AI-assisted codebase inside an explicit complexity budget."""

import ast
import tomllib
from pathlib import Path

CORE = Path("src/temms")
CORE_FILES = sorted(path for path in CORE.glob("*.py") if path.name != "__init__.py")
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


def test_core_stays_below_concept_and_line_budgets() -> None:
    line_counts = {path: len(path.read_text().splitlines()) for path in CORE_FILES}
    assert len(CORE_FILES) <= 6, [path.name for path in CORE_FILES]
    assert sum(line_counts.values()) <= 600, line_counts
    assert max(line_counts.values()) <= 200, line_counts


def test_core_has_only_the_intended_protocols() -> None:
    protocols = set()
    for path in CORE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                _base_name(base) == "Protocol" for base in node.bases
            ):
                protocols.add(node.name)
    assert protocols == {"Runtime", "Selector"}


def test_core_has_no_platform_dependencies_or_subpackages() -> None:
    imported: set[str] = set()
    for path in CORE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    unexpected_dirs = [
        path.name
        for path in CORE.iterdir()
        if path.is_dir() and path.name not in {"__pycache__", "adapters"}
    ]
    assert imported.isdisjoint(FORBIDDEN_IMPORTS), imported & FORBIDDEN_IMPORTS
    assert not unexpected_dirs, unexpected_dirs


def test_project_has_zero_required_runtime_dependencies() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert project.get("dependencies", []) == []


def test_example_stays_small() -> None:
    lines = Path("examples/adaptive_vision.py").read_text().splitlines()
    assert len(lines) <= 40, len(lines)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
