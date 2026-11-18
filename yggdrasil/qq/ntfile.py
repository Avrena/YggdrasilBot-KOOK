"""QQ NT media file ids: the ``fileid`` in a download link, Milky's ``resource_id``.

The id is a base64url protobuf whose field 2 is the 20-byte sha1 of the content (checked on 2026-11-17: the sha1 of
a downloaded picture equals it), so it identifies a picture across uploads and protocols.
"""

import base64
from urllib.parse import parse_qs, urlsplit


def sha1_of(file_id: str) -> str:
    """The content sha1 (lowercase hex) in an NT file id; "" when ``file_id`` is not one."""
    try:
        raw = base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4))
    except ValueError:
        return ""
    if len(raw) >= 22 and raw[0] == 0x12 and raw[1] == 20:  # field 2, length-delimited, 20 bytes
        return raw[2:22].hex()
    return ""


def file_id_of(url: str) -> str:
    """The ``fileid`` query parameter of an NT download link, or ""."""
    try:
        return parse_qs(urlsplit(url).query).get("fileid", [""])[0]
    except ValueError:
        return ""
