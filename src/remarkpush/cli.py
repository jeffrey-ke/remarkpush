"""remarkpush command-line interface.

Phase 0 commands: ``init`` (configure + verify access) and ``ls`` (read-only
device tree). ``push``/``pull``/``status`` are stubbed until Phase 1/2.
"""

from __future__ import annotations

import getpass
import posixpath
import shlex
import socket
import time
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
    TRASH_PARENT,
    Item,
    children_map,
    documents_under,
    download_original,
    ensure_folder_path,
    file_type_for,
    find_child,
    find_document_by_name,
    folder_path_of,
    move_document,
    read_device,
    restart_xochitl,
    sanitize_name,
    upload_document,
)
from . import history
from .ignore import is_ignored, load_ignore
from .index import Index, sha256_file
from .reading_list import ChecklistEntry, build_papers_index, parse_checklist, resolve_wikilink
from .transport import ssh, usb

app = typer.Typer(
    add_completion=True,
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
# reading-list
# --------------------------------------------------------------------------- #
_DEFAULT_READING_INDEX = Path("/Users/jke/repo/Research/Research/papers-remarkable.md")


@dataclass
class _ReadingItem:
    link_name: str
    checked: bool
    local: Path | None       # resolved source PDF, None when the link didn't match
    name: str                # device visible_name == local.stem ("" if unresolved)
    target_uuid: str         # destination folder uuid for this entry
    target_label: str        # destination folder name (for display)
    action: str              # push | move | noop | unresolved
    uuid: str = ""           # device doc uuid (for move/noop)
    size: int = 0


def _build_reading_plan(
    entries,
    papers_index: dict[str, Path],
    items: dict[str, Item],
    *,
    to_read_uuid: str,
    read_uuid: str,
    to_read_label: str,
    read_label: str,
) -> list[_ReadingItem]:
    """Per-entry action via a *library-wide* name lookup (so a copy already on the
    device is moved, never duplicated). `checked` picks the target folder."""
    plan: list[_ReadingItem] = []
    seen: set[str] = set()
    for e in entries:
        local = resolve_wikilink(e.link_name, papers_index)
        if local is None or file_type_for(local) is None:
            plan.append(_ReadingItem(e.link_name, e.checked, None, "", "", "", "unresolved"))
            continue
        name = local.stem
        key = name.casefold()
        if key in seen:  # same paper listed twice in the md — first occurrence wins
            continue
        seen.add(key)

        target_uuid = read_uuid if e.checked else to_read_uuid
        target_label = read_label if e.checked else to_read_label
        matches = find_document_by_name(items, name)
        if not matches:
            action, uuid = "push", ""
        elif any(m.parent == target_uuid for m in matches):
            action = "noop"
            uuid = next(m.uuid for m in matches if m.parent == target_uuid)
        else:
            # Prefer relocating a copy that's in the *other* managed folder; else any copy.
            other = next((m for m in matches if m.parent in (to_read_uuid, read_uuid)), matches[0])
            action, uuid = "move", other.uuid

        plan.append(
            _ReadingItem(
                e.link_name, e.checked, local, name,
                target_uuid, target_label, action, uuid, local.stat().st_size,
            )
        )
    return plan


def _stale_documents(items: dict[str, Item], managed_uuids: set[str], wanted_names: set[str]) -> list[Item]:
    """Live documents sitting in the managed folders whose name isn't in the md
    (``wanted_names`` are casefolded). These are the prune/stale candidates."""
    return [
        it
        for it in items.values()
        if it.is_document
        and not it.deleted
        and it.parent in managed_uuids
        and it.visible_name.casefold() not in wanted_names
    ]


_READING_STYLE = {
    "push": ("[green]push[/]", None),
    "move": ("[cyan]move[/]", "cyan"),
    "noop": ("[dim]in place[/]", "dim"),
    "unresolved": ("[red]unresolved[/]", "red"),
}


def _print_reading_plan(plan: list[_ReadingItem], stale: list[Item], prune: bool) -> None:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("action")
    table.add_column("document")
    table.add_column("→ folder")
    table.add_column("size", justify="right")
    for p in plan:
        label, style = _READING_STYLE[p.action]
        if p.action == "unresolved":
            table.add_row(label, f"[red]{p.link_name}[/]", "[dim]— no matching PDF[/]", "")
        else:
            doc = f"[{style}]{p.name}[/]" if style else p.name
            table.add_row(label, doc, f"[dim]/{p.target_label}[/]", _human(p.size))
    for it in stale:
        if prune:
            table.add_row("[magenta]prune[/]", f"[dim]{it.visible_name}[/]", "[dim]→ trash[/]", "")
        else:
            table.add_row("[yellow]stale[/]", f"[dim]{it.visible_name}[/]", "[dim](not in list)[/]", "")
    out.print(table)


@app.command(name="reading-list")
def reading_list(
    md_file: Path = typer.Argument(
        _DEFAULT_READING_INDEX, help="Obsidian checklist markdown file (the reading-list index)."
    ),
    papers_dir: Path = typer.Option(
        None, "--papers-dir", help="Folder of source PDFs (default: 'papers and figures' beside the md file)."
    ),
    to_read_folder: str = typer.Option("papers to read", "--to-read-folder", help="Device folder for unread papers."),
    read_folder: str = typer.Option("papers read", "--read-folder", help="Device folder for read papers."),
    prune: bool = typer.Option(
        False, "--prune", help="Move papers in the managed folders that are no longer in the md to the device trash."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; write nothing."),
    no_restart: bool = typer.Option(False, "--no-restart", help="Don't restart xochitl afterward."),
) -> None:
    """Sync an Obsidian reading-list checklist to the reMarkable.

    Unread (unchecked) papers go to the 'papers to read' folder; read (checked)
    papers go to 'papers read'. A paper already on the device is moved into the
    right folder, never re-uploaded — so unchecking a paper moves it back
    instead of creating a duplicate. With --prune, papers in those two folders
    that are no longer in the markdown file are moved to the device trash
    (reversible)."""
    cfg = _require_config()

    md_file = md_file.expanduser()
    if not md_file.is_file():
        err.print(f"[red]Reading-list file not found:[/] {md_file}")
        raise typer.Exit(1)
    papers_dir = (papers_dir or md_file.parent / "papers and figures").expanduser()
    if not papers_dir.is_dir():
        err.print(f"[red]Papers directory not found:[/] {papers_dir}")
        raise typer.Exit(1)

    entries = parse_checklist(md_file.read_text(encoding="utf-8"))
    if not entries:
        out.print("[dim]No checklist entries found in the markdown file.[/]")
        raise typer.Exit(0)
    papers_index = build_papers_index(papers_dir)

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

        sftp = client.open_sftp()
        try:
            # The two managed folders must exist before planning so noop/move
            # targeting is correct (ensure_folder_path mutates `items`). On a dry
            # run we only *resolve* them — never create — so nothing is written;
            # a not-yet-created folder gets a sentinel uuid no document can match.
            if dry_run:
                tr = _resolve_folder_uuid(items, to_read_folder)
                rd = _resolve_folder_uuid(items, read_folder)
                to_read_uuid = tr if tr is not None else "\x00to-read"
                read_uuid = rd if rd is not None else "\x00read"
            else:
                to_read_uuid = ensure_folder_path(sftp, cfg.xochitl_path, items, to_read_folder)
                read_uuid = ensure_folder_path(sftp, cfg.xochitl_path, items, read_folder)

            index = Index.load()
            plan = _build_reading_plan(
                entries, papers_index, items,
                to_read_uuid=to_read_uuid, read_uuid=read_uuid,
                to_read_label=to_read_folder, read_label=read_folder,
            )

            wanted = {p.name.casefold() for p in plan if p.action != "unresolved"}
            stale = _stale_documents(items, {to_read_uuid, read_uuid}, wanted)
            _print_reading_plan(plan, stale, prune)

            moves = [p for p in plan if p.action == "move"]
            uploads = [p for p in plan if p.action == "push"]
            unresolved = [p for p in plan if p.action == "unresolved"]
            prunes = stale if prune else []

            if unresolved:
                out.print(f"\n[yellow]⚠ {len(unresolved)} wikilink(s) matched no PDF in {papers_dir.name}/[/]")

            if dry_run:
                out.print(
                    f"\n[dim]Dry run — {len(uploads)} upload, {len(moves)} move, "
                    f"{len(prunes)} prune; nothing written.[/]"
                )
                raise typer.Exit(0)
            if not (moves or uploads or prunes):
                out.print("\n[dim]Already in sync.[/]")
                raise typer.Exit(0)

            # 1) moves — re-parent in place, keep the local index consistent.
            for p in moves:
                try:
                    move_document(sftp, cfg.xochitl_path, items[p.uuid], p.target_uuid)
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Move failed:[/] {p.name} — {exc}")
                    continue
                entry = index.get(p.local)
                if entry is not None:
                    index.record(
                        p.local, sha256=entry.sha256, uuid=entry.uuid,
                        visible_name=p.name, parent_uuid=p.target_uuid, size=entry.size,
                    )

            # 2) uploads — only papers not already anywhere on the device.
            if uploads:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    console=out,
                ) as prog:
                    for p in uploads:
                        task = prog.add_task(p.name[:38], total=p.size)

                        def _cb(done: int, total: int, _t=task, _sz=p.size) -> None:
                            prog.update(_t, completed=done, total=total or _sz)

                        new_uuid = upload_document(
                            sftp, cfg.xochitl_path, p.local,
                            parent_uuid=p.target_uuid, visible_name=p.name,
                            file_type=file_type_for(p.local) or "pdf", progress=_cb,
                        )
                        prog.update(task, completed=p.size)
                        index.record(
                            p.local, sha256=sha256_file(p.local), uuid=new_uuid,
                            visible_name=p.name, parent_uuid=p.target_uuid, size=p.size,
                        )

            # 3) prunes — trash papers dropped from the md (reversible).
            for it in prunes:
                try:
                    move_document(sftp, cfg.xochitl_path, it, TRASH_PARENT)
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Prune failed:[/] {it.visible_name} — {exc}")
        finally:
            sftp.close()

        index.save()
        if not no_restart:
            with out.status("[dim]Restarting xochitl…[/]", spinner="dots"):
                restart_xochitl(client)
    finally:
        client.close()

    summary = f"\n[green]✓ {len(uploads)} uploaded, {len(moves)} moved"
    summary += f", {len(prunes)} trashed.[/]" if prune else ".[/]"
    out.print(summary)
    if no_restart:
        out.print("[dim]Skipped xochitl restart — changes appear after the next restart/reboot.[/]")


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


@dataclass
class _PullItem:
    item: Item
    folder: str  # local relative folder path
    dest: Path
    kind: str  # "original" | "annotated" | "skip-notebook"


def _build_pull_plan(
    items: dict[str, Item], targets: list[Item], out_dir: Path, annotated: bool
) -> list[_PullItem]:
    plan: list[_PullItem] = []
    for doc in targets:
        folder = folder_path_of(items, doc)
        name = sanitize_name(doc.visible_name)
        local_folder = out_dir / Path(folder) if folder else out_dir
        if annotated:
            plan.append(_PullItem(doc, folder, local_folder / f"{name}.pdf", "annotated"))
        elif doc.file_type in ("pdf", "epub"):
            plan.append(_PullItem(doc, folder, local_folder / f"{name}.{doc.file_type}", "original"))
        else:
            plan.append(_PullItem(doc, folder, local_folder / f"{name}.pdf", "skip-notebook"))
    return plan


@app.command()
def pull(
    remote: str = typer.Argument("", help="Remote folder to pull (default: whole library)."),
    out_dir: Path = typer.Option(Path("."), "-o", "--out", help="Local output directory."),
    annotated: bool = typer.Option(
        False, "--annotated", help="Flatten annotations into a PDF (device-rendered over USB)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; download nothing."),
) -> None:
    """Pull documents (originals, or flattened annotated PDFs) from the device."""
    cfg = _require_config()
    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status(f"[dim]Connecting to {cfg.target()}…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
            items = read_device(client, cfg.xochitl_path)
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)

    folder_uuid = _resolve_folder_uuid(items, remote.strip("/"))
    if folder_uuid is None:
        client.close()
        err.print(f"[red]No such device folder:[/] {remote}")
        raise typer.Exit(1)

    targets = documents_under(items, folder_uuid)
    plan = _build_pull_plan(items, targets, out_dir, annotated)

    downloads = [p for p in plan if p.kind in ("original", "annotated")]
    skipped = [p for p in plan if p.kind == "skip-notebook"]

    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("kind")
    table.add_column("→ local path")
    for p in plan:
        if p.kind == "skip-notebook":
            table.add_row("[yellow]skip (notebook)[/]", f"[dim]{p.dest}[/]")
        else:
            table.add_row(f"[green]{p.kind}[/]", str(p.dest))
    out.print(table)

    if dry_run:
        client.close()
        out.print(f"\n[dim]Dry run — {len(downloads)} would download, nothing written.[/]")
        raise typer.Exit(0)
    if not downloads:
        client.close()
        out.print("\n[dim]Nothing to download.[/]")
        raise typer.Exit(0)

    web_host = cfg.host
    if annotated and not usb.web_interface_up(web_host):
        client.close()
        err.print(
            f"[red]Annotated pull needs the device renderer, but the USB web interface "
            f"isn't reachable at {web_host}:80.[/]"
        )
        err.print("[dim]Enable it on the tablet: Settings → Storage → USB web interface, "
                  "then make sure you're connected over the USB cable.[/]")
        raise typer.Exit(1)

    n_ok = 0
    try:
        sftp = client.open_sftp() if not annotated else None
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                console=out,
            ) as prog:
                task = prog.add_task("pulling", total=len(downloads))
                for p in downloads:
                    prog.update(task, description=p.item.visible_name[:38])
                    p.dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if annotated:
                            usb.download_rendered_pdf(p.item.uuid, p.dest, host=web_host)
                        else:
                            download_original(sftp, cfg.xochitl_path, p.item, p.dest)
                        n_ok += 1
                    except Exception as exc:  # noqa: BLE001
                        err.print(f"[red]Failed:[/] {p.item.visible_name} — {exc}")
                    prog.advance(task)
        finally:
            if sftp is not None:
                sftp.close()
    finally:
        client.close()

    out.print(f"\n[green]✓ Pulled {n_ok}/{len(downloads)} document(s) to {out_dir}/[/]")
    if skipped:
        out.print(f"[dim]Skipped {len(skipped)} notebook(s) (no original; use --annotated to render them).[/]")


