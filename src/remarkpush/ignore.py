"""`.rmpushignore` — gitignore-style exclusion for folder pushes."""

from __future__ import annotations

from pathlib import Path

import pathspec

# Always excluded, even without a .rmpushignore file.
DEFAULT_PATTERNS = [
    ".DS_Store",
    ".git/",
    ".remarkpush/",
    "*.part",
]

IGNORE_FILENAME = ".rmpushignore"


def load_ignore(root: Path) -> pathspec.PathSpec:
    """Build a matcher from DEFAULT_PATTERNS plus ``root/.rmpushignore`` if present."""
    patterns = list(DEFAULT_PATTERNS)
    ignore_file = root / IGNORE_FILENAME
    if ignore_file.exists():
        patterns += ignore_file.read_text(encoding="utf-8").splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def is_ignored(spec: pathspec.PathSpec, rel_path: str) -> bool:
    return spec.match_file(rel_path)
