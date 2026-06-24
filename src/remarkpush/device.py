"""Model of the reMarkable's on-device document store (xochitl).

Each document/folder is a UUID with sidecar JSON: ``<uuid>.metadata`` (name,
parent, type) and ``<uuid>.content`` (fileType, ...). We read the whole set in
one SSH round-trip and reconstruct the logical tree. Read-only here; the writer
(folder creation, sidecar generation) lands in Phase 1.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

import paramiko

from .transport.ssh import run

# Delimiter we print before each file when dumping the store in one shot. Chosen
# to be vanishingly unlikely to appear at the start of a metadata JSON line.
MARKER = "@@@RMPUSH@@@"

ROOT_PARENT = ""
TRASH_PARENT = "trash"


@dataclass
class Item:
    uuid: str
    visible_name: str
    parent: str
    type: str  # "CollectionType" (folder) | "DocumentType" (file/notebook)
    file_type: str = ""  # "pdf" | "epub" | "notebook" | ""
    deleted: bool = False
    pinned: bool = False
    last_modified: str = ""

    @property
    def is_folder(self) -> bool:
        return self.type == "CollectionType"

    @property
    def is_document(self) -> bool:
        return self.type == "DocumentType"


def dump_command(xochitl_path: str) -> str:
    """Shell command that prints every .metadata/.content file with a marker."""
    p = shlex.quote(xochitl_path)
    return (
        f"cd {p} 2>/dev/null || exit 0; "
        f'for f in *.metadata *.content; do '
        f'[ -e "$f" ] || continue; '
        f'printf "\\n{MARKER}%s\\n" "$f"; cat "$f"; '
        f"done"
    )


def parse_dump(text: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Split a dump into {uuid: metadata} and {uuid: content} maps."""
    metadata: dict[str, dict] = {}
    content: dict[str, dict] = {}
    if MARKER not in text:
        return metadata, content

    for chunk in text.split(MARKER)[1:]:
        newline = chunk.find("\n")
        if newline == -1:
            continue
        fname = chunk[:newline].strip()
        body = chunk[newline + 1 :]
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if fname.endswith(".metadata"):
            metadata[fname[: -len(".metadata")]] = data
        elif fname.endswith(".content"):
            content[fname[: -len(".content")]] = data
    return metadata, content


def build_items(metadata: dict[str, dict], content: dict[str, dict]) -> dict[str, Item]:
    items: dict[str, Item] = {}
    for uuid, meta in metadata.items():
        item_type = meta.get("type", "")
        if item_type not in ("CollectionType", "DocumentType"):
            continue
        cont = content.get(uuid, {})
        items[uuid] = Item(
            uuid=uuid,
            visible_name=meta.get("visibleName", uuid),
            parent=meta.get("parent", "") or "",
            type=item_type,
            file_type=cont.get("fileType", "") if item_type == "DocumentType" else "",
            deleted=bool(meta.get("deleted", False)),
            pinned=bool(meta.get("pinned", False)),
            last_modified=str(meta.get("lastModified", "")),
        )
    return items


def children_map(items: dict[str, Item], *, include_trash: bool = False) -> dict[str, list[Item]]:
    """Map parent-uuid -> [child items], skipping deleted (and trash unless asked)."""
    kids: dict[str, list[Item]] = {}
    for item in items.values():
        if item.deleted:
            continue
        if item.parent == TRASH_PARENT and not include_trash:
            continue
        kids.setdefault(item.parent, []).append(item)
    return kids


def read_device(client: paramiko.SSHClient, xochitl_path: str) -> dict[str, Item]:
    _rc, out, _err = run(client, dump_command(xochitl_path))
    metadata, content = parse_dump(out)
    return build_items(metadata, content)