# --------------------------------------------------------------------------- #
# sync-annotations
# --------------------------------------------------------------------------- #
@dataclass
class _AnnotationItem:
    link_name: str
    checked: bool
    local: Path | None       # resolved source PDF, None when the link didn't match
    name: str                # device visible_name == local.stem ("" if unresolved)
    uuid: str                # device doc uuid (for pull), "" when not on device
    dest: Path | None        # where the annotated copy is written, None when skipped early
    action: str              # pull | skip-unchecked | not-on-device | skip-existing | unresolved


def _build_annotation_plan(
    entries,
    papers_index: dict[str, Path],
    items: dict[str, Item],
    *,
    checked_only: bool,
    suffix: str,
    out_dir: Path | None,
    skip_existing: bool,
) -> list[_AnnotationItem]:
    """Per-entry pull plan: resolve the wikilink to a local source, find a live
    device copy by name (library-wide, case-insensitive), and target the
    annotated render at ``<source><suffix>.pdf`` beside the original (or under
    ``out_dir``). Repeated stems are de-duped (first occurrence wins)."""
    plan: list[_AnnotationItem] = []
    seen: set[str] = set()
    for e in entries:
        local = resolve_wikilink(e.link_name, papers_index)
        if local is None or file_type_for(local) is None:
            plan.append(_AnnotationItem(e.link_name, e.checked, None, "", "", None, "unresolved"))
            continue
        name = local.stem
        key = name.casefold()
        if key in seen:  # same paper listed twice in the md — first occurrence wins
            continue
        seen.add(key)

        if checked_only and not e.checked:
            plan.append(_AnnotationItem(e.link_name, e.checked, local, name, "", None, "skip-unchecked"))
            continue

        matches = find_document_by_name(items, name)
        if not matches:
            plan.append(_AnnotationItem(e.link_name, e.checked, local, name, "", None, "not-on-device"))
            continue

        dest = (out_dir or local.parent) / f"{local.stem}{suffix}.pdf"
        action = "skip-existing" if (skip_existing and dest.exists()) else "pull"
        plan.append(_AnnotationItem(e.link_name, e.checked, local, name, matches[0].uuid, dest, action))
    return plan


