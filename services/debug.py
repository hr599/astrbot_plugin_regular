"""Raw-event dumps and redacted debug bundles.

Raw dumps keep full payloads in the data folder (local only) so we can see
what LLBot actually sends. Bundles are what you attach to a chat with Claude:
IDs are replaced by stable short hashes and all message text is masked, so a
bundle shows event *shapes* without anyone's words or QQ numbers.
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path
from typing import Any

ID_KEYS = {
    "user_id", "group_id", "self_id", "target_id", "operator_id", "sender_id",
    "qq", "uin", "id", "message_id", "message_seq", "real_id", "real_seq", "peer_id",
}
TEXT_KEYS = {
    "text", "nickname", "card", "title", "name", "summary", "content", "raw_message",
    "url", "file", "path", "file_id", "file_unique", "group_name", "remark", "data_str",
}
_SALT = "regular-debug"


def short_hash(v: Any) -> str:
    return "h" + hashlib.sha256(f"{_SALT}:{v}".encode()).hexdigest()[:10]


def redact(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if key in ID_KEYS and obj not in (None, "", 0, "all"):
        return short_hash(obj)
    if key in TEXT_KEYS and isinstance(obj, str) and obj:
        return f"<{len(obj)} chars>"
    if isinstance(obj, str) and len(obj) > 40:
        return f"<{len(obj)} chars>"
    return obj


def redact_scope(scope_id: str) -> str:
    """aiocqhttp:GroupMessage:123 -> aiocqhttp:GroupMessage:h…"""
    head, _, tail = scope_id.rpartition(":")
    return f"{head}:{short_hash(tail)}" if head else short_hash(scope_id)


class RawDumper:
    """Appends raw payloads as JSON lines, one file per day, while enabled."""

    def __init__(self, folder: Path, enabled: bool = False, keep_days: int = 3):
        self.folder = folder
        self.enabled = enabled
        self.keep_days = keep_days
        folder.mkdir(parents=True, exist_ok=True)

    def write(self, payload: Any, note: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        day = time.strftime("%Y%m%d")
        line = json.dumps(
            {"at": int(time.time()), "note": note or {}, "raw": payload},
            ensure_ascii=False, default=str,
        )
        with open(self.folder / f"raw-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def prune(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        for p in self.folder.glob("raw-*.jsonl"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    def tail(self, n: int = 300) -> list[dict[str, Any]]:
        files = sorted(self.folder.glob("raw-*.jsonl"))
        lines: list[str] = []
        for p in reversed(files):
            lines = p.read_text(encoding="utf-8").splitlines() + lines
            if len(lines) >= n:
                break
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


def build_bundle(
    out_dir: Path,
    *,
    status: dict[str, Any],
    traces: list[dict[str, Any]],
    raw_events: list[dict[str, Any]],
    log_lines: list[str] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"regular-debug-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    safe_traces = []
    for t in traces:
        t = dict(t)
        t["scope_id"] = redact_scope(str(t.get("scope_id", "")))
        if t.get("msg_id"):
            t["msg_id"] = short_hash(t["msg_id"])
        try:
            t["reasons"] = redact(json.loads(t.get("reasons") or "{}"))
        except (TypeError, json.JSONDecodeError):
            t["reasons"] = {}
        safe_traces.append(t)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", (
            "Redacted debug bundle from astrbot_plugin_regular.\n"
            "IDs are salted hashes; message text is replaced by its length.\n"
            "Check it before sharing if you want to be sure.\n"
        ))
        z.writestr("status.json", json.dumps(redact(status), ensure_ascii=False, indent=2))
        z.writestr("traces.json", json.dumps(safe_traces, ensure_ascii=False, indent=1))
        z.writestr("raw_events.json", json.dumps(redact(raw_events), ensure_ascii=False, indent=1, default=str))
        if log_lines:
            z.writestr("log.txt", "\n".join(log_lines))
    return path
