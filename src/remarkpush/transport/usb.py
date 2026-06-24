"""USB web-interface transport — used only for `pull --annotated`.

xochitl serves an HTTP API at http://10.11.99.1 when "USB web interface" is
enabled (Settings → Storage). Its ``/download/{uuid}/pdf`` endpoint returns a
PDF the *device itself* rendered, with annotations flattened in — the highest
fidelity export, since xochitl's renderer is closed and can't be reproduced.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

DEFAULT_WEB_HOST = "10.11.99.1"


def web_interface_up(host: str = DEFAULT_WEB_HOST, timeout: float = 3.0) -> bool:
    try:
        socket.create_connection((host, 80), timeout=timeout).close()
        return True
    except OSError:
        return False


def download_rendered_pdf(
    uuid: str,
    dest: Path,
    *,
    host: str = DEFAULT_WEB_HOST,
    timeout: float = 300.0,
) -> int:
    """Fetch the device-rendered (annotation-flattened) PDF for a document.

    Returns bytes written. Raises on a non-PDF response (e.g. the web interface
    is off and something else answered)."""
    url = f"http://{host}/download/{uuid}/pdf"
    req = urllib.request.Request(url, headers={"Referer": f"http://{host}/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed host
        data = resp.read()
    if data[:4] != b"%PDF":
        raise RuntimeError(
            f"web interface did not return a PDF for {uuid} "
            f"(got {len(data)} bytes starting {data[:8]!r})"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)
