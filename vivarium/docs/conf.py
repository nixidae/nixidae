from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nix" / "shared"))

from sphinx_shared import (
    author as author,
    exclude_patterns as shared_exclude_patterns,
    extensions as extensions,
    html_theme as html_theme,
    intersphinx_mapping as intersphinx_mapping,
    myst_enable_extensions as myst_enable_extensions,
    myst_heading_anchors as myst_heading_anchors,
    smartquotes as smartquotes,
    templates_path as templates_path,
)

# A draft whose lines carry the number of their review round. It reads as a
# file, not as a page.
exclude_patterns = [*shared_exclude_patterns, "design/history"]

project = "vivarium"
copyright = "2026, Carl Andersson"  # noqa: A001 -- the name Sphinx reads
