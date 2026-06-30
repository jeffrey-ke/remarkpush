import json

from remarkpush import history
from remarkpush.device import Item
from remarkpush.history import (
    Commit,
    ManifestEntry,
    compute_commit_id,
    diff_working_tree,
    headline,
    is_remote_modified,
    make_commit,
    merge_stage_into_head,
)
from remarkpush.index import Entry, Index

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def me(sha, checked=False, path="/papers/x.pdf"):
    return ManifestEntry(sha256=sha, checked=checked, local_path=path)


def _entry(**kw):
    base = dict(
        sha256="s", uuid="u", visible_name="n", parent_uuid="p", size=1, pushed_at="0"
    )
    base.update(kw)
    return Entry(**base)


# --------------------------------------------------------------------------- #
# compute_commit_id / make_commit
# --------------------------------------------------------------------------- #
def test_commit_id_is_deterministic_and_sensitive():
    m = {"a": me("sha-a"), "b": me("sha-b", checked=True)}
    base = compute_commit_id("parent", "1000", "msg", m)
    assert base == compute_commit_id("parent", "1000", "msg", dict(reversed(list(m.items()))))  # order-independent
    assert len(base) == 12
    assert base != compute_commit_id("OTHER", "1000", "msg", m)        # parent matters
    assert base != compute_commit_id("parent", "1001", "msg", m)        # time matters
    assert base != compute_commit_id("parent", "1000", "msg2", m)       # message matters
    assert base != compute_commit_id("parent", "1000", "msg", {"a": me("sha-a")})  # manifest matters


def test_make_commit_uses_injected_now():
    c = make_commit("", {"a": me("sha-a")}, "first", now=1700.9)
    assert c.created_at == "1700"  # truncated to int seconds
    assert c.parent == ""
    assert c.id == compute_commit_id("", "1700", "first", {"a": me("sha-a")})


# --------------------------------------------------------------------------- #
# merge_stage_into_head
# --------------------------------------------------------------------------- #
def test_merge_overlays_stage_onto_head():
    head = {"a": me("old-a"), "b": me("b")}
    staged = {"a": me("new-a"), "c": me("c")}
    merged = merge_stage_into_head(head, staged)
    assert merged["a"].sha256 == "new-a"  # staged replaces
    assert merged["b"].sha256 == "b"      # untouched carried forward
    assert merged["c"].sha256 == "c"      # new added
    assert set(merged) == {"a", "b", "c"}
    assert head["a"].sha256 == "old-a"    # inputs untouched


# --------------------------------------------------------------------------- #
# is_remote_modified
# --------------------------------------------------------------------------- #
def _item(version="", last_modified=""):
    return Item("u", "n", "p", "DocumentType", "pdf", last_modified=last_modified, version=version)


def test_remote_modified_prefers_version_then_falls_back_to_lastmodified():
    # version present on both sides -> authoritative
    assert is_remote_modified(_item(version="3"), _entry(device_version="2", device_last_modified="x")) is True
    assert is_remote_modified(_item(version="2"), _entry(device_version="2", device_last_modified="x")) is False
    # no version (fresh upload) -> fall back to lastModified
    assert is_remote_modified(_item(last_modified="200"), _entry(device_last_modified="100")) is True
    assert is_remote_modified(_item(last_modified="100"), _entry(device_last_modified="100")) is False


def test_remote_modified_false_without_baseline():
    assert is_remote_modified(_item(version="9", last_modified="9"), None) is False
    # legacy entry with empty baseline -> cannot tell -> not flagged
    assert is_remote_modified(_item(version="9", last_modified="9"), _entry()) is False


# --------------------------------------------------------------------------- #
# diff_working_tree + headline
# --------------------------------------------------------------------------- #
def test_diff_matrix():
    working = {
        "untracked": me("u"),
        "staged": me("s2"),
        "stale": me("now"),        # working moved past the staged snapshot
        "localmod": me("changed"),
        "uptodate": me("same"),
        # "gone" intentionally absent from working
    }
    head = {
        "stale": me("h"),
        "localmod": me("orig"),
        "uptodate": me("same"),
        "gone": me("g"),
    }
    staged = {
        "staged": me("s2"),
        "stale": me("before"),     # staged snapshot differs from working -> stale
    }
    rows = {r.name: r for r in diff_working_tree(working, head, staged)}

    assert rows["untracked"].untracked and not rows["untracked"].tracked
    assert rows["staged"].staged and not rows["staged"].stale
    assert rows["stale"].staged and rows["stale"].stale
    assert rows["localmod"].local_mod and not rows["localmod"].staged
    assert rows["gone"].gone_local and not rows["gone"].untracked
    r = rows["uptodate"]
    assert not (r.untracked or r.staged or r.local_mod or r.gone_local)


