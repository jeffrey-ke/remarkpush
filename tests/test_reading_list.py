from pathlib import Path

from remarkpush.cli import _build_reading_plan, _stale_documents
from remarkpush.device import Item
from remarkpush.reading_list import (
    ChecklistEntry,
    build_papers_index,
    parse_checklist,
    resolve_wikilink,
)

# --------------------------------------------------------------------------- #
# parse_checklist
# --------------------------------------------------------------------------- #
SAMPLE_MD = """\
# Reading list

- [ ] [[mask rcnn.pdf]]
- [x] [[learning without forgetting.pdf]]
* [X] [[Capital.pdf]]
- [ ] [[aliased.pdf|A Nicer Title]]
- [ ] [[paged.pdf#page=3]]
- [ ] not a wikilink, ignore me
just some prose
    - [ ] [[indented.pdf]]
"""


def test_parse_checklist_marks_links_and_state():
    entries = parse_checklist(SAMPLE_MD)
    assert [e.link_name for e in entries] == [
        "mask rcnn.pdf",
        "learning without forgetting.pdf",
        "Capital.pdf",
        "aliased.pdf",          # alias stripped
        "paged.pdf",            # #page=N anchor stripped
        "indented.pdf",         # leading indent allowed
    ]
    assert [e.checked for e in entries] == [False, True, True, False, False, False]


def test_parse_checklist_skips_non_tasks():
    assert parse_checklist("plain text\n[[not a task.pdf]]\n- regular bullet") == []


# --------------------------------------------------------------------------- #
# build_papers_index / resolve_wikilink
# --------------------------------------------------------------------------- #
def test_resolve_is_case_insensitive_with_extension_fallback(tmp_path):
    (tmp_path / "Mask RCNN.pdf").write_bytes(b"x")
    (tmp_path / "notes.epub").write_bytes(b"y")
    idx = build_papers_index(tmp_path)

    assert resolve_wikilink("mask rcnn.pdf", idx).name == "Mask RCNN.pdf"
    assert resolve_wikilink("MASK RCNN.PDF", idx).name == "Mask RCNN.pdf"
    assert resolve_wikilink("notes", idx).name == "notes.epub"   # extension fallback
    assert resolve_wikilink("missing.pdf", idx) is None


def test_build_papers_index_follows_symlinked_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "p.pdf").write_bytes(b"x")
    link = tmp_path / "papers and figures"
    link.symlink_to(real)

    idx = build_papers_index(link)
    assert resolve_wikilink("p.pdf", idx) is not None


# --------------------------------------------------------------------------- #
# _build_reading_plan
# --------------------------------------------------------------------------- #
def _papers(tmp_path, *names):
    for n in names:
        (tmp_path / n).write_bytes(b"pdfbytes")
    return build_papers_index(tmp_path)


def test_reading_plan_push_move_noop_unresolved(tmp_path):
    idx = _papers(tmp_path, "p1.pdf", "p2.pdf", "p3.pdf", "p4.pdf")
    TR, RD = "to-read-uuid", "read-uuid"
    items = {
        TR: Item(TR, "papers to read", "", "CollectionType"),
        RD: Item(RD, "papers read", "", "CollectionType"),
        # p3 already correctly filed under the read folder
        "d3": Item("d3", "p3", RD, "DocumentType", "pdf"),
        # p4 sits in the read folder but the md now lists it as unread -> move back
        "d4": Item("d4", "p4", RD, "DocumentType", "pdf"),
    }
    entries = [
        ChecklistEntry(False, "p1.pdf"),     # not on device, unread -> push to TR
        ChecklistEntry(True, "p2.pdf"),      # not on device, read   -> push to RD
        ChecklistEntry(True, "p3.pdf"),      # in RD already         -> noop
        ChecklistEntry(False, "p4.pdf"),     # in RD, now unread     -> move to TR
        ChecklistEntry(False, "ghost.pdf"),  # no matching file      -> unresolved
    ]
    plan = _build_reading_plan(
        entries, idx, items,
        to_read_uuid=TR, read_uuid=RD,
        to_read_label="papers to read", read_label="papers read",
    )
    by_name = {p.link_name: p for p in plan}

    assert (by_name["p1.pdf"].action, by_name["p1.pdf"].target_uuid) == ("push", TR)
    assert (by_name["p2.pdf"].action, by_name["p2.pdf"].target_uuid) == ("push", RD)
    assert (by_name["p3.pdf"].action, by_name["p3.pdf"].uuid) == ("noop", "d3")
    assert (by_name["p4.pdf"].action, by_name["p4.pdf"].uuid, by_name["p4.pdf"].target_uuid) == ("move", "d4", TR)
    assert by_name["ghost.pdf"].action == "unresolved"


def test_reading_plan_dedup_same_paper_listed_twice(tmp_path):
    idx = _papers(tmp_path, "dup.pdf")
    plan = _build_reading_plan(
        [ChecklistEntry(True, "dup.pdf"), ChecklistEntry(False, "dup.pdf")],
        idx, {},
        to_read_uuid="TR", read_uuid="RD",
        to_read_label="papers to read", read_label="papers read",
    )
    assert len(plan) == 1  # first occurrence wins


def test_reading_plan_moves_copy_from_outside_managed_folders(tmp_path):
    idx = _papers(tmp_path, "filed.pdf")
    items = {"x": Item("x", "filed", "some-other-folder", "DocumentType", "pdf")}
    plan = _build_reading_plan(
        [ChecklistEntry(False, "filed.pdf")],
        idx, items,
        to_read_uuid="TR", read_uuid="RD",
        to_read_label="papers to read", read_label="papers read",
    )
    assert (plan[0].action, plan[0].uuid, plan[0].target_uuid) == ("move", "x", "TR")


# --------------------------------------------------------------------------- #
# _stale_documents
# --------------------------------------------------------------------------- #
def test_stale_documents_only_within_managed_folders():
    TR, RD = "TR", "RD"
    items = {
        "keep": Item("keep", "wanted", RD, "DocumentType", "pdf"),
        "stale": Item("stale", "dropped", TR, "DocumentType", "pdf"),
        "elsewhere": Item("elsewhere", "dropped-but-filed", "other", "DocumentType", "pdf"),
        "trashed": Item("trashed", "old", RD, "DocumentType", "pdf", deleted=True),
        "folder": Item("folder", "papers read", "", "CollectionType"),
    }
    stale = _stale_documents(items, {TR, RD}, {"wanted"})
    assert [s.uuid for s in stale] == ["stale"]  # not "elsewhere" (other folder), not "trashed"
