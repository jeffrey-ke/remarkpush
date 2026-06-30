from pathlib import Path

from remarkpush.cli import _AnnotationItem, _build_annotation_plan
from remarkpush.device import Item
from remarkpush.reading_list import ChecklistEntry, build_papers_index

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _papers(tmp_path, *names):
    for n in names:
        (tmp_path / n).write_bytes(b"pdfbytes")
    return build_papers_index(tmp_path)


def _plan(entries, idx, items, **kw):
    opts = dict(checked_only=False, suffix="_annotated", out_dir=None, skip_existing=False)
    opts.update(kw)
    return _build_annotation_plan(entries, idx, items, **opts)


# --------------------------------------------------------------------------- #
# _build_annotation_plan
# --------------------------------------------------------------------------- #
def test_pull_resolved_and_on_device(tmp_path):
    idx = _papers(tmp_path, "p1.pdf")
    items = {"d1": Item("d1", "p1", "some-folder", "DocumentType", "pdf")}
    plan = _plan([ChecklistEntry(False, "p1.pdf")], idx, items)
    assert len(plan) == 1
    p = plan[0]
    assert p.action == "pull"
    assert p.uuid == "d1"
    assert p.dest == tmp_path / "p1_annotated.pdf"


def test_resolved_but_not_on_device(tmp_path):
    idx = _papers(tmp_path, "p1.pdf")
    plan = _plan([ChecklistEntry(True, "p1.pdf")], idx, {})
    assert (plan[0].action, plan[0].uuid, plan[0].dest) == ("not-on-device", "", None)


def test_unresolved_wikilink(tmp_path):
    idx = _papers(tmp_path, "p1.pdf")
    plan = _plan([ChecklistEntry(False, "ghost.pdf")], idx, {})
    assert (plan[0].action, plan[0].local, plan[0].dest) == ("unresolved", None, None)


def test_checked_only_filters_unread(tmp_path):
    idx = _papers(tmp_path, "read.pdf", "unread.pdf")
    items = {
        "dr": Item("dr", "read", "f", "DocumentType", "pdf"),
        "du": Item("du", "unread", "f", "DocumentType", "pdf"),
    }
    plan = _plan(
        [ChecklistEntry(True, "read.pdf"), ChecklistEntry(False, "unread.pdf")],
        idx, items, checked_only=True,
    )
    by_name = {p.name: p for p in plan}
    assert by_name["read"].action == "pull"
    assert by_name["unread"].action == "skip-unchecked"
    assert by_name["unread"].dest is None


def test_skip_existing_when_dest_present(tmp_path):
    idx = _papers(tmp_path, "p1.pdf")
    (tmp_path / "p1_annotated.pdf").write_bytes(b"old")  # pretend a prior pull
    items = {"d1": Item("d1", "p1", "f", "DocumentType", "pdf")}
    plan = _plan([ChecklistEntry(False, "p1.pdf")], idx, items, skip_existing=True)
    assert plan[0].action == "skip-existing"
    # without skip_existing it would overwrite (refresh annotations)
    plan2 = _plan([ChecklistEntry(False, "p1.pdf")], idx, items, skip_existing=False)
    assert plan2[0].action == "pull"


def test_out_dir_override_and_custom_suffix(tmp_path):
    idx = _papers(tmp_path, "p1.pdf")
    items = {"d1": Item("d1", "p1", "f", "DocumentType", "pdf")}
    other = tmp_path / "elsewhere"
    plan = _plan([ChecklistEntry(False, "p1.pdf")], idx, items, out_dir=other, suffix=" annotated")
    assert plan[0].dest == other / "p1 annotated.pdf"


def test_dedup_same_paper_listed_twice(tmp_path):
    idx = _papers(tmp_path, "dup.pdf")
    items = {"d": Item("d", "dup", "f", "DocumentType", "pdf")}
    plan = _plan([ChecklistEntry(True, "dup.pdf"), ChecklistEntry(False, "dup.pdf")], idx, items)
    assert len(plan) == 1  # first occurrence wins


def test_case_insensitive_device_lookup(tmp_path):
    idx = _papers(tmp_path, "Mask RCNN.pdf")
    items = {"d": Item("d", "mask rcnn", "f", "DocumentType", "pdf")}  # device name lower-cased
    plan = _plan([ChecklistEntry(True, "MASK RCNN.PDF")], idx, items)
    assert plan[0].action == "pull"
    assert plan[0].dest == tmp_path / "Mask RCNN_annotated.pdf"  # canonical local stem preserved