def test_headline_precedence():
    def row(**kw):
        base = dict(name="x", tracked=True, staged=False, stale=False,
                    local_mod=False, untracked=False, gone_local=False)
        base.update(kw)
        return history.StatusRow(**base)

    assert headline(row(untracked=True, tracked=False), on_device=False, remote_mod=False) == "untracked"
    assert headline(row(staged=True), on_device=True, remote_mod=True) == "staged"          # staged beats remote
    assert headline(row(staged=True, stale=True), on_device=True, remote_mod=False) == "staged-stale"
    assert headline(row(local_mod=True), on_device=True, remote_mod=True) == "locally-modified"  # local beats remote
    assert headline(row(), on_device=True, remote_mod=True) == "remote-modified"
    assert headline(row(), on_device=False, remote_mod=False) == "not-on-device"
    assert headline(row(gone_local=True), on_device=True, remote_mod=False) == "gone-from-md"
    assert headline(row(), on_device=True, remote_mod=False) == "up-to-date"
    # device not read (offline): don't assert a device-derived verdict
    assert headline(row(), on_device=None, remote_mod=False) == "tracked"
    assert headline(row(untracked=True, tracked=False), on_device=None, remote_mod=False) == "untracked"


# --------------------------------------------------------------------------- #
# on-disk round-trips (stage / HEAD / log)
# --------------------------------------------------------------------------- #
def test_stage_roundtrip_and_clear(tmp_path):
    staged = {"a": me("sha-a", checked=True, path="/p/a.pdf"), "b": me("sha-b")}
    history.save_stage(tmp_path, staged)
    loaded = history.load_stage(tmp_path)
    assert loaded == staged
    history.clear_stage(tmp_path)
    assert history.load_stage(tmp_path) == {}


def test_head_and_log_roundtrip(tmp_path):
    assert history.read_head(tmp_path) == ""
    assert history.head_commit(tmp_path) is None

    c1 = make_commit("", {"a": me("a")}, "first", now=1000)
    history.append_commit(tmp_path, c1)
    history.write_head(tmp_path, c1.id)

    c2 = make_commit(c1.id, {"a": me("a"), "b": me("b")}, "second", now=1001)
    history.append_commit(tmp_path, c2)
    history.write_head(tmp_path, c2.id)

    log = history.load_log(tmp_path)
    assert [c.id for c in log] == [c1.id, c2.id]           # write order, oldest first
    assert log[1].manifest["b"].sha256 == "b"              # manifest survives the round-trip
    assert history.read_head(tmp_path) == c2.id
    assert history.head_commit(tmp_path).id == c2.id


def test_find_commit_by_exact_and_prefix(tmp_path):
    c = make_commit("", {"a": me("a")}, "only", now=1000)
    history.append_commit(tmp_path, c)
    assert history.find_commit(tmp_path, c.id).id == c.id
    assert history.find_commit(tmp_path, c.id[:6]).id == c.id   # unique prefix
    assert history.find_commit(tmp_path, "deadbeef") is None    # absent


# --------------------------------------------------------------------------- #
# Index schema migration (the device-baseline fields are backward-compatible)
# --------------------------------------------------------------------------- #
def test_entry_defaults_for_legacy_keys():
    legacy = dict(sha256="s", uuid="u", visible_name="n", parent_uuid="p", size=3, pushed_at="9")
    e = Entry(**legacy)
    assert e.device_version == "" and e.device_last_modified == ""


def test_index_load_tolerates_legacy_and_surplus_keys(tmp_path):
    rdir = tmp_path / ".remarkpush"
    rdir.mkdir()
    payload = {
        "entries": {
            "/papers/legacy.pdf": {  # six legacy keys only
                "sha256": "s", "uuid": "u", "visible_name": "n",
                "parent_uuid": "p", "size": 3, "pushed_at": "9",
            },
            "/papers/futuristic.pdf": {  # a surplus key from some future version
                "sha256": "s2", "uuid": "u2", "visible_name": "n2",
                "parent_uuid": "p2", "size": 4, "pushed_at": "10",
                "device_version": "7", "device_last_modified": "123", "unknown_future": "drop me",
            },
        }
    }
    (rdir / "index.json").write_text(json.dumps(payload), encoding="utf-8")

    idx = Index.load(tmp_path)
    legacy = idx.entries["/papers/legacy.pdf"]
    assert (legacy.device_version, legacy.device_last_modified) == ("", "")  # defaulted
    future = idx.entries["/papers/futuristic.pdf"]
    assert (future.device_version, future.device_last_modified) == ("7", "123")  # surplus key dropped, known kept
