"""Raw-event dumps and redacted debug bundles.

Raw dumps keep full payloads in the data folder (local only) so we can see
what LLBot actually sends. Bundles are what you attach to a chat with Claude:
IDs are replaced by stable short hashes and all message text is masked, so a
bundle shows event *shapes* without anyone's words or QQ numbers.
"""

from __future__ import annotations

import hashlib
import os
import json
import time
import zipfile
from pathlib import Path
from typing import Any

ID_KEYS = {
    "user_id", "group_id", "self_id", "target_id", "operator_id", "sender_id",
    "qq", "uin", "id", "message_id", "message_seq", "real_id", "real_seq", "peer_id",
    "msg_id",
}
# Allowlist: only these keys keep their values as-is. They describe the event's
# *shape* (types, flags, timings) or are our own trace fields. Everything else
# is masked whatever its length, because OneBot nests serialized payloads (json,
# xml, markdown segments) under generic keys like "data" or "content".
SAFE_KEYS = {
    # OneBot structure
    "post_type", "message_type", "notice_type", "sub_type", "subType", "request_type",
    "meta_event_type", "type", "time", "font", "message_format", "emoji_id",
    "count", "face_type", "duration", "file_size", "role", "level",
    # our trace / dump notes
    "at", "action", "gate", "notice", "stored", "mentions_bot", "replied_to",
    "command_like", "injection_flags", "media", "m0", "mode", "shadow", "would", "addressed",
    "address_kind", "signals", "policy", "recalled_found",
    "poked_bot", "reply_found", "reply_to_bot", "image_subtypes", "ended_at", "decision", "harness_version", "trace_id", "ts",
}
_SALT = "regular-debug"


def short_hash(v: Any) -> str:
    return "h" + hashlib.sha256(f"{_SALT}:{v}".encode()).hexdigest()[:10]


def redact(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, key) for v in obj]
    if obj is None or isinstance(obj, bool):
        return obj
    if key in SAFE_KEYS and isinstance(obj, (int, float, str)) and len(str(obj)) <= 64:
        return obj
    if key in ID_KEYS or isinstance(obj, (int, float)):
        # unknown numbers may be QQ numbers under a key we didn't foresee
        return short_hash(obj) if obj not in ("", "all") else obj
    if isinstance(obj, str):
        return f"<{len(obj)} chars>" if obj else ""
    return f"<{type(obj).__name__}>"


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

    def forget(self, uid: str) -> int:
        """/reg forget: drop this person's own events from the local raw dumps.
        Returns lines removed. (Other people's events that quote them keep the quote
        AstrBot attached; dumps are off by default and pruned after a few days.)"""
        uid, removed = str(uid), 0
        for p in sorted(self.folder.glob("raw-*.jsonl")):
            keep = []
            lines = p.read_text(encoding="utf-8").splitlines()
            before = len(lines)
            for ln in lines:
                try:
                    raw = json.loads(ln).get("raw") or {}
                except json.JSONDecodeError:
                    keep.append(ln)
                    continue
                sender = raw.get("sender") if isinstance(raw, dict) else None
                ids = {str(raw.get(k)) for k in ("user_id", "operator_id")} if isinstance(raw, dict) else set()
                if isinstance(sender, dict):
                    ids.add(str(sender.get("user_id")))
                if uid in ids:
                    removed += 1
                else:
                    keep.append(ln)
            if len(keep) == before:
                continue  # nothing of theirs in this file: leave it untouched
            # keep the file's original time: retention prunes by mtime, so a rewrite
            # must not give everyone else's old events a fresh lease (PR #12 review)
            st = p.stat()
            p.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
            os.utime(p, (st.st_atime, st.st_mtime))
        return removed

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
    bench: dict[str, Any] | None = None,
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
        # status is built by the plugin from counts and settings; no chat data in it
        z.writestr("status.json", json.dumps(status, ensure_ascii=False, indent=2, default=str))
        z.writestr("traces.json", json.dumps(safe_traces, ensure_ascii=False, indent=1))
        z.writestr("raw_events.json", json.dumps(redact(raw_events), ensure_ascii=False, indent=1, default=str))
        if bench:
            # numbers only (built by bench.commands.latest_summary): no text, no ids
            z.writestr("bench.json", json.dumps(bench, ensure_ascii=False, indent=1))
        if log_lines:
            z.writestr("log.txt", "\n".join(log_lines))
    return path
