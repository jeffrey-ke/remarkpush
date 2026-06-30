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
uv run remarkpush reading-list notes.md --dry-run        # push an Obsidian checklist → device
uv run remarkpush sync-annotations notes.md --dry-run    # pull annotated PDFs back beside the sources

# git-like local-history layer (Tier 2) — the md is the working tree, the tablet is the remote
uv run remarkpush git add -A                             # stage every resolved paper (offline)
uv run remarkpush git commit -m "week 1"                 # snapshot the stage into a local commit (offline)
uv run remarkpush git status                             # working-tree vs HEAD vs stage; flags papers annotated on the tablet
uv run remarkpush git push                               # push HEAD to the device + record the device change baseline
uv run remarkpush git pull                               # pull annotated PDFs for papers that changed on the tablet
uv run remarkpush git log / show                         # browse the local commit history
```

## Module index

| Module | Role | Key exports |
|---|---|---|
| `src/remarkpush/cli.py` | Typer CLI; all commands + plan/progress/render helpers | `app`, `init`, `preflight`, `ls`, `push`, `status`, `pull`, `reading_list`, `sync_annotations`, `git_app` (sub-app: `git add/commit/log/show/status/push/pull`) |
| `src/remarkpush/history.py` | **Pure** local-history core for `git_app`: working-tree diff, commit/stage/HEAD/log over `.remarkpush/`, device change-detection | `ManifestEntry`, `Commit`, `StatusRow`, `make_commit`, `merge_stage_into_head`, `diff_working_tree`, `headline`, `is_remote_modified`, `load_stage`/`save_stage`/`clear_stage`, `read_head`/`write_head`, `append_commit`/`load_log`/`head_commit`/`find_commit` |
| `src/remarkpush/config.py` | Per-machine device config (`~/.config/remarkpush/config.toml`) + repo paths | `DeviceConfig`, `load_config`, `save_config`, `repo_dir` |
| `src/remarkpush/device.py` | xochitl store model (read tree) + writer (push/move) + pull helpers | `Item`, `read_device`, `parse_dump`, `build_items`, `children_map`, `ensure_folder_path`, `upload_document`, `move_document`, `restart_xochitl`, `documents_under`, `download_original`, `folder_path_of`, `find_child`, `find_document_by_name`, `sanitize_name`, `file_type_for` |
| `src/remarkpush/reading_list.py` | **Pure** parser for an Obsidian checklist (`- [ ]/[x] [[name.pdf]]`) + case-insensitive wikilink→file resolution | `parse_checklist`, `build_papers_index`, `resolve_wikilink`, `ChecklistEntry` |
| `src/remarkpush/metadata.py` | Sidecar JSON builders for a *fresh* import (current OS format) | `now_ms`, `document_metadata`, `folder_metadata`, `document_content`, `folder_content` |
| `src/remarkpush/index.py` | Local sync index (`.remarkpush/index.json`); idempotent push + device change baseline (`device_version`/`device_last_modified`) | `Index`, `Entry`, `sha256_file` |
| `src/remarkpush/ignore.py` | `.rmpushignore` (gitwildmatch) matcher | `load_ignore`, `is_ignored` |
| `src/remarkpush/transport/ssh.py` | paramiko SSH/SFTP; deadlock-safe `run`, key install | `connect`, `run`, `install_public_key`, `check`, `SSHError` |
| `src/remarkpush/transport/usb.py` | USB web interface (xochitl HTTP) for device-rendered annotated pull | `web_interface_up`, `download_rendered_pdf` |
| `tests/test_device.py` | Unit tests for the pure model/helpers | — |
| `tests/test_reading_list.py` | Unit tests for the checklist parser + reading-list planner/stale logic | — |
| `tests/test_sync_annotations.py` | Unit tests for the sync-annotations planner (pull/not-on-device/unresolved/skip/dedup) | — |
| `tests/test_history.py` | Unit tests for the pure history core (commit id, stage/merge, working-tree diff + headline precedence, change detection, stage/HEAD/log round-trips, index schema migration) | — |

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

sync-annotations:  parse_checklist(md) + build_papers_index(papers_dir)   (inverse of reading-list)
       → _build_annotation_plan: per entry resolve_wikilink → local Path; find_document_by_name
            (LIBRARY-WIDE) → device UUID; target <stem>_annotated.pdf beside the source (or -o dir)
            none on device → not-on-device;  --checked-only → skip unread;  --skip-existing → skip
       → read_device over SSH only maps name→UUID; bytes come over the USB web interface
            (usb.web_interface_up gate → usb.download_rendered_pdf, HTTP /download/{uuid}/pdf)
       → READ-ONLY on device: no SFTP, no Index writes, no restart_xochitl

git (Tier 2, local history):  the reading-list md = working tree, the tablet = remote.
   working tree  = _build_working_tree(md): parse_checklist → resolve_wikilink → sha256_file,
                   keyed by file stem  (derived live, never persisted)
   git add       → history.save_stage: snapshot {stem → sha256,checked,local_path} to stage.json
   git commit    → merge_stage_into_head(HEAD.manifest, stage) → make_commit → append log.jsonl,
                   advance HEAD, clear stage      (PURELY LOCAL — no device)
   git status    → diff_working_tree(working, HEAD, stage) [+ read_device unless --offline]:
                   columns A=staged / M=local-mod (sha≠HEAD) / R=remote-mod (is_remote_modified:
                   device version|lastModified ≠ Index baseline) / dev=on·✗·?
   git push      → reconstruct (entries, papers_index) from HEAD.manifest → REUSE _build_reading_plan
                   (push/move/noop into "papers to read"/"papers read") → execute → restart →
                   re-read device → Index.record device_version+device_last_modified (the baseline)
   git pull      → reconstruct from HEAD.manifest → REUSE _build_annotation_plan, GATED to docs whose
                   device version/lastModified moved past the Index baseline (--all bypasses) →
                   usb.download_rendered_pdf → <stem>_annotated.pdf; advance baseline. NOT a commit.
```

Key invariants:
- **Transport is SSH-first** (no reMarkable cloud → no 50-doc cap / Connect subscription). The USB web interface is used *only* for `pull --annotated`.
- **Batch writes, then restart xochitl exactly once** per push, guarded by `systemctl reset-failed` (the start-limit can otherwise reboot the device).
- **The big SSH dump must be drained as it streams** — `transport/ssh.run` avoids the paramiko channel-window deadlock (a real library exceeds the 2 MB window).
- **Fresh-import sidecars are minimal** (`formatVersion 2`, empty `cPages`); xochitl populates page data on first open. Do not hand-author the CRDT page tree.
- **`git` commit/add/log/show are purely local** — only `push`/`pull` (and non-`--offline` `status`) touch the device. Stage/commit while the tablet is unplugged; push when docked.
- **The reading-list md is the single editable source of truth.** `git add`/`commit`/`log` write a derived snapshot layer under `.remarkpush/` (`stage.json`/`log.jsonl`/`HEAD`) and are NEVER written back to the md; a commit captures outbound intent only (sha + read-state), never device ids. Device change detection leans on `lastModified` (xochitl's `version` is often empty on the no-cloud path; see `history.is_remote_modified`). No `git rm`/branches/merge — removals stay in `reading-list --prune`.

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
