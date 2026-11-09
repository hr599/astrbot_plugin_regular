"""Fetch the photos of an incoming group message, for captioning (core/captions).

The OneBot image segment carries a download link from QQ's image servers (LLBot:
data.url). Only those hosts are fetched, over http(s), with a size cap and a
timeout; the bytes stay in memory and are dropped once the caption is written.
Stickers are skipped, the same way perceive tells them apart.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

try:  # inside the plugin package
    from ..core.captions import MAX_BYTES
    from ..core.perceive import _is_sticker_image
except ImportError:  # repo root (tests)
    from core.captions import MAX_BYTES
    from core.perceive import _is_sticker_image

QQ_IMAGE_HOSTS = ("qq.com", "qq.com.cn", "qpic.cn")
TIMEOUT_S = 15


def photo_links(raw: dict[str, Any] | None) -> list[tuple[int, str]]:
    """(position among the message's photos, link) for each photo with a QQ link."""
    out: list[tuple[int, str]] = []
    pos = 0
    for seg in (raw or {}).get("message") or []:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        d = seg.get("data") or {}
        if _is_sticker_image(d):
            continue
        link = next((str(d[k]) for k in ("url", "file") if str(d.get(k) or "").startswith(("http://", "https://"))),
                    "")
        if link and allowed(link):
            out.append((pos, link))
        pos += 1
    return out


def allowed(link: str) -> bool:
    host = (urlparse(link).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in QQ_IMAGE_HOSTS)


async def fetch(link: str) -> bytes | None:
    """The image bytes, or None (too big, not reachable, not a QQ image host)."""
    if not allowed(link):
        return None
    try:
        import aiohttp
    except ImportError:  # AstrBot ships aiohttp; without it, no captions
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(link, allow_redirects=False) as r:
                if r.status != 200:
                    return None
                buf = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        return None
                return bytes(buf)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return None


async def fetch_photos(raw: dict[str, Any] | None, limit: int = 4) -> list[tuple[int, bytes]]:
    """Up to `limit` photos of one message, in order."""
    out = []
    for pos, link in photo_links(raw)[:limit]:
        data = await fetch(link)
        if data:
            out.append((pos, data))
    return out
