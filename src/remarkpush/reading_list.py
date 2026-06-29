"""Parse an Obsidian reading-list checklist and resolve its wikilinks to PDFs.

These are pure helpers (no SSH, no device state) so they unit-test in isolation.
A reading-list line looks like ``- [ ] [[mask rcnn.pdf]]`` (unread) or
``- [x] [[learning without forgetting.pdf]]`` (read). The bare ``name.pdf``
inside the wikilink is resolved — case-insensitively — against a directory of
source PDFs (typically the vault's ``papers and figures`` iCloud symlink).

The reMarkable side (folder placement, move-vs-upload) lives in ``cli.py`` and
``device.py``; this module only turns markdown + a papers directory into a list
of ``(checked, Path)`` facts.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# A task line: optional indent, a `-`/`*` bullet, a `[ ]`/`[x]`/`[X]` checkbox,
# then an Obsidian `[[wikilink]]`. Anything else (prose, non-task bullets) is skipped.
_TASK_RE = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX])\]\s*\[\[(?P<link>[^\]]+)\]\]")

_SUPPORTED_FALLBACK_EXTS = (".pdf", ".epub")


@dataclass(frozen=True)
class ChecklistEntry:
    checked: bool
    link_name: str  # inner wikilink text, e.g. "mask rcnn.pdf"


def parse_checklist(text: str) -> list[ChecklistEntry]:
    """Markdown text -> ordered checklist entries.

    Lines that aren't a ``- [ ]/[x] [[wikilink]]`` task are skipped. The inner
    link is normalised: an Obsidian alias (``[[name|alias]]``) is reduced to
    ``name``, and a subpath/anchor (``[[name.pdf#page=3]]``) is dropped to the
    bare filename."""
    entries: list[ChecklistEntry] = []
    for line in text.splitlines():
        m = _TASK_RE.match(line)
        if not m:
            continue
        link = m.group("link").split("|", 1)[0].split("#", 1)[0].strip()
        if not link:
            continue
        entries.append(ChecklistEntry(checked=m.group("mark") in "xX", link_name=link))
    return entries


def build_papers_index(papers_dir: Path) -> dict[str, Path]:
    """Map casefolded filename -> real Path for files directly in ``papers_dir``.

    ``os.scandir`` resolves the iCloud symlink on ``papers_dir`` itself, and we
    key by casefolded name so resolution mirrors Obsidian's case-insensitive
    link behaviour. First file wins on a casefold collision."""
    index: dict[str, Path] = {}
    with os.scandir(papers_dir) as it:
        for entry in it:
            if entry.is_file(follow_symlinks=True):
                index.setdefault(entry.name.casefold(), Path(entry.path))
    return index


def resolve_wikilink(link_name: str, index: dict[str, Path]) -> Path | None:
    """Resolve a wikilink target to a Path via the directory index,
    case-insensitively. Returns None when nothing matches.

    If the link omits an extension (``[[mask rcnn]]``), a ``.pdf``/``.epub``
    suffix is tried as a fallback, matching how Obsidian links to documents."""
    hit = index.get(link_name.casefold())
    if hit is not None:
        return hit
    if not Path(link_name).suffix:
        for ext in _SUPPORTED_FALLBACK_EXTS:
            hit = index.get((link_name + ext).casefold())
            if hit is not None:
                return hit
    return None
