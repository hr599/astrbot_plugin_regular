"""Turn a message into readable, storable text. No model calls in M0."""

from __future__ import annotations

import re

from .types import ChatEvent, Perceived

# Group text is data, never instructions. These flags only mark messages for the
# trace and, later, for the decide stage; nothing is ever executed because of them.
_INJECTION_PATTERNS = [
    ("ignore_previous", re.compile(r"(ignore|disregard|forget)\s+(all\s+)?(the\s+)?(previous|prior|above)", re.I)),
    ("ignore_previous", re.compile(r"(忽略|无视|忘记|忘掉).{0,6}(之前|前面|上面|以上|所有).{0,6}(指令|设定|规则|提示|要求)")),
    ("system_prompt", re.compile(r"system\s*prompt|系统提示|系统指令|提示词|人设(内容|设定|全文)?")),
    ("role_override", re.compile(r"(you are now|from now on you|act as|pretend to be|developer mode|jailbreak)", re.I)),
    ("role_override", re.compile(r"(你现在是|从现在开始你|扮演|假装你是|开发者模式|越狱)")),
    ("reveal", re.compile(r"(repeat|print|show|reveal|output).{0,20}(instructions|prompt|rules|config)", re.I)),
    ("reveal", re.compile(r"(重复|输出|显示|告诉我|发出来).{0,12}(指令|设定|规则|配置|提示词|人设)")),
    ("reveal", re.compile(r"(指令|设定|规则|配置|提示词|人设).{0,8}(发出来|说出来|贴出来|告诉我|复述|念一遍|原文)")),
    ("fake_role_tag", re.compile(r"</?\s*(system|assistant|instructions?)\s*>|\[\s*(system|SYSTEM)\s*\]")),
]

# The design doc: "/, ! or # followed by letters" is command-like; a message that
# starts with "..." or "。。。" is ordinary chat, so . and 。 are not prefixes.
_COMMAND_PREFIX = re.compile(r"^\s*[/!！#＃]\s*[A-Za-z\u4e00-\u9fff]")

_PLACEHOLDER = {
    "face": "[表情]",
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "shake": "[窗口抖动]",
    "poke": "[戳一戳]",
    "location": "[位置]",
    "music": "[音乐]",
    "share": "[链接]",
    "markdown": "[markdown]",
}


# Image sub types that mean "sticker". 1 = QQ's own sticker flag (OneBot/NapCat docs).
# 7 = a sticker sent from a keyboard/input-method app; undocumented, identified on the
# live account (Nov 8). Anything else stays a photo until we see it.
STICKER_SUBTYPES = {"1", "7"}


def _raw_subtype(d: dict) -> str:
    for key in ("sub_type", "subType"):
        v = d.get(key)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def _is_sticker_image(d: dict) -> bool:
    """QQ marks a sticker sent as an image with a sub type in STICKER_SUBTYPES.

    The key differs by implementation: OneBot/NapCat use `sub_type`, LLBot 8.3.2
    sends `subType` (found on the live account, Nov 8). When neither is present,
    QQ's own summary text for stickers is the fallback.
    """
    for key in ("sub_type", "subType"):
        v = d.get(key)
        if v is not None and str(v).strip() != "":
            return str(v).strip() in STICKER_SUBTYPES
    return str(d.get("summary") or "").strip() in ("[动画表情]", "[表情]")


def injection_flags(text: str) -> list[str]:
    found = []
    for name, pat in _INJECTION_PATTERNS:
        if name not in found and pat.search(text):
            found.append(name)
    return found


def perceive(ev: ChatEvent, *, name_of=None) -> Perceived:
    """name_of(uid) -> display name, used to render @-mentions."""
    parts: list[str] = []
    mentions: list[str] = []
    mentions_bot = False
    reply_to = None
    media: list[str] = []
    image_subtypes: list[str] = []
    for seg in ev.segments:
        k, d = seg.kind, seg.data
        if k == "text":
            parts.append(str(d.get("text", "")))
        elif k == "at":
            qq = str(d.get("qq", ""))
            if qq == "all":
                mentions.append("all")
                parts.append("@全体成员")
            elif qq and qq == ev.self_id:
                mentions_bot = True
                mentions.append("bot")
                parts.append("@我")
            else:
                mentions.append(qq)
                shown = (name_of(qq) if name_of else None) or d.get("name") or qq
                parts.append(f"@{shown}")
        elif k == "reply":
            if d.get("id") is not None:
                reply_to = f"{ev.scope_id}:{d.get('id')}"
        elif k == "mface":
            media.append("sticker")
            summary = str(d.get("summary") or "").strip("[]") or "表情包"
            parts.append(f"[表情包:{summary}]")
        elif k == "image":
            is_sticker = _is_sticker_image(d)
            image_subtypes.append(_raw_subtype(d))
            media.append("sticker" if is_sticker else "image")
            parts.append("[表情包]" if is_sticker else "[图片]")
        elif k == "file":
            media.append("file")
            parts.append(f"[文件:{d.get('name') or d.get('file') or ''}]")
        else:
            if k in ("record", "video"):
                media.append("voice" if k == "record" else "video")
            elif k == "face":
                media.append("face")
            parts.append(_PLACEHOLDER.get(k, f"[{k}]"))
    text = "".join(parts).strip()
    return Perceived(
        text=text,
        reply_to=reply_to,
        reply_to_bot=bool(reply_to) and bool(ev.self_id) and ev.reply_sender_uid == ev.self_id,
        mentions=mentions,
        mentions_bot=mentions_bot,
        is_command_like=bool(_COMMAND_PREFIX.match(ev.raw_text)),
        injection_flags=injection_flags(text),
        media_kinds=media,
        image_subtypes=image_subtypes,
    )
