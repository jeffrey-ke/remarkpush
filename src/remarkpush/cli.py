"""remarkpush command-line interface.

Phase 0 commands: ``init`` (configure + verify access) and ``ls`` (read-only
device tree). ``push``/``pull``/``status`` are stubbed until Phase 1/2.
"""

from __future__ import annotations

import getpass
import posixpath
import shlex
import socket
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

import paramiko
import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
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
from .device import (
    Item,
    children_map,
    ensure_folder_path,
    file_type_for,
    find_child,
    read_device,
    restart_xochitl,
    upload_document,
)
from .ignore import is_ignored, load_ignore
from .index import Index, sha256_file
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


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
@dataclass
class _PushItem:
    local: Path
    folder_path: str  # device folder, "" = root
    name: str
    file_type: str | None
    size: int
    sha: str
    action: str  # upload | uptodate | exists | unsupported
    uuid: str = ""


def _expand_paths(paths: list[str], to: str) -> list[tuple[Path, str]]:
    pairs: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            err.print(f"[red]Not found:[/] {raw}")
            raise typer.Exit(1)
        if p.is_file():
            pairs.append((p, to))
        else:
            spec = load_ignore(p)
            base = "/".join(x for x in (to, p.name) if x)
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(p)
                if is_ignored(spec, str(rel)):
                    continue
                sub = "/".join(rel.parts[:-1])
                folder = "/".join(x for x in (base, sub) if x)
                pairs.append((f, folder))
    return pairs


def _resolve_folder_uuid(items: dict[str, Item], folder_path: str) -> str | None:
    """Existing folder UUID for a slash path, or None if it isn't all there yet."""
    if not folder_path:
        return ""
    parent = ""
    for part in (p for p in folder_path.split("/") if p):
        child = find_child(items, parent, part, folders_only=True)
        if child is None:
            return None
        parent = child.uuid
    return parent


def _build_plan(
    pairs: list[tuple[Path, str]],
    items: dict[str, Item],
    index: Index,
    force: bool,
) -> list[_PushItem]:
    plan: list[_PushItem] = []
    for local, folder in pairs:
        ft = file_type_for(local)
        size = local.stat().st_size
        name = local.stem
        if ft is None:
            plan.append(_PushItem(local, folder, name, None, size, "", "unsupported"))
            continue
        sha = sha256_file(local)
        entry = index.get(local)
        if entry and entry.sha256 == sha and entry.uuid in items and not force:
            plan.append(_PushItem(local, folder, name, ft, size, sha, "uptodate", entry.uuid))
            continue
        folder_uuid = _resolve_folder_uuid(items, folder)
        existing = find_child(items, folder_uuid, name, folders_only=False) if folder_uuid is not None else None
        if existing is not None and not force:
            plan.append(_PushItem(local, folder, name, ft, size, sha, "exists", existing.uuid))
            continue
        plan.append(_PushItem(local, folder, name, ft, size, sha, "upload"))
    return plan


_ACTION_STYLE = {
    "upload": ("[green]push[/]", None),
    "uptodate": ("[dim]up-to-date[/]", "dim"),
    "exists": ("[yellow]exists[/]", "yellow"),
    "unsupported": ("[red]skip (type)[/]", "red"),
}


def _print_plan(plan: list[_PushItem]) -> None:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("action")
    table.add_column("document")
    table.add_column("→ folder")
    table.add_column("size", justify="right")
    for p in plan:
        label, style = _ACTION_STYLE[p.action]
        dest = "/" + p.folder_path if p.folder_path else "/"
        name = f"[{style}]{p.name}[/]" if style else p.name
        table.add_row(label, name, f"[dim]{dest}[/]", _human(p.size))
    out.print(table)