_ANNOTATION_STYLE = {
    "pull": ("[green]pull[/]", None),
    "skip-unchecked": ("[dim]skip (unread)[/]", "dim"),
    "not-on-device": ("[yellow]not on device[/]", "yellow"),
    "skip-existing": ("[dim]skip (exists)[/]", "dim"),
    "unresolved": ("[red]unresolved[/]", "red"),
}


def _print_annotation_plan(plan: list[_AnnotationItem]) -> None:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("action")
    table.add_column("document")
    table.add_column("→ local path")
    for p in plan:
        label, _style = _ANNOTATION_STYLE[p.action]
        if p.action == "unresolved":
            table.add_row(label, f"[red]{p.link_name}[/]", "[dim]— no matching PDF[/]")
        elif p.action == "not-on-device":
            table.add_row(label, p.name, "[dim]— nothing to render[/]")
        elif p.action == "pull":
            table.add_row(label, p.name, str(p.dest))
        else:  # skip-unchecked / skip-existing
            table.add_row(label, f"[dim]{p.name}[/]", f"[dim]{p.dest}[/]" if p.dest else "")
    out.print(table)


@app.command(name="sync-annotations")
def sync_annotations(
    md_file: Path = typer.Argument(
        _DEFAULT_READING_INDEX, help="Obsidian checklist markdown file (the reading-list index)."
    ),
    papers_dir: Path = typer.Option(
        None, "--papers-dir", help="Folder of source PDFs (default: 'papers and figures' beside the md file)."
    ),
    checked_only: bool = typer.Option(
        False, "--checked-only", help="Only pull papers checked (read) in the md."
    ),
    suffix: str = typer.Option(
        "_annotated", "--suffix", help="Inserted before .pdf so the pull doesn't clobber the original."
    ),
    out_dir: Path = typer.Option(
        None, "-o", "--out", help="Write pulled PDFs here instead of beside each source file."
    ),
    skip_existing: bool = typer.Option(
        False, "--skip-existing", help="Skip papers whose annotated copy already exists."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; download nothing."),
) -> None:
    """Pull annotated (device-rendered) PDFs for a reading-list checklist.

    For each wikilink in the markdown index, the matching document's flattened
    annotated PDF is fetched from the tablet (over the USB web interface) and
    written beside the original source as '<name>_annotated.pdf', so your
    annotations land in the vault without clobbering the pristine original. By
    default every listed paper that exists on the device is pulled; use
    --checked-only to restrict to papers marked read."""
    cfg = _require_config()

    md_file = md_file.expanduser()
    if not md_file.is_file():
        err.print(f"[red]Reading-list file not found:[/] {md_file}")
        raise typer.Exit(1)
    papers_dir = (papers_dir or md_file.parent / "papers and figures").expanduser()
    if not papers_dir.is_dir():
        err.print(f"[red]Papers directory not found:[/] {papers_dir}")
        raise typer.Exit(1)
    out_dir = out_dir.expanduser() if out_dir is not None else None

    entries = parse_checklist(md_file.read_text(encoding="utf-8"))
    if not entries:
        out.print("[dim]No checklist entries found in the markdown file.[/]")
        raise typer.Exit(0)
    papers_index = build_papers_index(papers_dir)

    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status(f"[dim]Connecting to {cfg.target()}…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
            items = read_device(client, cfg.xochitl_path)
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)

    plan = _build_annotation_plan(
        entries, papers_index, items,
        checked_only=checked_only, suffix=suffix, out_dir=out_dir, skip_existing=skip_existing,
    )
    _print_annotation_plan(plan)

    pulls = [p for p in plan if p.action == "pull"]
    missing = [p for p in plan if p.action == "not-on-device"]
    unresolved = [p for p in plan if p.action == "unresolved"]
    skipped = [p for p in plan if p.action in ("skip-unchecked", "skip-existing")]

    if unresolved:
        out.print(f"\n[yellow]⚠ {len(unresolved)} wikilink(s) matched no PDF in {papers_dir.name}/[/]")
    if missing:
        out.print(f"[yellow]⚠ {len(missing)} paper(s) not on the device — nothing to render.[/]")

    if dry_run:
        client.close()
        out.print(f"\n[dim]Dry run — {len(pulls)} would download, nothing written.[/]")
        raise typer.Exit(0)
    if not pulls:
        client.close()
        out.print("\n[dim]Nothing to pull.[/]")
        raise typer.Exit(0)

    web_host = cfg.host
    if not usb.web_interface_up(web_host):
        client.close()
        err.print(
            f"[red]Annotated pull needs the device renderer, but the USB web interface "
            f"isn't reachable at {web_host}:80.[/]"
        )
        err.print("[dim]Enable it on the tablet: Settings → Storage → USB web interface, "
                  "then make sure you're connected over the USB cable.[/]")
        raise typer.Exit(1)

    n_ok = 0
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=out,
        ) as prog:
            task = prog.add_task("pulling", total=len(pulls))
            for p in pulls:
                prog.update(task, description=p.name[:38])
                p.dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    usb.download_rendered_pdf(p.uuid, p.dest, host=web_host)
                    n_ok += 1
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Failed:[/] {p.name} — {exc}")
                prog.advance(task)
    finally:
        client.close()

    out.print(f"\n[green]✓ Pulled {n_ok}/{len(pulls)} annotated PDF(s).[/]")
    tail = []
    if missing:
        tail.append(f"{len(missing)} not on device")
    if unresolved:
        tail.append(f"{len(unresolved)} unresolved")
    if skipped:
        tail.append(f"{len(skipped)} skipped")
    if tail:
        out.print(f"[dim]({', '.join(tail)}.)[/]")


