"""Device connection config (global) + per-repo state location.

The *device* connection (host/auth/storage path) is stored once per machine at
``~/.config/remarkpush/config.toml`` and reused across every local "repo".
Per-repo sync state (the index) lives in a ``.remarkpush/`` directory inside the
folder you sync — created by ``init`` and consumed from Phase 1 onward.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

DEFAULT_HOST = "10.11.99.1"
DEFAULT_USERNAME = "root"
DEFAULT_XOCHITL = "/home/root/.local/share/remarkable/xochitl"

REPO_DIR_NAME = ".remarkpush"

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_PATH = _CONFIG_HOME / "remarkpush" / "config.toml"


@dataclass
class DeviceConfig:
    host: str = DEFAULT_HOST
    username: str = DEFAULT_USERNAME
    xochitl_path: str = DEFAULT_XOCHITL
    # When set, authenticate with this private key. Otherwise prompt for a
    # password at runtime (never stored on disk).
    key_path: str | None = None

    @property
    def uses_key(self) -> bool:
        return bool(self.key_path)

    def target(self) -> str:
        return f"{self.username}@{self.host}"


def load_config(path: Path = CONFIG_PATH) -> DeviceConfig | None:
    """Return the saved device config, or ``None`` if not configured yet."""
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text())
    dev = data.get("device", {})
    return DeviceConfig(
        host=dev.get("host", DEFAULT_HOST),
        username=dev.get("username", DEFAULT_USERNAME),
        xochitl_path=dev.get("xochitl_path", DEFAULT_XOCHITL),
        key_path=dev.get("key_path") or None,
    )


def save_config(cfg: DeviceConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    device: dict[str, str] = {
        "host": cfg.host,
        "username": cfg.username,
        "xochitl_path": cfg.xochitl_path,
    }
    if cfg.key_path:
        device["key_path"] = cfg.key_path
    path.write_text(tomli_w.dumps({"device": device}))
    path.chmod(0o600)


def repo_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / REPO_DIR_NAME
