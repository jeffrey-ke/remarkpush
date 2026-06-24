"""remarkpush command-line interface.

Phase 0 commands: ``init`` (configure + verify access) and ``ls`` (read-only
device tree). ``push``/``pull``/``status`` are stubbed until Phase 1/2.
"""

from __future__ import annotations

import getpass
from pathlib import Path

import paramiko
import typer
from rich.console import Console
from rich.tree import Tree as RichTree

from . import __version__
from .config import (
    DEFAULT_HOST,
    DEFAULT_USERNAME,
    DEFAULT_XOCHITL,
    DeviceConfig,
    CONFIG_PATH,
    load_config,
    repo_dir,
    save_config,
)
from .device import Item, children_map, read_device
from .transport import ssh

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="git-style push/pull of PDFs & EPUBs to a reMarkable 2 over SSH.",
)
out = Console()
err = Console(stderr=True)

_DEFAULT_KEY = Path.home() / ".ssh" / "remarkpush_rsa"


def _version_cb(value: bool) -> None:
    if value:
        out.print(f"remarkpush {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_config() -> DeviceConfig:
    cfg = load_config()
    if cfg is None:
        err.print("[red]Not configured.[/] Run [bold]remarkpush init[/] first.")
        raise typer.Exit(1)
    return cfg


def _connect(cfg: DeviceConfig) -> paramiko.SSHClient:
    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    return ssh.connect(cfg, password=password)


def _connection_hint(cfg: DeviceConfig) -> None:
    err.print("[dim]Checklist:[/]")
    err.print(f"[dim]  • Device on, and reachable at {cfg.host} (USB) or its Wi-Fi address[/]")
    err.print("[dim]  • Wi-Fi SSH on OS >3.20 needs a one-time 'rm-ssh-over-wlan on' over USB[/]")
    err.print("[dim]  • Root password: Settings → Help → Copyright and licenses (GPLv3 header)[/]")
    err.print(
        "[dim]  • If you see 'no matching host key type ... ssh-rsa', add to ~/.ssh/config:[/]\n"
        "[dim]      Host remarkable 10.11.99.1[/]\n"
        "[dim]        HostkeyAlgorithms +ssh-rsa[/]\n"
        "[dim]        PubkeyAcceptedAlgorithms +ssh-rsa[/]"
    )


def _render_tree(items: dict[str, Item], *, include_trash: bool) -> None:
    if not items:
        out.print("[dim](device library is empty)[/]")
        return
    kids = children_map(items, include_trash=include_trash)

    def add(node: RichTree, parent_uuid: str) -> None:
        ordered = sorted(
            kids.get(parent_uuid, []),
            key=lambda i: (not i.is_folder, i.visible_name.lower()),
        )
        for item in ordered:
            if item.is_folder:
                child = node.add(f"[bold blue]{item.visible_name}/[/]")
                add(child, item.uuid)
            else:
                suffix = f" [dim]({item.file_type})[/]" if item.file_type else ""
                star = "⭐ " if item.pinned else ""
                node.add(f"{star}{item.visible_name}{suffix}")

    root = RichTree("[bold]reMarkable[/]")
    add(root, "")
    if include_trash and kids.get("trash"):
        trash = root.add("[dim]🗑  Trash[/]")
        add(trash, "trash")

    n_docs = sum(1 for i in items.values() if i.is_document and not i.deleted)
    n_folders = sum(1 for i in items.values() if i.is_folder and not i.deleted)
    out.print(root)
    out.print(f"[dim]{n_docs} documents, {n_folders} folders[/]")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
@app.command()
def init(
    host: str = typer.Option(DEFAULT_HOST, help="Device IP / hostname."),
    username: str = typer.Option(DEFAULT_USERNAME, help="SSH user (root on the reMarkable)."),
    xochitl_path: str = typer.Option(DEFAULT_XOCHITL, help="On-device document store path."),
    use_key: bool = typer.Option(
        True,
        "--key/--password",
        help="Install a dedicated SSH key (recommended) vs. prompt for the password each run.",
    ),
) -> None:
    """Configure the device connection and verify access."""
    cfg = DeviceConfig(host=host, username=username, xochitl_path=xochitl_path, key_path=None)

    if use_key:
        priv = _DEFAULT_KEY
        if not priv.exists():
            out.print(f"[dim]Generating SSH key at {priv} …[/]")
            priv.parent.mkdir(parents=True, exist_ok=True)
            key = paramiko.RSAKey.generate(3072)
            key.write_private_key_file(str(priv))
            priv.chmod(0o600)
            pub_line = f"{key.get_name()} {key.get_base64()} remarkpush"
            (priv.parent / (priv.name + ".pub")).write_text(pub_line + "\n")
        else:
            key = paramiko.RSAKey.from_private_key_file(str(priv))
            pub_line = f"{key.get_name()} {key.get_base64()} remarkpush"

        password = getpass.getpass(f"Root password for {cfg.target()} (used once to install the key): ")
        try:
            ssh.install_public_key(cfg, password, pub_line)
        except ssh.SSHError as exc:
            err.print(f"[red]Key install failed:[/] {exc}")
            _connection_hint(cfg)
            raise typer.Exit(1)
        cfg.key_path = str(priv)
    else:
        # Verify the password works now, but never store it.
        password = getpass.getpass(f"Root password for {cfg.target()}: ")
        try:
            client = ssh.connect(cfg, password=password)
            client.close()
        except ssh.SSHError as exc:
            err.print(f"[red]Connection failed:[/] {exc}")
            _connection_hint(cfg)
            raise typer.Exit(1)

    # Final verification using the saved auth method.
    try:
        client = _connect(cfg) if cfg.uses_key else ssh.connect(cfg, password=password)
        ok = ssh.check(client)
        client.close()
    except ssh.SSHError as exc:
        err.print(f"[red]Verification failed:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)
    if not ok:
        err.print("[red]Connected, but the device did not respond as expected.[/]")
        raise typer.Exit(1)

    save_config(cfg)
    rdir = repo_dir()
    rdir.mkdir(exist_ok=True)

    out.print(f"[green]✓[/] Connected to [bold]{cfg.target()}[/].")
    out.print(f"[green]✓[/] Config saved to {CONFIG_PATH}.")
    out.print(f"[green]✓[/] Repo state initialized at {rdir}/.")
    if cfg.uses_key:
        out.print(f"[dim]Using key auth ({cfg.key_path}). No password stored.[/]")
    out.print("\nNext: [bold]remarkpush ls[/] to list your device library.")


@app.command()
def ls(
    include_trash: bool = typer.Option(False, "--trash", help="Include trashed items."),
) -> None:
    """List the device's folders and documents (read-only)."""
    cfg = _require_config()
    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status(f"[dim]Connecting to {cfg.target()}…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect to {cfg.host}:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)
    try:
        with out.status("[dim]Reading device library…[/]", spinner="dots"):
            items = read_device(client, cfg.xochitl_path)
    finally:
        client.close()
    _render_tree(items, include_trash=include_trash)


def _not_yet(name: str, phase: str) -> None:
    err.print(f"[yellow]`{name}` arrives in {phase} — not implemented yet.[/]")
    raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show what would push/pull (Phase 1)."""
    _not_yet("status", "Phase 1")


@app.command()
def push(
    paths: list[str] = typer.Argument(None, help="Files or folders to push."),
) -> None:
    """Push files/folders to the device (Phase 1)."""
    _not_yet("push", "Phase 1")


@app.command()
def pull(
    remote: str = typer.Argument(None, help="Remote folder to pull."),
    annotated: bool = typer.Option(False, "--annotated", help="Flatten annotations into the PDF."),
) -> None:
    """Pull documents back from the device (Phase 2)."""
    _not_yet("pull", "Phase 2")
