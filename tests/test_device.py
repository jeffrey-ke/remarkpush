from remarkpush.device import MARKER, build_items, children_map, parse_dump

SAMPLE = f"""
{MARKER}aaa.metadata
{{"visibleName": "Books", "type": "CollectionType", "parent": ""}}
{MARKER}bbb.metadata
{{"visibleName": "My Paper", "type": "DocumentType", "parent": "aaa", "lastModified": "1700000000000"}}
{MARKER}bbb.content
{{"fileType": "pdf"}}
{MARKER}ccc.metadata
{{"visibleName": "Old Note", "type": "DocumentType", "parent": "trash"}}
{MARKER}ddd.metadata
{{"visibleName": "Gone", "type": "DocumentType", "parent": "", "deleted": true}}
"""


def test_parse_and_build():
    metadata, content = parse_dump(SAMPLE)
    items = build_items(metadata, content)

    assert set(items) == {"aaa", "bbb", "ccc", "ddd"}
    assert items["aaa"].is_folder
    assert items["bbb"].is_document
    assert items["bbb"].file_type == "pdf"
    assert items["bbb"].parent == "aaa"
    assert items["ddd"].deleted is True


def test_children_map_skips_deleted_and_trash():
    items = build_items(*parse_dump(SAMPLE))

    visible = children_map(items, include_trash=False)
    assert [i.uuid for i in visible[""]] == ["aaa"]  # ddd (deleted) excluded
    assert [i.uuid for i in visible["aaa"]] == ["bbb"]
    assert "trash" not in visible

    with_trash = children_map(items, include_trash=True)
    assert [i.uuid for i in with_trash["trash"]] == ["ccc"]


def test_parse_empty():
    assert parse_dump("") == ({}, {})
    assert parse_dump("no markers here") == ({}, {})
