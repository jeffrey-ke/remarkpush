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
    version: str = ""  # monotonic xochitl change counter (often "" on this no-cloud path)

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
            version=str(meta.get("version", "")),
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


def find_document_by_name(items: dict[str, Item], name: str, *, include_trash: bool = False) -> list[Item]:
    """Every live DocumentType item whose visible_name matches ``name``
    (case-insensitive), across the *whole* library — unlike ``find_child`` which
    is scoped to a single parent.

    Used to dedup a reading-list entry against any copy already on the device so
    we can *move* it instead of uploading a duplicate. Case-insensitive because
    the hard requirement is "never duplicate": a device copy differing only in
    case from the source file's stem must still be found."""
    target = name.casefold()
    matches: list[Item] = []
    for item in items.values():
        if item.deleted or not item.is_document:
            continue
        if item.parent == TRASH_PARENT and not include_trash:
            continue
        if item.visible_name.casefold() == target:
            matches.append(item)
    return matches


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


def move_document(
    sftp: _paramiko.SFTPClient,
    xochitl_path: str,
    item: Item,
    new_parent_uuid: str,
) -> None:
    """Re-parent an existing document by rewriting only its ``.metadata`` (the
    ``parent`` field, plus a fresh ``lastModified``). Does *not* re-upload the
    file. Mutates ``item`` so in-memory tree state stays correct.

    Moving to ``TRASH_PARENT`` is exactly how a document is trashed (reversible
    from the device's Trash), so prune reuses this.

    Touching only ``parent``/``lastModified`` is sufficient on the SSH-only path:
    xochitl rebuilds its tree from each document's ``parent`` on restart. The
    cloud-reconcile flags (``metadatamodified``/``synced``/``version``) are
    deliberately left alone — consistent with this tool's "no cloud" stance.

    The new metadata is written to a ``.part`` temp and renamed into place so an
    interrupted write can never leave a corrupt sidecar (mirrors
    ``upload_document``)."""
    meta_path = posixpath.join(xochitl_path, f"{item.uuid}.metadata")
    with sftp.open(meta_path, "r") as handle:
        meta = json.loads(handle.read().decode("utf-8"))
    meta["parent"] = new_parent_uuid
    meta["lastModified"] = _md.now_ms()

    tmp = f"{meta_path}.part"
    _write_text(sftp, tmp, json.dumps(meta, indent=4))
    try:
        sftp.posix_rename(tmp, meta_path)
    except (AttributeError, OSError):
        try:
            sftp.remove(meta_path)
        except OSError:
            pass
        sftp.rename(tmp, meta_path)
    item.parent = new_parent_uuid


def sanitize_name(name: str) -> str:
    """Make a visibleName safe as a local filename. The device allows '/' in
    names (e.g. '1/21'); map it to U+2215 DIVISION SLASH so it reads the same
    without creating phantom directories."""
    cleaned = name.replace("/", "∕").strip()
    return cleaned or "untitled"


def folder_path_of(items: dict[str, Item], item: Item) -> str:
    """Slash-joined sanitized folder path of an item's parents (no filename)."""
    parts: list[str] = []
    seen: set[str] = set()
    cur = items.get(item.parent)
    while cur is not None and cur.uuid not in seen:
        seen.add(cur.uuid)
        parts.append(sanitize_name(cur.visible_name))
        cur = items.get(cur.parent)
    return "/".join(reversed(parts))


def documents_under(items: dict[str, Item], root_uuid: str) -> list[Item]:
    """All DocumentType items at or below ``root_uuid`` (\"\" = whole library)."""
    kids = children_map(items, include_trash=False)
    docs: list[Item] = []
    stack = [root_uuid]
    while stack:
        parent = stack.pop()
        for item in kids.get(parent, []):
            if item.is_folder:
                stack.append(item.uuid)
            elif item.is_document:
                docs.append(item)
    return docs


def download_original(sftp: _paramiko.SFTPClient, xochitl_path: str, item: Item, dest, *, callback=None) -> None:
    remote = posixpath.join(xochitl_path, f"{item.uuid}.{item.file_type}")
    sftp.get(remote, str(dest), callback=callback)


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
