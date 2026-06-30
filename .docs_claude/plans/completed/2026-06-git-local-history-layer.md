# Plan: `remarkpush git` — a local-history (git-like) layer over the reMarkable sync

## Context

`remarkpush` already syncs PDFs between an Obsidian reading-list markdown (the "local
repo") and a reMarkable tablet (the "remote"): `reading-list` pushes a wikilink checklist
to the device; `sync-annotations` pulls the annotation-flattened renders back beside the
sources. The user wants a **git-familiar workflow** on top of this — `add` (stage),
`commit`, `push`, `pull`, `status`, `log`, `show` — with **commit separate from push** so
they can stage/commit while the tablet is unplugged and push when it is docked over USB.
They also want `status`/`pull` to **detect device-side annotations** (papers they marked up
on the tablet), which today nothing does.

Research (3 explore agents + 1 plan agent, this session) established that the existing
`Index` is already git's *index* layer (content-addressing by sha256 + tracked/idempotent
push state), the device already exposes a cheap change signal (`lastModified`, and a
`version` counter), and that staging/commit/push/refs port cleanly to opaque binaries while
merge/rebase/delta-compression do not (confirmed by git-lfs / huggingface precedent — they
skip line-diffs entirely for binaries). So this is mostly a thin **porcelain + history**
layer over proven mechanics, plus a two-field index schema bump.

### Decisions locked (via Q&A this session)

1. **Scope = Tier 2 (local history).** Real staging area + append-only commit log; commit is
   a distinct, offline step from push. **No** branches, refs, merge, rebase, cherry-pick,
   delta/packfile compression, or per-page annotation deltas (text/CRDT-bound, meaningless
   for opaque annotated PDFs).
2. **Change signal = `version` + `lastModified`.** Store both in the index baseline; treat
   `lastModified` as the practical trigger (`version` may be a cloud-sync counter that does
   not move on this no-cloud device — validate empirically on first real run), `version`
   corroborating.
3. **Removals stay out of history (v1).** No removal tombstones in the manifest; `git push`
   only adds/moves, **never trashes**. Dropping a paper off the device remains the explicit,
   separate `reading-list --prune` action. `rm` can be added later.
4. **Dedicated command namespace.** All new verbs live under `remarkpush git <verb>` (Typer
   sub-app). The 5 existing commands (`push`/`pull`/`status`/`reading-list`/`sync-annotations`)
   are left **100% untouched** — no behavioral breaks, no arity-dispatch footguns.

## Resolved data model

> **The markdown manifest is the only editable source of truth. The working tree is
> *derived* from it (parse → resolve → hash) and never persisted. `add`/`commit`/`log`
> write a separate snapshot layer under `.remarkpush/` that is NEVER written back to the
> md** — so the read/unread checkboxes edited in Obsidian never fight the CLI.

| Git concept | remarkpush | Lives in | Editable by user |
|---|---|---|---|
| Working tree | join of `parse_checklist(md)` → `resolve_wikilink` → local PDF + `sha256_file` + `checked` (computed on the fly) | derived, never stored | yes — edit the md / the PDFs |
| Index (last-pushed + change baseline) | existing `Index`/`Entry`, **+`device_version`,`device_last_modified`** | `.remarkpush/index.json` | no |
| Staging area | NEW `{name → {sha256, checked, local_path}}` snapshot at `add` time | `.remarkpush/stage.json` | no |
| Commit log | NEW append-only, one commit JSON per line | `.remarkpush/log.jsonl` | no |
| HEAD | NEW one-line file: current commit id (`""` = none) | `.remarkpush/HEAD` | no |

`.remarkpush/` is already git-ignored and `.rmpushignore`-excluded → this history is
local-only. `repo_dir()` (`config.py:72`) already resolves it.

**Commit object** (one line of `log.jsonl`):
```json
{"id":"a1b2c3d4e5f6","parent":"0f9e…","created_at":"1782762208",
 "message":"week 3 reading","manifest":{
   "mask rcnn":{"sha256":"49c6…","checked":false,"local_path":"/…/mask rcnn.pdf"}}}
```
- `id = sha256(parent + created_at + message + canonical_json(manifest))[:12]`.
- The manifest captures **outbound intent only** (which PDF by content sha, at which
  read-state). Device fields (`uuid`/`version`/`lastModified`) are **deliberately absent** —
  unknown at commit time; `push` records them into the *Index*, not the commit.
