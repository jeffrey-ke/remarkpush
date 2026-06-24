"""Local sync index — the git-index analogue that makes push idempotent.

Maps each local file (by absolute path) to what we last pushed: its content
hash and the device document UUID. On re-push, an unchanged hash means "already
on the device, skip"; a changed hash means "content updated". Stored as JSON in
``.remarkpush/index.json`` inside the folder you sync.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import repo_dir

INDEX_FILENAME = "index.json"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass
class Entry:
    sha256: str
    uuid: str
    visible_name: str
    parent_uuid: str
    size: int
    pushed_at: str


class Index:
    def __init__(self, path: Path, entries: dict[str, Entry] | None = None):
        self.path = path
        self.entries: dict[str, Entry] = entries or {}

    @classmethod
    def load(cls, root: Path | None = None) -> "Index":
        path = repo_dir(root) / INDEX_FILENAME
        if not path.exists():
            return cls(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = {k: Entry(**v) for k, v in raw.get("entries", {}).items()}
        return cls(path, entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": {k: asdict(v) for k, v in self.entries.items()}}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def key(local_path: Path) -> str:
        return str(local_path.resolve())

    def get(self, local_path: Path) -> Entry | None:
        return self.entries.get(self.key(local_path))

    def record(
        self,
        local_path: Path,
        *,
        sha256: str,
        uuid: str,
        visible_name: str,
        parent_uuid: str,
        size: int,
    ) -> None:
        self.entries[self.key(local_path)] = Entry(
            sha256=sha256,
            uuid=uuid,
            visible_name=visible_name,
            parent_uuid=parent_uuid,
            size=size,
            pushed_at=str(int(time.time())),
        )
