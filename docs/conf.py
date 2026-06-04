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
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_title = "TEMMS"
html_baseurl = "https://lewisjor.github.io/temms/"

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "substitution",
]
