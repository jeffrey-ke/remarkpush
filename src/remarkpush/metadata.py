"""Builders for the xochitl sidecar files written on push.

Mirrors the minimal shape a *fresh* import needs on current reMarkable OS
(formatVersion 2, empty ``cPages`` — xochitl populates page data on first open).
Verified against the on-device format of an existing PDF.
"""

from __future__ import annotations

import json
import time


def now_ms() -> str:
    return str(int(time.time() * 1000))


def document_metadata(visible_name: str, parent_uuid: str, *, pinned: bool = False) -> str:
    return json.dumps(
        {
            "createdTime": now_ms(),
            "lastModified": now_ms(),
            "lastOpened": "0",
            "lastOpenedPage": 0,
            "new": True,
            "parent": parent_uuid,
            "pinned": pinned,
            "source": "",
            "type": "DocumentType",
            "visibleName": visible_name,
        },
        indent=4,
    )


def folder_metadata(visible_name: str, parent_uuid: str) -> str:
    return json.dumps(
        {
            "createdTime": now_ms(),
            "lastModified": now_ms(),
            "metadatamodified": False,
            "modified": False,
            "parent": parent_uuid,
            "pinned": False,
            "synced": False,
            "type": "CollectionType",
            "version": 0,
            "visibleName": visible_name,
        },
        indent=4,
    )


def document_content(file_type: str, *, tags: list[str] | None = None) -> str:
    content: dict = {
        "cPages": {
            "original": {"timestamp": "1:0", "value": -1},
            "pages": [],
        },
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": file_type,
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "margins": 100,
        "orientation": "portrait",
        "pageCount": 0,
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [{"name": t, "timestamp": "1:1"} for t in (tags or [])],
        "textAlignment": "left",
        "textScale": 1,
    }
    return json.dumps(content, indent=4)


def folder_content() -> str:
    return json.dumps([], indent=4)