# --------------------------------------------------------------------------- #
# git — local-history layer (stage / commit / log / show / status / push / pull)
# --------------------------------------------------------------------------- #
git_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Local-history (git-like) layer over the reading-list sync: stage & commit "
        "papers offline, then push/pull the reMarkable. The md is the source of "
        "truth; stage/commit/log live in .remarkpush/ and are never written back."
    ),
)


def _resolve_md_and_papers(md_file: Path, papers_dir: Path | None) -> tuple[Path, Path]:
    """Validate the reading-list md and its papers directory (default: 'papers
    and figures' beside the md). Shared by the working-tree-building git commands."""
    md_file = md_file.expanduser()
    if not md_file.is_file():
        err.print(f"[red]Reading-list file not found:[/] {md_file}")
        raise typer.Exit(1)
    papers_dir = (papers_dir or md_file.parent / "papers and figures").expanduser()
    if not papers_dir.is_dir():
        err.print(f"[red]Papers directory not found:[/] {papers_dir}")
        raise typer.Exit(1)
    return md_file, papers_dir


@dataclass
class _ResolvedPaper:
    name: str
    local_path: Path
    checked: bool


def _resolve_papers(md_file: Path, papers_dir: Path) -> tuple[list[_ResolvedPaper], list[str]]:
    """Resolve every checklist wikilink to a local PDF — the hash-free half of
    `_build_working_tree`, shared with shell-completion (which can't afford to
    sha256 every paper, especially over the iCloud-synced papers dir, on every
    keypress). Repeated stems are de-duped (first wins, matching the push
    planners). Returns (resolved papers, unresolved wikilink names)."""
    entries = parse_checklist(md_file.read_text(encoding="utf-8"))
    papers_index = build_papers_index(papers_dir)
    resolved: list[_ResolvedPaper] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for e in entries:
        local = resolve_wikilink(e.link_name, papers_index)
        if local is None or file_type_for(local) is None:
            unresolved.append(e.link_name)
            continue
        name = local.stem
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(_ResolvedPaper(name=name, local_path=local, checked=e.checked))
    return resolved, unresolved


