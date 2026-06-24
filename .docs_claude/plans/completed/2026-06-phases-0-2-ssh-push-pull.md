## Keywords / Tags
- remarkpush
- remarkable-2
- ssh
- xochitl
- push
- pull
- annotated-pdf
- usb-web-interface
- sync-index
- plan-completed
- architecture
- gotchas

# Completed Plan — remarkpush Phases 0–2 (SSH push/pull)

Status: **DONE & verified on a real reMarkable 2.** Implemented 2026-06-24.

## Goal

A git/huggingface-style CLI to push and pull PDFs & EPUBs to/from a reMarkable 2,
readable/annotatable in the **stock** software, with a "specify files/folders,
sync them" philosophy and a local index for incremental operations.

## Approach decided (signed off)

- **Transport: SSH-first** (paramiko, pure SFTP — no `rsync` install to be wiped
  by OS updates). Chosen over the cloud and USB-web because it clears the cloud's
  **50-document free-tier cap / Connect subscription** and the USB interface's
  cable-only + no-folder-creation limits, while keeping the stock reader. The USB
  web interface is used *only* for the high-fidelity annotated pull.
- **Language: Python** (matches the user's stack), packaged with `uv` + hatchling,
  `typer` CLI, `rich` output.
- **Sync model:** stateless device + a local **sync index** (`.remarkpush/index.json`,
  sha256 + device UUID) for idempotent/incremental push — the git-index analogue.
- **Safety rails:** atomic `.part`→rename upload; write complete sidecars then
  restart xochitl **once** per push, guarded by `systemctl reset-failed`.

### Phased delivery (all complete)

- **Phase 0:** `init` (configure + install SSH key, password used once/never stored),
  `ls` (read-only tree), device model, SSH transport. + `preflight` (added).
- **Phase 1:** `push` files/folders → collections (+tags), local index, `status`,
  `--dry-run`/`--force`, `.rmpushignore`.
- **Phase 2:** `pull` originals (SFTP); `pull --annotated` (device-rendered flat PDF
  via the USB web interface).
- **Phase 3 (deferred/optional):** cloud (`rmapi`) backend, bidirectional `sync`.

## Discoveries & modifications while implementing

1. **SSH `run` deadlock (root cause of an early hang).** The library's
   metadata+content dump is **2,318,146 bytes**, just over paramiko's **2,097,152 B**
   (2 MB) channel window. Calling `recv_exit_status()` before draining stdout
   deadlocks: the remote blocks on write, never exits. Fix: drain stdout/stderr as
   they stream, with a wall-clock timeout. (`transport/ssh.py`.)
2. **Device runs Dropbear `2025.88`** (modern) — so the old `ssh-rsa` host-key
   workaround is **not** needed; paramiko negotiates curve25519 cleanly. Hint text
   kept as conditional only.
3. **Fresh-import sidecar format (current OS = `formatVersion 2`).** A *fresh* PDF
   needs only a **minimal** `.content` (empty `cPages`, `pageCount 0`); xochitl
   populates the CRDT page tree on first open. Confirmed by reading an existing
   PDF's sidecars on-device and cross-checking a tool verified on current hardware.
   We deliberately **do not** hand-author the page CRDT. Doc =
   `{uuid}.pdf|.epub` + `.metadata` + `.content` + empty `{uuid}/` + empty `.pagedata`;
   Folder = `.metadata` + `.content == []` + `.pagedata "\n"`.
4. **Annotated pull is highest-fidelity via the device's own renderer.** The cloud
   gives no server-side flattened PDF and `rmapi geta` is one-pen-only; over pure SSH
   you'd need client-side `rmscene`/`remarks`. The USB web interface
   `GET /download/{uuid}/pdf` returns xochitl's own flattened render — so Phase 2
   uses that (requires the cable + "USB web interface" toggle). Wi-Fi client-render
   is a reserved `[annotate]` extra / future fallback.
5. **`preflight` added** at user request: reachability, ssh auth, store path, write
   access (temp file), free space, xochitl service, library read — one ✓/✗ report.
6. **Name sanitization.** Real libraries have `/` in document names (e.g. `1/21`,
   `Math 5/5`). On pull, `/` → U+2215 (∕) so no phantom local directories.
7. **xochitl restart guard is real, not theoretical.** xochitl's systemd start-limit
   can drop the device to its emergency target and reboot it; we `reset-failed`
   before a single restart per push.

## Verification (on device)

- `ls`: 153 documents / 10 folders, ~2 s.
- `preflight`: all ✓ (5.80 GiB free, xochitl active).
- `push`: pushed a real 1.4 MB PDF to root; opened/annotated in the stock reader.
- `pull`: 4 ArgMin originals (SFTP) + 5 device-rendered PDFs — all valid `%PDF`.
- 6 unit tests pass (`parse_dump`, `build_items`, `children_map`, `sanitize_name`,
  `folder_path_of`, `documents_under`).

## Known caveats / follow-ups

- **Tags** are written to the sidecar but not yet visually confirmed on-device.
- **Annotated pull** needs the USB cable + web interface (no Wi-Fi client-render yet).
- **`push --force`** re-uploads but does not remove the prior copy (can duplicate);
  add delete-then-replace.
- Multi-file/folder push is implemented but was exercised live only with a single
  file and the ArgMin folder pull.

## Pointers

- Architecture & module map: top-level `CLAUDE.md`.
- Sidecar reference format: read an existing doc's `.metadata`/`.content` over SSH.