- `commit` overlays stage onto HEAD's manifest (`{**head, **staged}`) so HEAD always
  describes the *complete* desired device state → `push`/`status` are well-defined.

**Why `add` snapshots (not edits the md):** the md's `checked` flag is real user intent; a
second writer would create the exact drift the user fears. `add` snapshots sha+checked like
`git add` snapshots a blob — a later md edit simply isn't in the next commit unless re-`add`ed.

## `commit ≠ push` execution boundary

| Command | Device? | Touches |
|---|---|---|
| `git add` / `commit` / `log` / `show` | **none — fully offline** | `sha256_file`; `stage.json` / `log.jsonl` / `HEAD` |
| `git status` | one SSH dump (skip with `--offline`) | reads device `version`/`lastModified` for the remote column |
| `git push` | SSH+SFTP (+1 restart) | reading-list mechanics; records device baseline into Index |
| `git pull` | SSH dump + USB web interface | writes `*_annotated.pdf`; advances Index baseline |

## Files & changes

### 1. NEW `src/remarkpush/history.py` (pure core — mirrors `reading_list.py` idiom)
Dataclasses `ManifestEntry{sha256,checked,local_path}`, `Commit{id,parent,created_at,message,manifest}`.
Pure (unit-testable): `compute_commit_id(...)`, `make_commit(parent, manifest, message, *, now)`
(`now` injectable for tests), `merge_stage_into_head(head, staged)`, `diff_working_tree(head, working, staged, items, index)` → status rows.
Thin I/O on `repo_dir(root)`: `load_stage/save_stage/clear_stage`, `read_head/write_head`,
`load_log/append_commit/head_commit`.

### 2. `src/remarkpush/cli.py` — a `git` Typer sub-app (`app.add_typer(git_app, name="git")`)
Shared helper `_build_working_tree(md_file, papers_dir)` = parse_checklist + build_papers_index
+ resolve_wikilink + sha256_file (reused by `add`/`status`). Commands:
- **`git add [papers…] [-A/--all]`** — build working tree, snapshot selected papers into `stage.json`. Local only. Rich "staged N" table.
- **`git commit -m MSG`** — empty stage → exit; overlay stage onto HEAD; `make_commit` (real `time.time()`); `append_commit` + `write_head` + `clear_stage`. Local only.
- **`git log [-n 20]`** / **`git show [commit=HEAD]`** — read `log.jsonl`; render tables (reuse `_print_*_plan` idiom). Local only.
- **`git status [--offline]`** — working tree vs HEAD vs stage vs (unless offline) device. Independent columns **staged(A) / local(M) / remote(R) / device(on/✗)**; headline precedence: untracked → staged → locally-modified → remote-modified → not-on-device → up-to-date. R = device `version`/`lastModified` ≠ Index baseline.
- **`git push [--dry-run] [--no-restart]`** — push HEAD's manifest. **Reuse `_build_reading_plan` (cli.py:471-515)** refactored to a routing core over resolved `(name,checked,local)` triples (the manifest already *is* those); push/move/noop routing, library-wide `find_document_by_name`, `move_document`, `ensure_folder_path`, `upload_document`, one `restart_xochitl` all reused from the `reading-list` path (cli.py:628-712). **Then one final `read_device()`** to record authoritative post-write `uuid`/`device_version`/`device_last_modified` per touched doc into the Index — this seeds change detection and prevents our own writes (move bumps `lastModified`) from later reading as remote-modified.
- **`git pull [--dry-run]`** — for HEAD's papers, **reuse `_build_annotation_plan` (cli.py:1007-1046)** with an added `changed_only` gate: a paper is a pull candidate only if device `version`/`lastModified` ≠ Index baseline. Fetch flattened render via the existing USB path (`web_interface_up` gate → `download_rendered_pdf` → `<stem>_annotated.pdf`); after each success advance that entry's Index baseline. **Pull does NOT create a commit** (annotated renders are an inbound byproduct, not outbound intent; auto-committing would conflate the two histories).