def _build_working_tree(
    md_file: Path, papers_dir: Path
) -> tuple[dict[str, history.ManifestEntry], list[str]]:
    """Derive the git *working tree* from the reading-list md: resolve every
    wikilink to a local PDF and snapshot its content hash + read-state, keyed by
    file stem (== device visible_name). Returns (tree, unresolved wikilink names)."""
    resolved, unresolved = _resolve_papers(md_file, papers_dir)
    tree = {
        p.name: history.ManifestEntry(
            sha256=sha256_file(p.local_path), checked=p.checked, local_path=str(p.local_path)
        )
        for p in resolved
    }
    return tree, unresolved


def _complete_paper_name(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion for `git add <TAB>`: real resolved paper stems from the
    reading list, filtered to those containing the partial word typed so far.
    `git add` matches on the *full* stem (no substring/fuzzy matching — see
    `git_add` below), so without this a partial guess like 'mask2former' just
    fails against the full title 'mask2former masked attention mask
    transformer...'. Swallows any error so a bad/missing md never breaks Tab."""
    try:
        md_file = Path(ctx.params.get("md_file") or _DEFAULT_READING_INDEX).expanduser()
        papers_dir = ctx.params.get("papers_dir")
        papers_dir = Path(papers_dir).expanduser() if papers_dir else md_file.parent / "papers and figures"
        if not md_file.is_file() or not papers_dir.is_dir():
            return []
        resolved, _ = _resolve_papers(md_file, papers_dir)
        return [p.name for p in resolved if incomplete.casefold() in p.name.casefold()]
    except Exception:
        return []


def _fmt_ts(created_at: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created_at)))
    except (ValueError, OSError):
        return created_at or "—"


@git_app.command("add")
def git_add(
    papers: list[str] = typer.Argument(
        None,
        help="Paper name(s)/stem(s) to stage (e.g. 'mask rcnn'). Omit and use --all.",
        autocompletion=_complete_paper_name,
    ),
    all_: bool = typer.Option(False, "--all", "-A", help="Stage every resolved paper in the reading list."),
    md_file: Path = typer.Option(_DEFAULT_READING_INDEX, "--md", help="Reading-list markdown index."),
    papers_dir: Path = typer.Option(
        None, "--papers-dir", help="Folder of source PDFs (default: 'papers and figures' beside the md)."
    ),
) -> None:
    """Stage papers from the reading list for the next commit. Purely local — no
    device contact. Snapshots each paper's content hash and read-state (so a later
    md edit isn't committed unless re-added), and never writes back to the md."""
    if not (papers or all_):
        err.print("[red]Nothing to stage.[/] Pass paper name(s) or [bold]--all[/].")
        raise typer.Exit(1)
    md_file, papers_dir = _resolve_md_and_papers(md_file, papers_dir)
    tree, unresolved = _build_working_tree(md_file, papers_dir)

    if all_:
        selected = list(tree)
    else:
        by_fold = {n.casefold(): n for n in tree}
        selected = []
        for want in papers:
            key = Path(want).stem.casefold()
            hit = by_fold.get(key) or by_fold.get(want.casefold())
            if hit is None:
                err.print(f"[red]Not in the reading list (or its wikilink is unresolved):[/] {want}")
                raise typer.Exit(1)
            selected.append(hit)

    staged = history.load_stage()
    for name in selected:
        staged[name] = tree[name]
    history.save_stage(None, staged)

    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("staged")
    table.add_column("read", justify="center")
    for name in selected:
        table.add_row(f"[green]{name}[/]", "✓" if tree[name].checked else "")
    out.print(table)
    out.print(f"[green]✓ Staged {len(selected)} paper(s).[/] [dim]{len(staged)} staged for the next commit.[/]")
    if unresolved:
        out.print(f"[yellow]⚠ {len(unresolved)} wikilink(s) matched no PDF and were skipped.[/]")


