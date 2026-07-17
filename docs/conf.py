project = "TEMMS"
author = "TEMMS contributors"
copyright = "2026, TEMMS contributors"

extensions = [
    "myst_parser",
]

source_suffix = {
    ".md": "markdown",
}

master_doc = "index"
# reliability-report.md is a machine-generated artifact (make soak); it is
# linked from reliability.md rather than being a narrative page in the toctree.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "reliability-report.md"]

html_theme = "furo"
html_title = "TEMMS"
html_baseurl = "https://lewisjor.github.io/temms/"

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "substitution",
]
