"""Build the public site directly from the maintained repository documents."""

import tomllib
from pathlib import Path

project = "TEMMS"
copyright = "2026, TEMMS Contributors"
release = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
)["project"]["version"]

extensions = ["myst_parser"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
root_doc = "index"
include_patterns = [
    "index.rst",
    "README.md",
    "CONTRIBUTING.md",
    "NEXT_STEPS.md",
    "AGENTS.md",
    "docs/architecture.md",
    "docs/progress.md",
    "examples/live_swap/README.md",
    "examples/live_swap/COMPARISON.md",
]
myst_heading_anchors = 6

html_theme = "furo"
html_title = "TEMMS"
html_baseurl = "https://lewisjor.github.io/temms/"
