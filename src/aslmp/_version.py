"""The single source of truth for the package version.

`pyproject.toml` reads it from here via hatchling's `[tool.hatch.version]`, so the
literal exists once. Layer 0: stdlib only, no imports at all.
"""

from __future__ import annotations

__version__ = "0.1.0"
