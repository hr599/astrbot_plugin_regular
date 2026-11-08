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

SCHEMA_VERSION = 1
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
        if v == 0:
            sql = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
            self.db.executescript("BEGIN;\n" + sql + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
        elif v > SCHEMA_VERSION:
            raise RuntimeError(f"store schema v{v} is newer than this plugin (v{SCHEMA_VERSION})")

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
    ) -> bool:
        """Returns False if the message was already stored (platform re-delivery)."""
        if self.db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
            return False
        sid = self._session_for(scope_id, ts)
        cur = self.db.execute(
            """INSERT INTO messages (msg_id, scope_id, session_id, person_id, ts, reply_to,
                 mentions, text, is_bot, is_command_like)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id, scope_id, sid, person_id, ts, reply_to,
                json.dumps(mentions, ensure_ascii=False) if mentions else None,
                text, int(is_bot), int(is_command_like),
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
            "schema_version": q("PRAGMA user_version"),
        }


def _fts_quote(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'

