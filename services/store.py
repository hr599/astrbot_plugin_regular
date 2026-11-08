"""SQLite store. One file in the plugin's data folder; never committed.

Schema lives in schema.sql (tested on SQLite 3.53). Versioned with PRAGMA
user_version so later milestones can migrate forward.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

try:  # inside the plugin package
    from ..core.perceive import is_command_like as _is_command_like
except ImportError:  # repo root on sys.path (tests, CLI)
    from core.perceive import is_command_like as _is_command_like

SCHEMA_VERSION = 2

# v2 (M1): your labels for bench moments (kept apart from transcripts; a label goes
# when its message is purged or recalled, unless it was saved into a recording), and
# messages.reply_to_bot, because the bot's own messages are never stored, so a quote
# of the bot can only be recognised from the quoted sender at arrival time.
MIGRATIONS = {
    2: """
    ALTER TABLE messages ADD COLUMN reply_to_bot INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE labels (
      msg_id     TEXT PRIMARY KEY REFERENCES messages ON DELETE CASCADE,
      addressed  INTEGER NOT NULL CHECK (addressed IN (0, 1)),
      should     TEXT NOT NULL CHECK (should IN ('quiet', 'react', 'reply')),
      labeled_at INTEGER NOT NULL
    );
    CREATE TABLE label_skips (
      msg_id TEXT PRIMARY KEY REFERENCES messages ON DELETE CASCADE
    );
    """,
}
SESSION_GAP_S = 30 * 60  # a pause this long starts a new session


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ---- setup ---------------------------------------------------------
    def _migrate(self) -> None:
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v > SCHEMA_VERSION:
            raise RuntimeError(f"store schema v{v} is newer than this plugin (v{SCHEMA_VERSION})")
        if v == 0:
            sql = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
            self.db.executescript("BEGIN;\n" + sql + "\nPRAGMA user_version=1;\nCOMMIT;")
            v = 1
        for target in range(v + 1, SCHEMA_VERSION + 1):
            self.db.executescript(
                "BEGIN;\n" + MIGRATIONS[target] + f"\nPRAGMA user_version={target};\nCOMMIT;"
            )
            if target == 2:
                # command-like messages are no longer stored (design doc: they never
                # reach memory); remove the ones M0 logged
                self.drop_command_like()

    def drop_command_like(self) -> int:
        """Delete stored command-like messages, judged by the *current* rule.

        M0's flag also covered "." and "。" (".NET 项目更新了" was flagged), which the
        design doc counts as chat. So each old candidate is re-judged on its text:
        real commands are deleted (row, search index, media), the rest are kept and
        their stale flag cleared (PR #4 review, round 2). Text is the rendered form,
        so a borderline case is kept rather than deleted."""
        rows = self.db.execute(
            "SELECT rowid, msg_id, text FROM messages WHERE is_command_like=1"
        ).fetchall()
        drop = [r for r in rows if _is_command_like(r["text"])]
        keep = [r for r in rows if not _is_command_like(r["text"])]
        for r in drop:
            if r["text"]:
                self.db.execute(
                    "INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', ?, ?)",
                    (r["rowid"], r["text"]),
                )
            self.db.execute("DELETE FROM message_media WHERE msg_id=?", (r["msg_id"],))
            self.db.execute("DELETE FROM messages WHERE rowid=?", (r["rowid"],))
        for r in keep:
            self.db.execute("UPDATE messages SET is_command_like=0 WHERE rowid=?", (r["rowid"],))
        return len(drop)

    def close(self) -> None:
        self.db.close()

    # ---- people and scopes --------------------------------------------
    def person(self, platform: str, uid: str, name: str | None, ts: int) -> int:
        row = self.db.execute(
            "SELECT person_id FROM people WHERE platform=? AND platform_uid=?", (platform, uid)
        ).fetchone()
        if row:
            pid = row[0]
        else:
            pid = self.db.execute(
                "INSERT INTO people (platform, platform_uid, first_seen) VALUES (?,?,?)",
                (platform, uid, ts),
            ).lastrowid
        if name:
            cur = self.db.execute(
                "UPDATE aliases SET last_seen=? WHERE person_id=? AND name=?", (ts, pid, name)
            )
            if cur.rowcount == 0:
                self.db.execute(
                    "INSERT INTO aliases (person_id, name, first_seen, last_seen) VALUES (?,?,?,?)",
                    (pid, name, ts, ts),
                )
        return pid

    def latest_name(self, platform: str, uid: str) -> str | None:
        row = self.db.execute(
            """SELECT a.name FROM aliases a JOIN people p USING (person_id)
               WHERE p.platform=? AND p.platform_uid=? ORDER BY a.last_seen DESC LIMIT 1""",
            (platform, uid),
        ).fetchone()
        return row[0] if row else None

    def opted_out_uids(self, platform: str | None = None) -> set[str]:
        """Opted-out uids; all platforms unless one is given. The platform is the
        AstrBot adapter's id, which the user names (it's "llbot" on the live setup)."""
        sql, args = "SELECT platform_uid FROM people WHERE opted_out=1", ()
        if platform is not None:
            sql, args = sql + " AND platform=?", (platform,)
        return {r[0] for r in self.db.execute(sql, args)}

    def is_bot_message(self, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        row = self.db.execute("SELECT is_bot FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        return bool(row and row[0])

    def messages_in(self, scope_id: str, since_ts: int = 0, until_ts: int | None = None) -> list[sqlite3.Row]:
        """Messages oldest first, with the sender's latest display name."""
        sql = """SELECT m.rowid AS rid, m.*, (SELECT a.name FROM aliases a WHERE a.person_id=m.person_id
                              ORDER BY a.last_seen DESC LIMIT 1) AS sender_name
                 FROM messages m WHERE m.scope_id=? AND m.ts>=?"""
        args: list = [scope_id, since_ts]
        if until_ts is not None:
            sql += " AND m.ts<=?"
            args.append(until_ts)
        return self.db.execute(sql + " ORDER BY m.ts, m.rowid", args).fetchall()

    def before(self, msg_id: str, n: int) -> list[sqlite3.Row]:
        """The n messages that arrived before msg_id in the same scope, oldest first,
        by (ts, rowid): same-second messages count only if they were stored earlier."""
        t = self.db.execute("SELECT scope_id, ts, rowid FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        if not t:
            return []
        rows = self.db.execute(
            """SELECT m.rowid AS rid, m.*, (SELECT a.name FROM aliases a WHERE a.person_id=m.person_id
                              ORDER BY a.last_seen DESC LIMIT 1) AS sender_name
               FROM messages m WHERE m.scope_id=? AND (m.ts<? OR (m.ts=? AND m.rowid<?))
               ORDER BY m.ts DESC, m.rowid DESC LIMIT ?""",
            (t["scope_id"], t["ts"], t["ts"], t["rowid"], n),
        ).fetchall()
        return rows[::-1]

    def labelable(self, msg_id: str) -> bool:
        """Still there, not recalled, has text (PR #4 review: check before every answer)."""
        r = self.db.execute("SELECT recalled, text FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        return bool(r) and not r["recalled"] and bool(r["text"])

    def scope_ids(self, kind: str = "group") -> list[str]:
        return [r[0] for r in self.db.execute("SELECT scope_id FROM scopes WHERE kind=?", (kind,))]

    def group_scopes(self, group_ids=None) -> list[str]:
        """Scope ids of the watched groups (scope_id ends with ":<group id>"). Old
        scopes from groups no longer watched stay in the db but are left out
        (PR #4 review, round 3). group_ids=None means "no filter" (every group
        scope); an explicitly empty collection means "no groups" and returns []
        (PR #4 review, round 4)."""
        all_ids = self.scope_ids("group")
        if group_ids is None:
            return all_ids
        want = {str(g) for g in group_ids}
        return [s for s in all_ids if s.rsplit(":", 1)[-1] in want]

    # ---- labels (M1) ---------------------------------------------------
    def set_label(self, msg_id: str, addressed: bool, should: str, ts: int | None = None) -> None:
        self.db.execute(
            """INSERT INTO labels (msg_id, addressed, should, labeled_at) VALUES (?,?,?,?)
               ON CONFLICT (msg_id) DO UPDATE SET addressed=excluded.addressed,
                 should=excluded.should, labeled_at=excluded.labeled_at""",
            (msg_id, int(addressed), should, ts or int(time.time())),
        )
        self.db.execute("DELETE FROM label_skips WHERE msg_id=?", (msg_id,))

    def skip_label(self, msg_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO label_skips (msg_id) VALUES (?)", (msg_id,))

    def labels(self) -> dict[str, dict[str, Any]]:
        return {
            r["msg_id"]: {"addressed": bool(r["addressed"]), "should": r["should"]}
            for r in self.db.execute("SELECT * FROM labels")
        }

    def unlabeled(self, scope_id: str, limit: int = 2000) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT m.rowid AS rid, m.*, (SELECT a.name FROM aliases a WHERE a.person_id=m.person_id
                              ORDER BY a.last_seen DESC LIMIT 1) AS sender_name
               FROM messages m
               WHERE m.scope_id=? AND m.recalled=0 AND m.is_bot=0 AND m.text<>''
                 AND m.reply_to_bot=0
                 AND (m.mentions IS NULL OR m.mentions NOT LIKE '%"bot"%')
                 AND m.msg_id NOT IN (SELECT msg_id FROM labels)
                 AND m.msg_id NOT IN (SELECT msg_id FROM label_skips)
               ORDER BY m.ts DESC LIMIT ?""",
            (scope_id, limit),
        ).fetchall()

    def has_message(self, msg_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone() is not None

    def scope(self, scope_id: str, platform: str, kind: str, title: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO scopes (scope_id, platform, kind, title) VALUES (?,?,?,?)
               ON CONFLICT (scope_id) DO UPDATE SET title=COALESCE(excluded.title, title)""",
            (scope_id, platform, kind, title),
        )

    # ---- sessions ------------------------------------------------------
    def _session_for(self, scope_id: str, ts: int) -> int:
        row = self.db.execute(
            """SELECT s.session_id,
                      COALESCE((SELECT MAX(ts) FROM messages m WHERE m.session_id=s.session_id),
                               s.started)
               FROM sessions s WHERE scope_id=? AND ended IS NULL
               ORDER BY started DESC LIMIT 1""",
            (scope_id,),
        ).fetchone()
        if row:
            # an open session whose messages were purged falls back to its start time,
            # so the 30-minute gap still applies
            sid, last = row[0], row[1]
            if ts - last < SESSION_GAP_S:
                return sid
            self.db.execute("UPDATE sessions SET ended=? WHERE session_id=?", (last, sid))
        return self.db.execute(
            "INSERT INTO sessions (scope_id, started) VALUES (?,?)", (scope_id, ts)
        ).lastrowid

    # ---- messages ------------------------------------------------------
    def add_message(
        self,
        *,
        msg_id: str,
        scope_id: str,
        person_id: int | None,
        ts: int,
        text: str,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
        is_bot: bool = False,
        is_command_like: bool = False,
        reply_to_bot: bool = False,
    ) -> bool:
        """Returns False if the message was already stored (platform re-delivery)."""
        if self.db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
            return False
        sid = self._session_for(scope_id, ts)
        cur = self.db.execute(
            """INSERT INTO messages (msg_id, scope_id, session_id, person_id, ts, reply_to,
                 mentions, text, is_bot, is_command_like, reply_to_bot)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id, scope_id, sid, person_id, ts, reply_to,
                json.dumps(mentions, ensure_ascii=False) if mentions else None,
                text, int(is_bot), int(is_command_like), int(reply_to_bot),
            ),
        )
        self.db.execute("INSERT INTO messages_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, text))
        self.db.execute("UPDATE sessions SET n_msgs=n_msgs+1 WHERE session_id=?", (sid,))
        return True

    def recall(self, msg_id: str) -> bool:
        """撤回: blank the text and drop it from search. Returns True if found."""
        row = self.db.execute(
            "SELECT rowid, text, recalled FROM messages WHERE msg_id=?", (msg_id,)
        ).fetchone()
        if not row or row["recalled"]:
            return bool(row)
        self.db.execute(
            "INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', ?, ?)",
            (row["rowid"], row["text"]),
        )
        self.db.execute("UPDATE messages SET text='', recalled=1 WHERE msg_id=?", (msg_id,))
        self.db.execute("DELETE FROM message_media WHERE msg_id=?", (msg_id,))
        self.db.execute("DELETE FROM labels WHERE msg_id=?", (msg_id,))
        self.db.execute("DELETE FROM label_skips WHERE msg_id=?", (msg_id,))
        return True

    def search(self, scope_id: str, query: str, limit: int = 20) -> list[sqlite3.Row]:
        query = query.strip()
        if not query:
            return []
        if len(query) < 3:
            # trigram can't match 1-2 characters (most Chinese words); scan instead
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return self.db.execute(
                """SELECT * FROM messages WHERE scope_id=? AND recalled=0 AND text LIKE ? ESCAPE '\\'
                   ORDER BY ts DESC LIMIT ?""",
                (scope_id, like, limit),
            ).fetchall()
        return self.db.execute(
            """SELECT m.* FROM messages_fts f JOIN messages m ON m.rowid=f.rowid
               WHERE messages_fts MATCH ? AND m.scope_id=? ORDER BY m.ts DESC LIMIT ?""",
            (_fts_quote(query), scope_id, limit),
        ).fetchall()

    def recent(self, scope_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE scope_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (scope_id, limit),
        ).fetchall()[::-1]

    def purge_before(self, cutoff_ts: int) -> int:
        """Retention: delete transcripts older than cutoff. Returns rows deleted."""
        rows = self.db.execute(
            "SELECT rowid, text FROM messages WHERE ts<?", (cutoff_ts,)
        ).fetchall()
        for r in rows:
            if r["text"]:
                self.db.execute(
                    "INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', ?, ?)",
                    (r["rowid"], r["text"]),
                )
        self.db.execute(
            "DELETE FROM message_media WHERE msg_id IN (SELECT msg_id FROM messages WHERE ts<?)",
            (cutoff_ts,),
        )
        for t in ("labels", "label_skips"):
            self.db.execute(
                f"DELETE FROM {t} WHERE msg_id IN (SELECT msg_id FROM messages WHERE ts<?)", (cutoff_ts,)
            )
        self.db.execute("DELETE FROM messages WHERE ts<?", (cutoff_ts,))
        self.db.execute("DELETE FROM traces WHERE ts<?", (cutoff_ts,))
        # close open sessions that retention just emptied
        self.db.execute(
            """UPDATE sessions SET ended=started WHERE ended IS NULL AND started<?
               AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id=sessions.session_id)""",
            (cutoff_ts,),
        )
        return len(rows)

    # ---- traces --------------------------------------------------------
    def trace(
        self,
        *,
        scope_id: str,
        msg_id: str | None,
        ended_at: str,
        decision: str | None,
        reasons: dict[str, Any],
        harness_version: str,
        ts: int | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO traces (scope_id, msg_id, ts, ended_at, decision, reasons, harness_version)
               VALUES (?,?,?,?,?,?,?)""",
            (
                scope_id, msg_id, ts or int(time.time()), ended_at, decision,
                json.dumps(reasons, ensure_ascii=False, sort_keys=True), harness_version,
            ),
        )

    def recent_traces(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM traces ORDER BY trace_id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- status --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        q = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]  # noqa: E731
        day = int(time.time()) - 86400
        return {
            "people": q("SELECT COUNT(*) FROM people"),
            "scopes": q("SELECT COUNT(*) FROM scopes"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "messages_24h": q("SELECT COUNT(*) FROM messages WHERE ts>=?", day),
            "recalled": q("SELECT COUNT(*) FROM messages WHERE recalled=1"),
            "sessions": q("SELECT COUNT(*) FROM sessions"),
            "traces_24h": q("SELECT COUNT(*) FROM traces WHERE ts>=?", day),
            "labels": q("SELECT COUNT(*) FROM labels"),
            "schema_version": q("PRAGMA user_version"),
        }


def _fts_quote(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'