@git_app.command("commit")
def git_commit(
    message: str = typer.Option(..., "-m", "--message", help="Commit message."),
) -> None:
    """Snapshot the staged papers into a new local commit. Purely local — no
    device contact. Overlays the stage onto HEAD's manifest so HEAD always
    describes the complete desired device state."""
    staged = history.load_stage()
    if not staged:
        out.print("[dim]Nothing staged — run 'remarkpush git add' first.[/]")
        raise typer.Exit(0)
    parent = history.read_head()
    head = history.head_commit()
    head_manifest = head.manifest if head else {}
    new_manifest = history.merge_stage_into_head(head_manifest, staged)
    commit = history.make_commit(parent, new_manifest, message, now=time.time())
    history.append_commit(None, commit)
    history.write_head(None, commit.id)
    history.clear_stage()
    out.print(
        f"[green]✓[/] [bold]{commit.id}[/] {message}  "
        f"[dim]· {len(staged)} staged, {len(new_manifest)} paper(s) total[/]"
    )


@git_app.command("log")
def git_log(
    n: int = typer.Option(20, "-n", help="Show at most N commits (0 = all)."),
) -> None:
    """Show the local commit history (newest first). Purely local."""
    log = history.load_log()
    if not log:
        out.print("[dim]No commits yet.[/]")
        raise typer.Exit(0)
    head = history.read_head()
    shown = list(reversed(log if n <= 0 else log[-n:]))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("commit")
    table.add_column("when")
    table.add_column("papers", justify="right")
    table.add_column("message")
    for c in shown:
        marker = " [green]← HEAD[/]" if c.id == head else ""
        table.add_row(f"[bold]{c.id}[/]", _fmt_ts(c.created_at), str(len(c.manifest)), c.message + marker)
    out.print(table)
    if n > 0 and len(log) > n:
        out.print(f"[dim]… {len(log) - n} earlier commit(s) not shown (use -n 0 for all).[/]")


@git_app.command("show")
def git_show(
    commit: str = typer.Argument("HEAD", help="Commit id / id-prefix, or HEAD."),
) -> None:
    """Show a commit's manifest (the papers + read-state it snapshots). Purely local."""
    ref = history.read_head() if commit.upper() == "HEAD" else commit
    if not ref:
        out.print("[dim]No commits yet.[/]")
        raise typer.Exit(0)
    c = history.find_commit(None, ref)
    if c is None:
        err.print(f"[red]Unknown or ambiguous commit:[/] {commit}")
        raise typer.Exit(1)
    out.print(
        f"[bold]commit {c.id}[/]  [dim]parent {c.parent or '—'} · {_fmt_ts(c.created_at)}[/]"
    )
    out.print(f"  {c.message}\n")
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("read", justify="center")
    table.add_column("document")
    table.add_column("sha")
    table.add_column("source")
    for name, e in sorted(c.manifest.items(), key=lambda kv: kv[0].casefold()):
        table.add_row("✓" if e.checked else "", name, e.sha256[:10], f"[dim]{e.local_path}[/]")
    out.print(table)
    out.print(f"\n[dim]{len(c.manifest)} paper(s).[/]")


_GIT_STATUS_STYLE = {
    "untracked": "green",
    "staged": "green",
    "staged-stale": "yellow",
    "locally-modified": "cyan",
    "remote-modified": "magenta",
    "not-on-device": "yellow",
    "gone-from-md": "yellow",
    "tracked": "dim",
    "up-to-date": "dim",
}


@git_app.command("status")
def git_status(
    md_file: Path = typer.Option(_DEFAULT_READING_INDEX, "--md", help="Reading-list markdown index."),
    papers_dir: Path = typer.Option(
        None, "--papers-dir", help="Folder of source PDFs (default: 'papers and figures' beside the md)."
    ),
    offline: bool = typer.Option(False, "--offline", help="Skip the device read; show local columns only."),
) -> None:
    """Show working-tree vs HEAD vs stage, plus device annotations.

    Columns: A=staged, M=locally-modified (PDF changed since HEAD), R=remote-
    modified (annotated on the tablet since the last push/pull). With --offline
    (or if the device is unreachable) the device columns show '?'."""
    md_file, papers_dir = _resolve_md_and_papers(md_file, papers_dir)
    tree, unresolved = _build_working_tree(md_file, papers_dir)
    head = history.head_commit()
    head_manifest = head.manifest if head else {}
    staged = history.load_stage()
    rows = history.diff_working_tree(tree, head_manifest, staged)
    index = Index.load()

    items: dict[str, Item] = {}
    device_known = False
    if not offline:
        cfg = _require_config()
        password = None
        if not cfg.uses_key:
            password = getpass.getpass(f"Password for {cfg.target()}: ")
        try:
            with out.status("[dim]Reading device library…[/]", spinner="dots"):
                client = ssh.connect(cfg, password=password)
                items = read_device(client, cfg.xochitl_path)
                client.close()
            device_known = True
        except ssh.SSHError as exc:
            err.print(
                f"[yellow]Device unreachable[/] ({exc}); showing local status only "
                f"(pass --offline to silence)."
            )

    if not rows:
        out.print("[dim]Reading list is empty (no resolvable papers).[/]")
        raise typer.Exit(0)

    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("state")
    table.add_column("A", justify="center")
    table.add_column("M", justify="center")
    table.add_column("R", justify="center")
    table.add_column("dev", justify="center")
    table.add_column("paper")
    n_remote = 0
    for row in rows:
        # device-derived columns
        local_path = None
        for src in (tree, staged, head_manifest):
            if row.name in src:
                local_path = src[row.name].local_path
                break
        on_device: bool | None = None  # None = device not read (offline/unreachable)
        remote_mod = False
        if device_known:
            matches = find_document_by_name(items, row.name)
            on_device = bool(matches)
            if matches and local_path is not None:
                remote_mod = history.is_remote_modified(matches[0], index.get(Path(local_path)))
        if remote_mod:
            n_remote += 1

        state = history.headline(row, on_device=on_device, remote_mod=remote_mod)
        style = _GIT_STATUS_STYLE.get(state, "")
        a = "[green]A[/]" if row.staged else "[dim]·[/]"
        m = "[cyan]M[/]" if row.local_mod else "[dim]·[/]"
        if not device_known:
            r = "[dim]?[/]"
            dev = "[dim]?[/]"
        else:
            r = "[magenta]R[/]" if remote_mod else "[dim]·[/]"
            dev = "on" if on_device else "[red]✗[/]"
        table.add_row(f"[{style}]{state}[/]", a, m, r, dev, row.name)
    out.print(table)

    if head is None and not staged:
        out.print("[dim]No commits yet — 'git add' then 'git commit' to start tracking.[/]")
    if n_remote:
        out.print(f"[magenta]✎ {n_remote} paper(s) annotated on the tablet — 'remarkpush git pull' to fetch.[/]")
    if unresolved:
        out.print(f"[yellow]⚠ {len(unresolved)} wikilink(s) matched no PDF (not shown).[/]")


