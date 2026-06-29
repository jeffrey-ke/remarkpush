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
from .ignore import is_ignored, load_ignore
from .index import Index, sha256_file
from .reading_list import build_papers_index, parse_checklist, resolve_wikilink
from .transport import ssh, usb

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
