---
description: git/huggingface-style CLI to push & pull PDFs/EPUBs to a reMarkable 2 over SSH
alwaysApply: true
---

# remarkpush

A git/huggingface-style CLI to push and pull PDFs & EPUBs to/from a reMarkable 2 over SSH, readable and annotatable in the stock reMarkable software.

## Quick start

```
uv sync
uv run remarkpush init        # configure device, install SSH key, verify
uv run remarkpush preflight   # check device is ready (after plugging in)
uv run remarkpush push paper.pdf --to Reading
uv run remarkpush pull Reading -o ./out --annotated
uv run remarkpush reading-list notes.md --dry-run   # sync an Obsidian checklist
```

## Module index

| Module | Role | Key exports |
|---|---|---|
| `src/remarkpush/cli.py` | Typer CLI; all commands + plan/progress/render helpers | `app`, `init`, `preflight`, `ls`, `push`, `status`, `pull`, `reading_list` |
| `src/remarkpush/config.py` | Per-machine device config (`~/.config/remarkpush/config.toml`) + repo paths | `DeviceConfig`, `load_config`, `save_config`, `repo_dir` |
| `src/remarkpush/device.py` | xochitl store model (read tree) + writer (push/move) + pull helpers | `Item`, `read_device`, `parse_dump`, `build_items`, `children_map`, `ensure_folder_path`, `upload_document`, `move_document`, `restart_xochitl`, `documents_under`, `download_original`, `folder_path_of`, `find_child`, `find_document_by_name`, `sanitize_name`, `file_type_for` |
| `src/remarkpush/reading_list.py` | **Pure** parser for an Obsidian checklist (`- [ ]/[x] [[name.pdf]]`) + case-insensitive wikilink→file resolution | `parse_checklist`, `build_papers_index`, `resolve_wikilink`, `ChecklistEntry` |
| `src/remarkpush/metadata.py` | Sidecar JSON builders for a *fresh* import (current OS format) | `now_ms`, `document_metadata`, `folder_metadata`, `document_content`, `folder_content` |
| `src/remarkpush/index.py` | Local sync index (`.remarkpush/index.json`) for idempotent push | `Index`, `Entry`, `sha256_file` |
| `src/remarkpush/ignore.py` | `.rmpushignore` (gitwildmatch) matcher | `load_ignore`, `is_ignored` |
| `src/remarkpush/transport/ssh.py` | paramiko SSH/SFTP; deadlock-safe `run`, key install | `connect`, `run`, `install_public_key`, `check`, `SSHError` |
| `src/remarkpush/transport/usb.py` | USB web interface (xochitl HTTP) for device-rendered annotated pull | `web_interface_up`, `download_rendered_pdf` |
| `tests/test_device.py` | Unit tests for the pure model/helpers | — |
| `tests/test_reading_list.py` | Unit tests for the checklist parser + reading-list planner/stale logic | — |

## Data flow

```
cli command
  └─ config.load_config()                      # device host/auth/xochitl_path
       └─ transport/ssh.connect()              # key or password auth
            └─ device.read_device()            # one SSH dump of *.metadata/*.content
                 └─ parse_dump → build_items   # logical tree of Item(s)

push:  expand paths (+.rmpushignore) → sha256 (index) → plan
       → ensure_folder_path (create collections) + upload_document
         (SFTP .part→rename + metadata.py sidecars) → restart_xochitl (once)
       → Index.record / save

pull:  documents_under(folder) → either
         download_original()                 (SFTP, exact bytes)  or
         transport/usb.download_rendered_pdf  (device-rendered flattened PDF)

reading-list:  parse_checklist(md) + build_papers_index(papers_dir)
       → ensure_folder_path("papers to read"/"papers read")   (skipped on --dry-run)
       → _build_reading_plan: per entry, find_document_by_name (LIBRARY-WIDE, case-insensitive)
            none → push;  in target folder → noop;  elsewhere → move (never re-upload)
       → execute: moves (move_document) → uploads (upload_document) → prunes
            (--prune: move stale managed-folder docs to TRASH_PARENT) → restart_xochitl once
```

Key invariants:
- **Transport is SSH-first** (no reMarkable cloud → no 50-doc cap / Connect subscription). The USB web interface is used *only* for `pull --annotated`.
- **Batch writes, then restart xochitl exactly once** per push, guarded by `systemctl reset-failed` (the start-limit can otherwise reboot the device).
- **The big SSH dump must be drained as it streams** — `transport/ssh.run` avoids the paramiko channel-window deadlock (a real library exceeds the 2 MB window).
- **Fresh-import sidecars are minimal** (`formatVersion 2`, empty `cPages`); xochitl populates page data on first open. Do not hand-author the CRDT page tree.

## Where to look next

Documentation, plans, style guidance, and investigation notes live in `.docs_claude/`.

- `.docs_claude/plans/active/` -- plans currently in progress
- `.docs_claude/plans/completed/` -- finished plans
- `.docs_claude/style-and-beliefs/` -- code style and design principles

## Plans & workflow

Plans are first-class artifacts in `.docs_claude/plans/`.

- **Small change** (one file, obvious fix): no plan needed.
- **Medium change** (new feature, wire up a subsystem): lightweight plan in `plans/active/`.
- **Complex change** (new architecture, pipeline redesign): full execution plan with goal, approach, staged checklist, and decision log in `plans/active/`.

Move completed plans to `plans/completed/`.

**Before planning any new implementation:**
1. Read `plans/active/` -- don't duplicate in-progress work.
2. Read `plans/completed/` -- learn from past decisions and avoid re-solving solved problems.
3. Read relevant docs in `.docs_claude/` -- context that shaped the current design.

## Core beliefs

Before planning any implementation, read `/reusable-parts` and apply its guidelines to the design.