@app.command()
def push(
    paths: list[str] = typer.Argument(..., help="Files or folders to push."),
    to: str = typer.Option("", "--to", help="Destination folder (slash path, created if missing)."),
    tag: list[str] = typer.Option([], "--tag", help="Tag(s) to apply (best-effort)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; write nothing."),
    force: bool = typer.Option(False, "--force", help="Upload even if a same-named doc exists."),
    no_restart: bool = typer.Option(False, "--no-restart", help="Don't restart xochitl afterward."),
) -> None:
    """Push PDFs/EPUBs (files or folders) to the device."""
    cfg = _require_config()
    pairs = _expand_paths(paths, to.strip("/"))
    if not pairs:
        out.print("[dim]Nothing to push.[/]")
        raise typer.Exit(0)

    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status(f"[dim]Connecting to {cfg.target()}…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)

    try:
        with out.status("[dim]Reading device library…[/]", spinner="dots"):
            items = read_device(client, cfg.xochitl_path)
        index = Index.load()
        plan = _build_plan(pairs, items, index, force)
        _print_plan(plan)

        uploads = [p for p in plan if p.action == "upload"]
        if dry_run:
            out.print(f"\n[dim]Dry run — {len(uploads)} would upload, nothing written.[/]")
            raise typer.Exit(0)
        if not uploads:
            out.print("\n[dim]Nothing to upload.[/]")
            raise typer.Exit(0)

        sftp = client.open_sftp()
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                console=out,
            ) as prog:
                for p in uploads:
                    folder_uuid = (
                        ensure_folder_path(sftp, cfg.xochitl_path, items, p.folder_path)
                        if p.folder_path
                        else ""
                    )
                    task = prog.add_task(p.name[:38], total=p.size)

                    def _cb(done: int, total: int, _t=task, _sz=p.size) -> None:
                        prog.update(_t, completed=done, total=total or _sz)

                    new_uuid = upload_document(
                        sftp,
                        cfg.xochitl_path,
                        p.local,
                        parent_uuid=folder_uuid,
                        visible_name=p.name,
                        file_type=p.file_type or "pdf",
                        tags=list(tag),
                        progress=_cb,
                    )
                    prog.update(task, completed=p.size)
                    index.record(
                        p.local,
                        sha256=p.sha,
                        uuid=new_uuid,
                        visible_name=p.name,
                        parent_uuid=folder_uuid,
                        size=p.size,
                    )
        finally:
            sftp.close()

        index.save()
        if not no_restart:
            with out.status("[dim]Restarting xochitl…[/]", spinner="dots"):
                restart_xochitl(client)
    finally:
        client.close()

    out.print(f"\n[green]✓ Pushed {len(uploads)} document(s).[/]")
    if no_restart:
        out.print("[dim]Skipped xochitl restart — files appear after the next restart/reboot.[/]")


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def _tcp_open(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True, "open"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _write_test(client: paramiko.SSHClient, xochitl_path: str) -> tuple[bool, str]:
    sftp = client.open_sftp()
    name = posixpath.join(xochitl_path, f".remarkpush-preflight-{_uuid.uuid4().hex}.tmp")
    try:
        with sftp.open(name, "w") as h:
            h.write(b"ok")
        sftp.remove(name)
        return True, "writable"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sftp.close()


def _free_space(client: paramiko.SSHClient, path: str) -> str | None:
    _rc, dfout, _e = ssh.run(client, f"df -k {shlex.quote(path)} | tail -1")
    parts = dfout.split()
    if len(parts) >= 4 and parts[3].isdigit():
        return f"{int(parts[3]) / 1024 / 1024:.2f} GiB free"
    return None


@app.command()
def preflight() -> None:
    """Check the device is ready for push/pull (run after plugging in)."""
    cfg = _require_config()
    rows: list[tuple[str, bool, str]] = [("config", True, str(CONFIG_PATH))]

    ok, detail = _tcp_open(cfg.host, 22)
    rows.append((f"reach {cfg.host}:22", ok, detail))
    if not ok:
        _render_checks(rows)
        _connection_hint(cfg)
        raise typer.Exit(1)

    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        client = ssh.connect(cfg, password=password)
    except ssh.SSHError as exc:
        rows.append(("ssh auth", False, str(exc)))
        _render_checks(rows)
        _connection_hint(cfg)
        raise typer.Exit(1)
    rows.append(("ssh auth", True, cfg.target() + (" · key" if cfg.uses_key else " · password")))

    try:
        _rc, dirout, _e = ssh.run(client, f"test -d {shlex.quote(cfg.xochitl_path)} && echo ok")
        rows.append(("xochitl store", dirout.strip() == "ok", cfg.xochitl_path))

        wok, wdetail = _write_test(client, cfg.xochitl_path)
        rows.append(("write access (push)", wok, wdetail))

        free = _free_space(client, cfg.xochitl_path)
        rows.append(("free space", free is not None, free or "unknown"))

        _rc, active, _e = ssh.run(client, "systemctl is-active xochitl.service")
        rows.append(("xochitl service", active.strip() == "active", active.strip() or "unknown"))

        items = read_device(client, cfg.xochitl_path)
        ndoc = sum(1 for i in items.values() if i.is_document and not i.deleted)
        rows.append(("library read (pull)", True, f"{ndoc} documents"))
    finally:
        client.close()

    _render_checks(rows)
    ready = all(ok for _n, ok, _d in rows)
    out.print(
        "\n[green]✓ Ready to push and pull.[/]" if ready else "\n[red]✗ Not ready — see failures above.[/]"
    )
    raise typer.Exit(0 if ready else 1)


def _render_checks(rows: list[tuple[str, bool, str]]) -> None:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("")
    table.add_column("check")
    table.add_column("detail")
    for name, ok, detail in rows:
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        table.add_row(mark, name, f"[dim]{detail}[/]")
    out.print(table)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@app.command()
def status() -> None:
    """Show push state of PDFs/EPUBs under the current directory."""
    cfg = _require_config()
    root = Path.cwd()
    spec = load_ignore(root)
    files = [
        f
        for f in sorted(root.rglob("*"))
        if f.is_file() and file_type_for(f) and not is_ignored(spec, str(f.relative_to(root)))
    ]
    if not files:
        out.print("[dim]No PDFs/EPUBs under the current directory.[/]")
        raise typer.Exit(0)

    index = Index.load(root)
    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status("[dim]Reading device library…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
            items = read_device(client, cfg.xochitl_path)
            client.close()
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect:[/] {exc}")
        raise typer.Exit(1)

    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("state")
    table.add_column("file")
    for f in files:
        rel = f.relative_to(root)
        entry = index.get(f)
        if entry is None:
            state = "[green]new[/]"
        elif entry.uuid not in items:
            state = "[yellow]gone on device[/]"
        elif sha256_file(f) != entry.sha256:
            state = "[cyan]modified[/]"
        else:
            state = "[dim]up-to-date[/]"
        table.add_row(state, str(rel))
    out.print(table)


@app.command()
def pull(
    remote: str = typer.Argument(None, help="Remote folder to pull."),
    annotated: bool = typer.Option(False, "--annotated", help="Flatten annotations into the PDF."),
) -> None:
    """Pull documents back from the device (Phase 2)."""
    err.print("[yellow]`pull` arrives in Phase 2 — not implemented yet.[/]")
    raise typer.Exit(1)
