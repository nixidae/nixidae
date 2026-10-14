from __future__ import annotations

project = "pydd"
copyright = "2026, Carl Andersson"
author = "Carl Andersson"

extensions = [
    "myst_parser",
    "sphinx.ext.graphviz",
]

graphviz_output_format = "svg"

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

exclude_patterns = ["_build", "_generated", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_title = "pydd"

smartquotes = False
