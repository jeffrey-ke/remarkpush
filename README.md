# remarkpush

A git/huggingface-style CLI to **push and pull PDFs & EPUBs** to/from a
**reMarkable 2**, readable and annotatable in the stock reMarkable software.

It talks to the device over **SSH** (default), which avoids the reMarkable
cloud's free-tier limits (50 documents, 50-day unsynced drop) and its Connect
subscription entirely — your library is bounded only by the device's storage.

> **Status: push + pull working** (Phases 0–2). `init`, `preflight`, `ls`,
> `status`, `push`, and `pull` (incl. device-rendered `--annotated`) are all
> functional. Phase 3 (cloud backend, bidirectional `sync`) is optional/future.

## Why SSH

| | Cloud (rmapi) | USB web UI | **SSH (this tool)** |
|---|---|---|---|
| 50-doc cap / Connect $ | capped | none | **none** |
| Works over Wi-Fi | yes | cable only | yes |
| Create folders + tags | yes | no | yes |
| Pull originals / raw ink | partial | PDF only | yes |
| Hacking level | none | none | medium |

The reMarkable 2 exposes root SSH out of the box (no "Developer Mode" wipe —
that's only the Paper Pro/Pure). The trade-off: this tool writes document
sidecar files and restarts the device's `xochitl` app, so it's more invasive
than the cloud/USB paths. See [Safety](#safety).

## Install

```sh
uv sync
uv run remarkpush --help
```

## Quick start

```sh
# One-time: configure + verify access. Installs a dedicated SSH key by default
# (asks for the root password once; never stores it).
uv run remarkpush init

# Check the device is ready (run after plugging in).
uv run remarkpush preflight

# List your device library (read-only).
uv run remarkpush ls

# Push a PDF/EPUB (or a whole folder) into a collection.
uv run remarkpush push paper.pdf --to Research
uv run remarkpush push ./papers --to Reading --dry-run   # preview first

# See what's new/modified vs. the device.
uv run remarkpush status

# Pull originals back, or flattened annotated PDFs (device-rendered over USB).
uv run remarkpush pull Research -o ./out
uv run remarkpush pull Research -o ./out --annotated
```

`--annotated` needs the USB web interface on (Settings → Storage) and the
cable connected — it uses xochitl's own renderer for the highest fidelity.

Find the root password on the device under **Settings → Help → Copyright and
licenses**, beneath the *GPLv3 Compliance* header. Connect over USB
(`10.11.99.1`) or Wi-Fi. For Wi-Fi SSH on OS > 3.20, run `rm-ssh-over-wlan on`
once over USB.

If you hit `no matching host key type ... ssh-rsa`, add to `~/.ssh/config`:

```
Host remarkable 10.11.99.1
  HostkeyAlgorithms +ssh-rsa
  PubkeyAcceptedAlgorithms +ssh-rsa
```

## Configuration

Stored once per machine at `~/.config/remarkpush/config.toml`:

```toml
[device]
host = "10.11.99.1"
username = "root"
xochitl_path = "/home/root/.local/share/remarkable/xochitl"
key_path = "~/.ssh/remarkpush_rsa"   # omit to be prompted for a password
```

Per-folder sync state lives in `.remarkpush/` (used from Phase 1).

## Safety

Pushing writes files into the device's `xochitl` store and restarts the app so
changes appear. Design rules this tool follows:

- **Batch, then restart once.** `xochitl` has a strict systemd start-limit;
  restarting per-file can drop the device into its emergency target and reboot
  it. We `systemctl reset-failed` then restart a single time per run.
- **Write complete files, then restart** — never mutate the store while
  `xochitl` is live, to avoid corrupt library entries.
- **Never delete on-device** without an explicit flag.
- OS updates rotate the SSH host key (clear `known_hosts`) and may reset the
  root password (re-read it from Settings).

## Roadmap

- **Phase 0 (done):** `init`, `preflight`, `ls` (read-only).
- **Phase 1 (done):** `push` files/folders, folder + tag creation, local sync
  index, `status` / `--dry-run`, `.rmpushignore`.
- **Phase 2 (done):** `pull` originals; `pull --annotated` via the USB
  device-rendered PDF. (Wi-Fi client-side render with `rmscene`/`remarks` is a
  future fallback.)
- **Phase 3 (optional):** cloud (`rmapi`) backend, bidirectional `sync`,
  thumbnails.

### Known caveats

- **Tags** are written to the sidecar but not yet verified to render on-device.
- **Annotated pull** currently requires the USB cable + web interface (no Wi-Fi
  client-render fallback yet).
- `push --force` re-uploads but doesn't yet remove the prior copy (can duplicate).

## Acknowledgements

Builds on knowledge from the reMarkable hacking community: `rmscene`, `remarks`,
`rmapi`, `remarkable.guide`, and `awesome-reMarkable`.
