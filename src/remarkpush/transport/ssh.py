"""Thin paramiko wrapper for talking to the reMarkable over SSH.

Read-only in Phase 0: connect, run a command, install a public key. The device
runs an old Dropbear, so connections may need ssh-rsa host-key/pubkey
algorithms; ``connect`` keeps paramiko's defaults (which still include ssh-rsa)
and surfaces a clear hint on failure rather than silently disabling anything.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import paramiko

from ..config import DeviceConfig


class SSHError(RuntimeError):
    pass


def connect(
    cfg: DeviceConfig,
    password: str | None = None,
    *,
    timeout: float = 10.0,
) -> paramiko.SSHClient:
    """Open an SSH connection. Uses key auth when ``cfg.key_path`` is set,
    otherwise the supplied password."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs: dict = dict(
        hostname=cfg.host,
        port=22,
        username=cfg.username,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    if cfg.key_path:
        kwargs["key_filename"] = str(Path(cfg.key_path).expanduser())
    else:
        if password is None:
            raise SSHError("a password is required (no key configured)")
        kwargs["password"] = password

    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException as exc:
        raise SSHError(f"authentication failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - normalize to one error type for the CLI
        raise SSHError(str(exc)) from exc
    return client


def run(client: paramiko.SSHClient, command: str, *, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run a shell command; return (exit_code, stdout, stderr)."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return rc, out, err


def install_public_key(cfg: DeviceConfig, password: str, public_key_line: str) -> None:
    """Append ``public_key_line`` to the device's authorized_keys (idempotent),
    authenticating once with the password."""
    pw_cfg = DeviceConfig(
        host=cfg.host,
        username=cfg.username,
        xochitl_path=cfg.xochitl_path,
        key_path=None,
    )
    client = connect(pw_cfg, password=password)
    try:
        quoted = shlex.quote(public_key_line)
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qxF {quoted} ~/.ssh/authorized_keys || echo {quoted} >> ~/.ssh/authorized_keys"
        )
        rc, _out, err = run(client, cmd)
        if rc != 0:
            raise SSHError(f"could not install key (exit {rc}): {err.strip()}")
    finally:
        client.close()


def check(client: paramiko.SSHClient) -> bool:
    """Cheap liveness check."""
    rc, out, _err = run(client, "echo remarkpush-ok")
    return rc == 0 and "remarkpush-ok" in out
