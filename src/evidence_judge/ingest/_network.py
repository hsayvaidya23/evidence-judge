"""Convert HAR 1.2 entries (also used inside Playwright traces) into NetworkEvents."""

from __future__ import annotations

import base64
import binascii
import codecs
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote_plus

from evidence_judge.core.evidence import NetworkEvent
from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str

BodySource = Callable[[JsonObject], bytes | str | None]
"""Returns the raw body of a HAR ``postData`` or ``content`` object, or None if absent."""

_TEXT_MIME_MARKERS = (
    "json",
    "xml",
    "javascript",
    "ecmascript",
    "x-www-form-urlencoded",
    "graphql",
    "csv",
    "yaml",
)
_BINARY_MIME_PREFIXES = ("image/", "audio/", "video/", "font/")
_BINARY_MIME_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/wasm",
        "application/protobuf",
        "application/x-protobuf",
        "application/font-woff",
        "application/vnd.ms-fontobject",
    }
)


def check_max_body_chars(max_body_chars: int) -> None:
    if max_body_chars < 0:
        raise ValueError(f"max_body_chars must be >= 0, got {max_body_chars}")


def inline_body(part: JsonObject) -> bytes | str | None:
    """Body stored inline in a HAR object, base64-decoded when the HAR says so."""
    text = as_str(part.get("text"))
    if text is None:
        return None
    if part.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return None
    return text


def network_event(
    entry: JsonObject,
    *,
    event_id: str,
    ts: float,
    max_body_chars: int,
    body_source: BodySource,
) -> NetworkEvent | None:
    """Build a NetworkEvent from a HAR entry, or None if it has no method or URL."""
    request = as_object(entry.get("request"))
    response = as_object(entry.get("response"))
    method = as_str(request.get("method"))
    url = as_str(request.get("url"))
    if not method or not url:
        return None
    status = as_int(response.get("status"))
    failed = status is None or status <= 0 or "_failureText" in response
    post_data = as_object(request.get("postData"))
    content = as_object(response.get("content"))
    response_mime = as_str(content.get("mimeType")) or None
    return NetworkEvent(
        id=event_id,
        ts=ts,
        method=method.upper(),
        url=url,
        status=status if status is not None and status > 0 else None,
        resource_type=as_str(entry.get("_resourceType")),
        request_headers=headers_dict(request.get("headers")),
        request_body=_request_body(post_data, body_source, max_body_chars),
        response_headers=headers_dict(response.get("headers")),
        response_body=decode_body(body_source(content), response_mime, max_body_chars)
        if content
        else None,
        mime_type=response_mime,
        failed=failed,
    )


def wall_ms(timestamp: str | None) -> float | None:
    """Epoch milliseconds of an ISO 8601 HAR timestamp, or None if absent or invalid."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp() * 1000
    except ValueError:
        return None


def headers_dict(raw: object) -> dict[str, str]:
    """HAR header list to a dict with lower-cased names; repeated headers are comma-joined."""
    out: dict[str, str] = {}
    for item in as_list(raw):
        header = as_object(item)
        name = as_str(header.get("name"))
        value = as_str(header.get("value"))
        if name is None or value is None:
            continue
        key = name.lower()
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


def decode_body(raw: bytes | str | None, mime_type: str | None, max_chars: int) -> str | None:
    """Text of a body, truncated to max_chars; None for absent, empty or binary bodies."""
    if raw is None:
        return None
    textual = _is_textual(mime_type)
    if textual is False:
        return None
    text = raw if isinstance(raw, str) else _decode_bytes(raw, mime_type, strict=textual is None)
    if not text:
        return None
    return truncate(text, max_chars)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…[truncated {len(text) - max_chars} chars]"


def _request_body(post_data: JsonObject, body_source: BodySource, max_chars: int) -> str | None:
    if not post_data:
        return None
    mime = as_str(post_data.get("mimeType")) or None
    body = decode_body(body_source(post_data), mime, max_chars)
    if body is not None:
        return body
    params = [as_object(p) for p in as_list(post_data.get("params"))]
    pairs = [
        f"{quote_plus(name)}={quote_plus(as_str(p.get('value')) or '')}"
        for p in params
        if (name := as_str(p.get("name")))
    ]
    return truncate("&".join(pairs), max_chars) if pairs else None


def _is_textual(mime_type: str | None) -> bool | None:
    """True for text mime types, False for binary ones, None when unknown."""
    if not mime_type:
        return None
    essence = mime_type.split(";", 1)[0].strip().lower()
    if essence.startswith("text/") or any(m in essence for m in _TEXT_MIME_MARKERS):
        return True
    if essence.startswith(_BINARY_MIME_PREFIXES) or essence in _BINARY_MIME_TYPES:
        return False
    return None


def _charset(mime_type: str | None) -> str:
    for param in (mime_type or "").split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return "utf-8"


def _decode_bytes(raw: bytes, mime_type: str | None, *, strict: bool) -> str | None:
    """Decode bytes as text. In strict mode (unknown mime type) undecodable bytes mean binary."""
    if b"\x00" in raw[:1024]:
        return None
    try:
        codec = codecs.lookup(_charset(mime_type)).name
    except LookupError:
        codec = "utf-8"
    try:
        return raw.decode(codec, errors="strict" if strict else "replace")
    except UnicodeDecodeError:
        return None
