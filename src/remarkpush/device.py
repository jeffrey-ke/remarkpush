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


# --------------------------------------------------------------------------- #
# writer (push side)
# --------------------------------------------------------------------------- #
import posixpath
import uuid as _uuid

import paramiko as _paramiko

from . import metadata as _md
from .transport.ssh import SSHError

SUPPORTED_TYPES = {".pdf": "pdf", ".epub": "epub"}


def file_type_for(path) -> str | None:
    from pathlib import Path

    return SUPPORTED_TYPES.get(Path(path).suffix.lower())


def find_child(items: dict[str, Item], parent_uuid: str, name: str, *, folders_only: bool) -> Item | None:
    for item in items.values():
        if item.deleted or item.parent != parent_uuid:
            continue
        if folders_only and not item.is_folder:
            continue
        if item.visible_name == name:
            return item
    return None


def _write_text(sftp: _paramiko.SFTPClient, remote_path: str, text: str) -> None:
    with sftp.open(remote_path, "w") as handle:
        handle.write(text.encode("utf-8"))


def ensure_folder_path(
    sftp: _paramiko.SFTPClient,
    xochitl_path: str,
    items: dict[str, Item],
    folder_path: str,
) -> str:
    """Resolve ``folder_path`` (slash-separated) under root, creating missing
    collections and reusing existing ones. Returns the leaf folder UUID, or ""
    for root. Mutates ``items`` so callers see newly created folders."""
    parts = [p.strip() for p in folder_path.split("/") if p.strip()]
    parent = ""
    for part in parts:
        existing = find_child(items, parent, part, folders_only=True)
        if existing is not None:
            parent = existing.uuid
            continue
        new_uuid = str(_uuid.uuid4())
        _write_text(sftp, posixpath.join(xochitl_path, f"{new_uuid}.metadata"), _md.folder_metadata(part, parent))
        _write_text(sftp, posixpath.join(xochitl_path, f"{new_uuid}.content"), _md.folder_content())
        _write_text(sftp, posixpath.join(xochitl_path, f"{new_uuid}.pagedata"), "\n")
        items[new_uuid] = Item(uuid=new_uuid, visible_name=part, parent=parent, type="CollectionType")
        parent = new_uuid
    return parent


def upload_document(
    sftp: _paramiko.SFTPClient,
    xochitl_path: str,
    local_path,
    *,
    parent_uuid: str,
    visible_name: str,
    file_type: str,
    tags: list[str] | None = None,
    progress=None,
) -> str:
    """Upload one document + sidecars. Returns the new document UUID.

    The original file streams to a ``.part`` temp name and is renamed into place
    so a partial transfer is never left as a live document file."""
    doc_uuid = str(_uuid.uuid4())
    base = posixpath.join(xochitl_path, doc_uuid)
    final = f"{base}.{file_type}"
    tmp = f"{final}.part"

    sftp.put(str(local_path), tmp, callback=progress)
    try:
        sftp.posix_rename(tmp, final)
    except (AttributeError, OSError):
        # Fall back if the server lacks posix-rename: remove then rename.
        try:
            sftp.remove(final)
        except OSError:
            pass
        sftp.rename(tmp, final)

    _write_text(sftp, f"{base}.content", _md.document_content(file_type, tags=tags))
    try:
        sftp.mkdir(base)
    except OSError:
        pass
    _write_text(sftp, f"{base}.pagedata", "")
    # Write metadata last: it's what xochitl scans to discover the document.
    _write_text(sftp, f"{base}.metadata", _md.document_metadata(visible_name, parent_uuid))
    return doc_uuid


def restart_xochitl(client: paramiko.SSHClient) -> None:
    """Restart xochitl once so writes appear, guarding the systemd start-limit.

    xochitl has a strict start-limit; hitting it can drop the device into its
    emergency target and reboot. Reset the failure counter first, then restart;
    tolerate a transient channel drop if the service comes back active."""
    run(client, "systemctl reset-failed xochitl.service")
    rc, _out, err = run(client, "systemctl restart xochitl.service")
    if rc == 0:
        return
    _rc2, active, _err2 = run(client, "systemctl is-active xochitl.service")
    if active.strip() == "active":
        return
    raise SSHError(f"failed to restart xochitl (exit {rc}): {err.strip()}")
