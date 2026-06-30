"""Local-history core for the ``remarkpush git`` layer — the git-mechanical
substrate (staging area, commit log, HEAD) over the reading-list sync.

These are **pure / local-only** helpers: no SSH, no USB, no device contact. They
turn a *working tree* (a name -> {sha256, checked, local_path} snapshot derived
from the Obsidian reading-list markdown) into staged snapshots and an append-only
commit log under ``.remarkpush/``. The device side (push/pull, change detection
against the live tablet) lives in ``cli.py``; this module only models the local
git state, so it unit-tests in isolation like ``reading_list.py``.

Conceptual mapping (see the repo CLAUDE.md "Tier 2" data-flow block):

    working tree  = derived from the md, never persisted
    staging area  = .remarkpush/stage.json   (snapshot written by `git add`)
    commit log    = .remarkpush/log.jsonl     (append-only, one commit per line)
    HEAD          = .remarkpush/HEAD           (current commit id, "" = none)

A commit's manifest captures *outbound intent only* — which PDF (by content
sha) at which read-state. Device identifiers (uuid/version/lastModified) are
unknown at commit time (commit precedes push) and are recorded into the *Index*
by `git push`, never into a commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .config import repo_dir

STAGE_FILENAME = "stage.json"
HEAD_FILENAME = "HEAD"
LOG_FILENAME = "log.jsonl"


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ManifestEntry:
    """One paper's local fact: content hash, read-state, and source path. Used
    identically for the live working tree, a staged snapshot, and a committed
    manifest entry."""

    sha256: str
    checked: bool
    local_path: str


@dataclass(frozen=True)
class Commit:
    id: str
    parent: str  # parent commit id, "" for the first commit
    created_at: str  # unix seconds as a string
    message: str
    manifest: dict[str, ManifestEntry]  # name (== local file stem) -> entry


@dataclass(frozen=True)
class StatusRow:
    """Per-paper *local* status flags (device columns are added by the caller,
    which alone can read the tablet). ``name`` is the local file stem."""

    name: str
    tracked: bool  # present in HEAD's manifest
    staged: bool  # present in the staging area
    stale: bool  # staged, but the working tree moved past the staged snapshot
    local_mod: bool  # tracked + working sha differs from HEAD + not staged
    untracked: bool  # resolves in the working tree but isn't tracked or staged
    gone_local: bool  # tracked but no longer present in the working tree (md)


# --------------------------------------------------------------------------- #
# pure commit mechanics
# --------------------------------------------------------------------------- #
def _canonical_manifest_json(manifest: dict[str, ManifestEntry]) -> str:
    """Deterministic JSON of a manifest, so a commit id is a stable content hash
    regardless of dict insertion order."""
    return json.dumps(
        {
            name: {"sha256": e.sha256, "checked": e.checked, "local_path": e.local_path}
            for name, e in sorted(manifest.items())
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_commit_id(
    parent: str, created_at: str, message: str, manifest: dict[str, ManifestEntry]
) -> str:
    """Git-style content hash of a commit (12 hex chars). Two commits with the
    same parent, time, message, and manifest get the same id; any difference
    changes it."""
    payload = "\n".join((parent, created_at, message, _canonical_manifest_json(manifest)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def make_commit(
    parent: str,
    manifest: dict[str, ManifestEntry],
    message: str,
    *,
    now: float,
) -> Commit:
    """Build a Commit from a manifest. ``now`` (unix seconds) is injected so the
    function stays pure/testable; the CLI passes ``time.time()``."""
    created_at = str(int(now))
    manifest = dict(manifest)
    return Commit(
        id=compute_commit_id(parent, created_at, message, manifest),
        parent=parent,
        created_at=created_at,
        message=message,
        manifest=manifest,
    )


def merge_stage_into_head(
    head_manifest: dict[str, ManifestEntry], staged: dict[str, ManifestEntry]
) -> dict[str, ManifestEntry]:
    """Overlay the staged snapshot onto HEAD's manifest, so the new commit
    describes the *complete* desired device state (staged papers replace/add,
    untouched papers carry forward). Mirrors ``git commit`` snapshotting the whole
    tree, not just the staged delta."""
    return {**head_manifest, **staged}


# --------------------------------------------------------------------------- #
# change detection (pure comparison of a device item vs the recorded baseline)
# --------------------------------------------------------------------------- #
def is_remote_modified(item, entry) -> bool:
    """True if the device document changed (e.g. was annotated) since we last
    recorded its baseline. ``item`` is a device ``Item`` (duck-typed: ``version``,
    ``last_modified``); ``entry`` is the Index ``Entry`` (``device_version``,
    ``device_last_modified``) or None.

    ``version`` is the strong signal when both sides have it, but on the no-cloud
    SSH path xochitl often leaves it empty for freshly pushed docs, so we fall
    back to ``lastModified``. With no baseline (e.g. a paper never pushed by this
    tool) we can't tell, and report False rather than a false positive."""
    if entry is None:
        return False
    base_v = getattr(entry, "device_version", "") or ""
    base_m = getattr(entry, "device_last_modified", "") or ""
    cur_v = getattr(item, "version", "") or ""
    cur_m = getattr(item, "last_modified", "") or ""
    if base_v and cur_v:
        return cur_v != base_v
    if base_m and cur_m:
        return cur_m != base_m
    return False