### 3. `src/remarkpush/index.py` — schema bump (backward-compatible, no migration step)
Append two **defaulted** fields to `Entry` (index.py:33): `device_version: str = ""`,
`device_last_modified: str = ""`. Add the same two as defaulted params to `Index.record`
(existing callers keep working). Harden `load` so `Entry(**v)` filters to known fields
(`{f.name for f in fields(Entry)}`) → old indexes load, future field changes don't crash.

### 4. `src/remarkpush/device.py` — expose the change signal
Add `version: str = ""` to `Item` (device.py:27) and parse it in `build_items` beside the
`last_modified` parse (device.py:98): `version=str(meta.get("version", ""))`.

### 5. Tests — pure, `tmp_path` + synthetic `Item` dicts (mirror `tests/test_sync_annotations.py`)
NEW `tests/test_history.py`: `compute_commit_id` determinism; `merge_stage_into_head`
overlay; round-trips of stage/HEAD/log; `diff_working_tree` matrix (untracked / staged /
staged-stale / local-M / remote-R / both / not-on-device / up-to-date); **migration test**
(legacy 6-key `Entry(**…)` → new fields default `""`; `Index.load` on legacy json).
Extend `tests/test_device.py`: `build_items` parses `version`. Reuse tests: HEAD manifest
through the refactored reading-list core yields identical push/move/noop routing; `git pull`
`changed_only` gate pulls on a bump, skips when unchanged.

### 6. Docs
`CLAUDE.md`: new `history.py` module-index row; update `index.py` row (now stores device
baseline) and `cli.py` exports (`git` sub-app); add a "Tier 2 (local history)" data-flow
block and two invariants ("commit is purely local; only push/pull/non-offline status touch
the device" and "the md is the single editable source of truth; stage/commit/log are derived
snapshots never written back"). Add an execution plan doc to `.docs_claude/plans/active/`.

`device.py`, `reading_list.py`, `usb.py`, `metadata.py`, `ignore.py`, `config.py`: no
changes beyond the two above (`device.py` `version`, `index.py` schema).

## Non-goals
Branches/refs/merge/rebase/cherry-pick; delta/packfile compression; per-page `.rm`
annotation deltas; removal tombstones / push-deletes (prune stays in `reading-list --prune`);
reflog / inbound-annotation history; conflict *resolution* (status surfaces both M and R, but
nothing can be merged inside an opaque annotated PDF); multi-device remote tracking.

## Risks
1. **`version` may not move on a no-cloud device** — `lastModified` is likely the real
   trigger. Mitigation: store both, lean on `lastModified`, validate on first real annotation.
2. **Our own writes bump `lastModified`** (`move_document` → `now_ms`, device.py:270). Mitigation:
   `push` records the *post-write* device baseline (final `read_device()`), so pushes never
   self-trigger remote-modified.
3. **md-vs-stage drift** — working tree can change between `add` and `commit`. Like git, the
   change isn't committed unless re-`add`ed; `status` flags `staged (stale)` and `commit` warns.
4. **Scale** — always use the single whole-library SSH dump (never per-doc SSH); gate annotation
   pulls (sequential USB HTTP) to version-bumped papers only.

## Verification
1. **Unit (no device):** `uv run --directory /Users/jke/repo/remarkpush pytest -q` — new
   `test_history.py` + device/migration tests pass alongside the existing 22.
2. **Offline flow:** `git add -A` → `git status` (papers shown `A`) → `git commit -m x` →
   `git log` / `git show` — all with the tablet unplugged; `.remarkpush/{stage.json,log.jsonl,HEAD}` appear.
3. **Push:** dock device, `remarkpush preflight`, `git push --dry-run` then `git push` — papers
   land in "papers to read"/"papers read"; Index entries gain `device_version`/`device_last_modified`.
4. **Annotate → detect:** mark up a paper on the tablet, `git status` → that paper shows `R`
   (remote-modified); confirms `lastModified`/`version` detection (note which field actually moved).
5. **Pull:** enable USB web interface, `git pull --dry-run` then `git pull` → only the bumped
   paper(s) fetch `<stem>_annotated.pdf` beside the source; re-run `git status` → `R` clears.
6. **No regressions:** existing `push`/`pull`/`status`/`reading-list`/`sync-annotations` behave
   exactly as before (untouched commands).

## Out of scope (future)
`git rm` + removal tombstones; reflog of received annotations; reading `--papers-dir`/md path
from `config.toml`; a combined push-then-pull round-trip; chunk-level dedup of large PDFs.