@git_app.command("push")
def git_push(
    to_read_folder: str = typer.Option("papers to read", "--to-read-folder", help="Device folder for unread papers."),
    read_folder: str = typer.Option("papers read", "--read-folder", help="Device folder for read papers."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; write nothing."),
    no_restart: bool = typer.Option(False, "--no-restart", help="Don't restart xochitl afterward."),
) -> None:
    """Push HEAD's manifest to the reMarkable (reading-list mechanics), then record
    the device change baseline so later annotations register as remote-modified.

    Unread papers go to 'papers to read', read papers to 'papers read'; a copy
    already on the device is moved, never duplicated. Removals are out of scope —
    use 'remarkpush reading-list --prune' to trash dropped papers."""
    cfg = _require_config()
    head = history.head_commit()
    if head is None:
        out.print("[dim]No commits to push — 'remarkpush git commit' first.[/]")
        raise typer.Exit(0)
    manifest = head.manifest

    resolved: list[tuple[str, bool, Path]] = []
    missing: list[str] = []
    for name, e in manifest.items():
        local = Path(e.local_path)
        if local.is_file():
            resolved.append((name, e.checked, local))
        else:
            missing.append(name)
    if missing:
        out.print(f"[yellow]⚠ {len(missing)} committed paper(s) no longer on disk — skipped.[/]")

    # Reuse the reading-list planner by reconstructing its (entries, papers_index) inputs
    # from the committed manifest (each entry already resolves to a real local file).
    papers_index = {local.name.casefold(): local for _n, _c, local in resolved}
    entries = [ChecklistEntry(checked, local.name) for _n, checked, local in resolved]

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

    n_base = 0
    try:
        with out.status("[dim]Reading device library…[/]", spinner="dots"):
            items = read_device(client, cfg.xochitl_path)

        sftp = client.open_sftp()
        try:
            if dry_run:
                tr = _resolve_folder_uuid(items, to_read_folder)
                rd = _resolve_folder_uuid(items, read_folder)
                to_read_uuid = tr if tr is not None else "\x00to-read"
                read_uuid = rd if rd is not None else "\x00read"
            else:
                to_read_uuid = ensure_folder_path(sftp, cfg.xochitl_path, items, to_read_folder)
                read_uuid = ensure_folder_path(sftp, cfg.xochitl_path, items, read_folder)

            plan = _build_reading_plan(
                entries, papers_index, items,
                to_read_uuid=to_read_uuid, read_uuid=read_uuid,
                to_read_label=to_read_folder, read_label=read_folder,
            )
            _print_reading_plan(plan, [], False)

            moves = [p for p in plan if p.action == "move"]
            uploads = [p for p in plan if p.action == "push"]

            if dry_run:
                out.print(
                    f"\n[dim]Dry run — {len(uploads)} upload, {len(moves)} move; nothing written.[/]"
                )
                raise typer.Exit(0)

            for p in moves:
                try:
                    move_document(sftp, cfg.xochitl_path, items[p.uuid], p.target_uuid)
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Move failed:[/] {p.name} — {exc}")

            if uploads:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    console=out,
                ) as prog:
                    for p in uploads:
                        task = prog.add_task(p.name[:38], total=p.size)

                        def _cb(done: int, total: int, _t=task, _sz=p.size) -> None:
                            prog.update(_t, completed=done, total=total or _sz)

                        upload_document(
                            sftp, cfg.xochitl_path, p.local,
                            parent_uuid=p.target_uuid, visible_name=p.name,
                            file_type=file_type_for(p.local) or "pdf", progress=_cb,
                        )
                        prog.update(task, completed=p.size)
        finally:
            sftp.close()

        if not no_restart:
            with out.status("[dim]Restarting xochitl…[/]", spinner="dots"):
                restart_xochitl(client)

        # Baseline: re-read the (post-write, post-restart) device and record each
        # committed paper's uuid + version + lastModified into the index. This
        # seeds change detection and — by capturing the timestamp our own move/
        # upload just wrote — keeps those writes from later reading as remote-mod.
        with out.status("[dim]Recording device baseline…[/]", spinner="dots"):
            fresh = read_device(client, cfg.xochitl_path)
        index = Index.load()
        for name, e in manifest.items():
            matches = find_document_by_name(fresh, name)
            if not matches:
                continue
            m = matches[0]
            local = Path(e.local_path)
            size = local.stat().st_size if local.is_file() else 0
            index.record(
                local, sha256=e.sha256, uuid=m.uuid, visible_name=name,
                parent_uuid=m.parent, size=size,
                device_version=m.version, device_last_modified=m.last_modified,
            )
            n_base += 1
        index.save()
    finally:
        client.close()

    out.print(
        f"\n[green]✓ {len(uploads)} uploaded, {len(moves)} moved.[/] "
        f"[dim]baseline recorded for {n_base} paper(s).[/]"
    )
    if no_restart:
        out.print("[dim]Skipped xochitl restart — changes appear after the next restart/reboot.[/]")