def diff_working_tree(
    working: dict[str, ManifestEntry],
    head_manifest: dict[str, ManifestEntry],
    staged: dict[str, ManifestEntry],
) -> list[StatusRow]:
    """Local (device-free) status of every paper across the working tree, HEAD,
    and the staging area, ordered case-insensitively by name."""
    rows: list[StatusRow] = []
    names = sorted(set(working) | set(head_manifest) | set(staged), key=str.casefold)
    for name in names:
        in_working = name in working
        tracked = name in head_manifest
        is_staged = name in staged

        stale = False
        if is_staged:
            if not in_working:
                stale = True  # staged a paper the md no longer resolves
            else:
                w, s = working[name], staged[name]
                stale = (w.sha256 != s.sha256) or (w.checked != s.checked)

        local_mod = (
            tracked
            and in_working
            and not is_staged
            and working[name].sha256 != head_manifest[name].sha256
        )
        untracked = in_working and not tracked and not is_staged
        gone_local = tracked and not in_working and not is_staged

        rows.append(
            StatusRow(
                name=name,
                tracked=tracked,
                staged=is_staged,
                stale=stale,
                local_mod=local_mod,
                untracked=untracked,
                gone_local=gone_local,
            )
        )
    return rows


def headline(row: StatusRow, *, on_device: bool | None, remote_mod: bool) -> str:
    """Collapse a row's flags (plus the device-derived signals) into a single
    headline state by precedence. The raw columns are still shown alongside; this
    is just the one-word summary. ``on_device`` is None when the device wasn't
    read (offline / unreachable), in which case device-derived verdicts are
    suppressed: a clean tracked paper reads as ``tracked`` rather than asserting
    ``up-to-date`` or ``not-on-device`` we can't actually confirm."""
    if row.untracked:
        return "untracked"
    if row.staged:
        return "staged-stale" if row.stale else "staged"
    if row.local_mod:
        return "locally-modified"
    if remote_mod:
        return "remote-modified"
    if on_device is False and row.tracked:
        return "not-on-device"
    if row.gone_local:
        return "gone-from-md"
    if on_device is None and row.tracked:
        return "tracked"
    return "up-to-date"


# --------------------------------------------------------------------------- #
# (de)serialization
# --------------------------------------------------------------------------- #
def _entry_to_dict(e: ManifestEntry) -> dict:
    return {"sha256": e.sha256, "checked": e.checked, "local_path": e.local_path}


def _entry_from_dict(d: dict) -> ManifestEntry:
    return ManifestEntry(
        sha256=d["sha256"], checked=bool(d["checked"]), local_path=d["local_path"]
    )


def _commit_to_dict(c: Commit) -> dict:
    return {
        "id": c.id,
        "parent": c.parent,
        "created_at": c.created_at,
        "message": c.message,
        "manifest": {n: _entry_to_dict(e) for n, e in c.manifest.items()},
    }


def _commit_from_dict(d: dict) -> Commit:
    return Commit(
        id=d["id"],
        parent=d.get("parent", ""),
        created_at=d.get("created_at", ""),
        message=d.get("message", ""),
        manifest={n: _entry_from_dict(e) for n, e in d.get("manifest", {}).items()},
    )


# --------------------------------------------------------------------------- #
# on-disk state under .remarkpush/  (stage.json / HEAD / log.jsonl)
# --------------------------------------------------------------------------- #
def _stage_path(root: Path | None) -> Path:
    return repo_dir(root) / STAGE_FILENAME


def _head_path(root: Path | None) -> Path:
    return repo_dir(root) / HEAD_FILENAME


def _log_path(root: Path | None) -> Path:
    return repo_dir(root) / LOG_FILENAME


def load_stage(root: Path | None = None) -> dict[str, ManifestEntry]:
    p = _stage_path(root)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {k: _entry_from_dict(v) for k, v in raw.items()}


def save_stage(root: Path | None, staged: dict[str, ManifestEntry]) -> None:
    p = _stage_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: _entry_to_dict(v) for k, v in staged.items()}
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_stage(root: Path | None = None) -> None:
    p = _stage_path(root)
    if p.exists():
        p.unlink()


def read_head(root: Path | None = None) -> str:
    p = _head_path(root)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def write_head(root: Path | None, commit_id: str) -> None:
    p = _head_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(commit_id + "\n", encoding="utf-8")


def append_commit(root: Path | None, commit: Commit) -> None:
    p = _log_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_commit_to_dict(commit), separators=(",", ":")) + "\n")


def load_log(root: Path | None = None) -> list[Commit]:
    """All commits in write order (oldest first)."""
    p = _log_path(root)
    if not p.exists():
        return []
    commits: list[Commit] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            commits.append(_commit_from_dict(json.loads(line)))
    return commits


def head_commit(root: Path | None = None) -> Commit | None:
    hid = read_head(root)
    return find_commit(root, hid) if hid else None


def find_commit(root: Path | None, ref: str) -> Commit | None:
    """Resolve a commit by exact id or unique id-prefix; None if absent or the
    prefix is ambiguous."""
    log = load_log(root)
    by_id = {c.id: c for c in log}
    if ref in by_id:
        return by_id[ref]
    matches = [c for c in log if c.id.startswith(ref)]
    return matches[0] if len(matches) == 1 else None