@git_app.command("pull")
def git_pull(
    suffix: str = typer.Option(
        "_annotated", "--suffix", help="Inserted before .pdf so the pull doesn't clobber the original."
    ),
    out_dir: Path = typer.Option(
        None, "-o", "--out", help="Write pulled PDFs here instead of beside each source file."
    ),
    all_: bool = typer.Option(
        False, "--all", help="Pull every on-device paper, not just those changed since the last sync."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; download nothing."),
) -> None:
    """Pull annotated (device-rendered) PDFs for HEAD's papers that changed on the
    tablet since the last push/pull, writing '<name>_annotated.pdf' beside each
    source. Read-only on the device; does NOT create a commit. Use --all to pull
    every on-device paper regardless of the change baseline."""
    cfg = _require_config()
    head = history.head_commit()
    if head is None:
        out.print("[dim]No commits — nothing to pull for. 'remarkpush git commit' first.[/]")
        raise typer.Exit(0)
    manifest = head.manifest
    out_dir = out_dir.expanduser() if out_dir is not None else None

    papers_index: dict[str, Path] = {}
    entries: list[ChecklistEntry] = []
    for _name, e in manifest.items():
        local = Path(e.local_path)
        papers_index[local.name.casefold()] = local
        entries.append(ChecklistEntry(e.checked, local.name))

    password = None
    if not cfg.uses_key:
        password = getpass.getpass(f"Password for {cfg.target()}: ")
    try:
        with out.status(f"[dim]Connecting to {cfg.target()}…[/]", spinner="dots"):
            client = ssh.connect(cfg, password=password)
            items = read_device(client, cfg.xochitl_path)
    except ssh.SSHError as exc:
        err.print(f"[red]Could not connect:[/] {exc}")
        _connection_hint(cfg)
        raise typer.Exit(1)

    index = Index.load()
    plan = _build_annotation_plan(
        entries, papers_index, items,
        checked_only=False, suffix=suffix, out_dir=out_dir, skip_existing=False,
    )

    # Gate the on-device 'pull' items by the change baseline (unless --all): only
    # fetch a render when the device version/lastModified moved past what we recorded.
    changed: list[_AnnotationItem] = []
    unchanged: list[_AnnotationItem] = []
    for p in plan:
        if p.action != "pull":
            continue
        item = items.get(p.uuid)
        if all_ or (item is not None and history.is_remote_modified(item, index.get(p.local))):
            changed.append(p)
        else:
            unchanged.append(p)
    missing = [p for p in plan if p.action == "not-on-device"]
    unresolved = [p for p in plan if p.action == "unresolved"]

    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("action")
    table.add_column("document")
    table.add_column("→ local path")
    for p in changed:
        table.add_row("[green]pull[/]", p.name, str(p.dest))
    for p in unchanged:
        table.add_row("[dim]unchanged[/]", f"[dim]{p.name}[/]", f"[dim]{p.dest}[/]")
    for p in missing:
        table.add_row("[yellow]not on device[/]", p.name, "[dim]— nothing to render[/]")
    for p in unresolved:
        table.add_row("[red]unresolved[/]", f"[red]{p.link_name}[/]", "[dim]— no matching PDF[/]")
    out.print(table)

    if dry_run:
        client.close()
        out.print(f"\n[dim]Dry run — {len(changed)} would download, nothing written.[/]")
        raise typer.Exit(0)
    if not changed:
        client.close()
        out.print("\n[dim]Nothing to pull — no device-side changes since the last sync.[/]")
        raise typer.Exit(0)

    web_host = cfg.host
    if not usb.web_interface_up(web_host):
        client.close()
        err.print(
            f"[red]Annotated pull needs the device renderer, but the USB web interface "
            f"isn't reachable at {web_host}:80.[/]"
        )
        err.print("[dim]Enable it on the tablet: Settings → Storage → USB web interface, "
                  "then make sure you're connected over the USB cable.[/]")
        raise typer.Exit(1)

    n_ok = 0
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=out,
        ) as prog:
            task = prog.add_task("pulling", total=len(changed))
            for p in changed:
                prog.update(task, description=p.name[:38])
                p.dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    usb.download_rendered_pdf(p.uuid, p.dest, host=web_host)
                    n_ok += 1
                    # Advance this paper's baseline to the just-observed device state
                    # so it isn't re-pulled until the next annotation.
                    item = items[p.uuid]
                    prev = index.get(p.local)
                    index.record(
                        p.local,
                        sha256=prev.sha256 if prev else (sha256_file(p.local) if p.local.is_file() else ""),
                        uuid=p.uuid, visible_name=p.name, parent_uuid=item.parent,
                        size=prev.size if prev else (p.local.stat().st_size if p.local.is_file() else 0),
                        device_version=item.version, device_last_modified=item.last_modified,
                    )
                except Exception as exc:  # noqa: BLE001
                    err.print(f"[red]Failed:[/] {p.name} — {exc}")
                prog.advance(task)
        index.save()
    finally:
        client.close()

    out.print(f"\n[green]✓ Pulled {n_ok}/{len(changed)} annotated PDF(s).[/]")
    tail = []
    if unchanged and not all_:
        tail.append(f"{len(unchanged)} unchanged")
    if missing:
        tail.append(f"{len(missing)} not on device")
    if unresolved:
        tail.append(f"{len(unresolved)} unresolved")
    if tail:
        out.print(f"[dim]({', '.join(tail)}.)[/]")


app.add_typer(git_app, name="git")
