"""Task H: /reg forget, optout/optin, pause/resume. No AstrBot needed."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from bench.commands import bench_command
from bench.report import forget_reports
from bench.situations import forget_sender, from_store, save
from core.admin import AdminCommands
from core.gate import GateConfig
from core.people_admin import PeopleAdmin
from core.pipeline import Pipeline
from services.debug import RawDumper
from services.store import Store
from tests.test_m0 import BOT, FRIEND, GROUP, ME, SCOPE, dm, ev, group_msg

OTHER = "40004"


class PeopleAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "r.db")
        self.cfg = GateConfig(group_ids={GROUP})
        self.saved_paused = None
        self.files_called = []
        people = PeopleAdmin(self.s, self.cfg, watched=lambda: self.cfg.group_ids,
                             save_paused=lambda p: setattr(self, "saved_paused", set(p)),
                             forget_files=lambda uid, sender: self.files_called.append((uid, sender)) or {})
        self.admin = AdminCommands(lambda: {}, lambda: "", lambda on: None, people=people)
        self.p = Pipeline(self.s, self.cfg, admin_handler=self.admin)
        self.t = int(time.time()) - 600
        self.mid = 0

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def say(self, text, uid=FRIEND):
        self.mid += 1
        return self.p.handle(ev(group_msg(text, uid=uid, mid=self.mid, ts=self.t + self.mid)))

    def reg(self, text, admin=True, uid=ME):
        return self.p.handle(ev(dm(text, uid=uid), admin=admin))

    def count(self, sql, *a):
        return self.s.db.execute(sql, a).fetchone()[0]

    # ---------------------------------------------------------------- who can
    def test_only_admin_dm(self):
        self.say("/reg forget 30003 confirm")  # in the group: a command, never stored, never run
        self.say("reg forget 30003 confirm")
        out = self.reg("/reg forget 30003 confirm", admin=False, uid=FRIEND)  # non-admin DM
        self.assertIsNone(out.admin_reply)
        self.assertEqual(out.reasons["gate"], "dm_command_not_admin")
        self.say("我还在")
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 2)  # 'reg forget...' + '我还在'
        self.assertEqual(self.s.opted_out_uids(), set())

    # ---------------------------------------------------------------- forget
    def seed_memory(self):
        self.say("我在学日语", FRIEND)  # m1 (FRIEND)
        self.say("小王是做芯片的", OTHER)  # m2 (OTHER, about FRIEND)
        self.say("我养了只猫", OTHER)  # m3
        self.say("对 他养猫", FRIEND)  # m4: FRIEND corroborating OTHER
        fp = self.s.person_id_of("aiocqhttp", FRIEND)
        op_ = self.s.person_id_of("aiocqhttp", OTHER)
        mid = lambda n: f"{SCOPE}:{n}"  # noqa: E731
        # claims as the dream pass (task I, PR #11) would leave them, written directly
        db = self.s.db

        def claim(subject, type_, text, msg, quote, run):
            cid = db.execute(
                """INSERT INTO claims (scope_id, subject_id, type, text, status, source, created,
                     last_evidence, run_id) VALUES (?,?,?,?,'validated','self',?,?,?)""",
                (SCOPE, subject, type_, text, self.t, self.t, run)).lastrowid
            db.execute("INSERT INTO evidence (claim_id, msg_id, quote, stance) VALUES (?,?,?,'supports')",
                       (cid, msg, quote))
            return cid

        r1 = db.execute("INSERT INTO dream_runs (scope_id, started, diff_json) VALUES (?,?,'[]')",
                        (SCOPE, self.t)).lastrowid
        c1 = claim(fp, "self_disclosure", "在学日语", mid(1), "我在学日语", r1)
        c2 = claim(op_, "self_disclosure", "养猫", mid(3), "我养了只猫", r1)
        r2 = db.execute("INSERT INTO dream_runs (scope_id, started, diff_json) VALUES (?,?,'[]')",
                        (SCOPE, self.t)).lastrowid
        c3 = claim(op_, "identity", "家里有猫", mid(4), "他养猫", r2)
        snap = lambda cid, text, m, q: {"claim": {"claim_id": cid, "text": text},  # noqa: E731
                                        "evidence": [{"msg_id": m, "quote": q}]}
        db.execute("UPDATE dream_runs SET diff_json=? WHERE run_id=?", (json.dumps(
            [{"op": {"kind": "add", "subject": fp, "evidence": [{"msg_id": mid(1), "quote": "我在学日语"}]},
              "before": {}, "touched": [c1], "after": {str(c1): snap(c1, "在学日语", mid(1), "我在学日语")}},
             {"op": {"kind": "add", "subject": op_, "evidence": [{"msg_id": mid(3), "quote": "我养了只猫"}]},
              "before": {}, "touched": [c2], "after": {str(c2): snap(c2, "养猫", mid(3), "我养了只猫")}}],
            ensure_ascii=False), r1))
        db.execute("UPDATE dream_runs SET diff_json=? WHERE run_id=?", (json.dumps(
            [{"op": {"kind": "add", "subject": op_, "evidence": [{"msg_id": mid(4), "quote": "他养猫"}]},
              "before": {}, "touched": [c3], "after": {str(c3): snap(c3, "家里有猫", mid(4), "他养猫")}}],
            ensure_ascii=False), r2))
        db.execute("INSERT INTO rejected VALUES (?,?,?)",
                   (r2, json.dumps({"kind": "add", "evidence": [{"msg_id": mid(1), "quote": "我在学日语"}]},
                                   ensure_ascii=False), "duplicate"))
        self.s.set_label(mid(1), False, "quiet")
        return fp, op_

    def test_preview_then_confirm(self):
        fp, op_ = self.seed_memory()
        out = self.reg(f"/reg forget {FRIEND}").admin_reply
        self.assertIn("2 条消息", out)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE person_id=?", fp), 2)  # nothing yet

        out = self.reg(f"/reg forget {FRIEND} confirm").admin_reply
        self.assertIn("已删除", out)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE person_id=?", fp), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM people WHERE person_id=?", fp), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM aliases WHERE person_id=?", fp), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM labels"), 0)
        self.assertEqual(self.count(f"SELECT COUNT(*) FROM traces WHERE msg_id IN ('{SCOPE}:1','{SCOPE}:4')"), 0)
        self.assertEqual(self.s.search(SCOPE, "日语"), [])
        # memory: the claim about them goes; OTHER's own claim stays; the claim supported
        # only by FRIEND's words goes too
        texts = [r[0] for r in self.s.db.execute("SELECT text FROM claims")]
        self.assertEqual(texts, ["养猫"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM evidence WHERE msg_id LIKE ?", f"{SCOPE}:4"), 0)
        # audit snapshots that held their words can't be used to bring them back
        blobs = " ".join(r[0] for r in self.s.db.execute("SELECT diff_json FROM dream_runs"))
        self.assertNotIn("日语", blobs)
        self.assertNotIn("他养猫", blobs)
        self.assertEqual(self.count("SELECT COUNT(*) FROM rejected"), 0)
        self.assertEqual(self.files_called, [(FRIEND, str(fp))])
        # not opted out: speaking again starts a fresh record
        self.say("我回来了", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE text='我回来了'"), 1)

    # ---------------------------------------------------------------- PR #12 review
    def test_forget_after_retention_still_finds_their_quotes(self):
        fp, op_ = self.seed_memory()
        self.s.purge_before(self.t + 10_000)  # every transcript row is past retention
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 0)
        # undo snapshots and rejected proposals citing purged messages don't outlive them
        blobs = " ".join(r[0] for r in self.s.db.execute("SELECT diff_json FROM dream_runs"))
        self.assertNotIn("他养猫", blobs)
        self.assertEqual(self.count("SELECT COUNT(*) FROM rejected"), 0)
        # memory itself does outlive them, with the author kept
        self.assertEqual(self.count("SELECT COUNT(*) FROM evidence WHERE author_id=?", fp), 2)
        self.s.forget_person("aiocqhttp", FRIEND)
        quotes = [r[0] for r in self.s.db.execute("SELECT quote FROM evidence")]
        self.assertEqual(quotes, ["我养了只猫"])  # OTHER's own words stay
        self.assertEqual([r[0] for r in self.s.db.execute("SELECT text FROM claims")], ["养猫"])

    def test_forget_removes_traces_naming_them(self):
        self.say("hi", FRIEND)
        fp = self.s.person_id_of("aiocqhttp", FRIEND)
        for reasons in ({"actor": fp, "is_add": True}, {"signals": {"answers": fp}}, {"actor": fp + 1}):
            self.s.trace(scope_id=SCOPE, msg_id=None, ended_at="gate", decision=None,
                         reasons=reasons, harness_version="t", ts=self.t)
        self.s.forget_person("aiocqhttp", FRIEND)
        left = [json.loads(r[0]) for r in self.s.db.execute("SELECT reasons FROM traces WHERE msg_id IS NULL")]
        self.assertEqual(left, [{"actor": fp + 1}])

    def test_forget_with_stop_opts_out(self):
        self.say("hi", FRIEND)
        out = self.reg(f"/reg mem forget {FRIEND} confirm stop").admin_reply  # design-doc spelling works too
        self.assertIn("以后不再记录", out)
        self.assertEqual(self.s.opted_out_uids(), {FRIEND})
        self.say("还在吗", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 0)

    def test_forget_keeps_existing_opt_out(self):
        self.say("hi", FRIEND)
        self.reg(f"/reg optout {FRIEND}")
        self.reg(f"/reg forget {FRIEND} confirm")
        self.assertEqual(self.s.opted_out_uids(), {FRIEND})  # forgetting never re-enables recording

    def test_bad_input(self):
        self.assertIn("用法", self.reg("/reg forget").admin_reply)
        self.assertIn("用法", self.reg("/reg forget 小王 confirm").admin_reply)
        self.assertIn("没有", self.reg("/reg forget 99999 confirm").admin_reply)

    def test_optout_optin(self):
        self.reg(f"/reg optout {FRIEND}")
        self.say("hi", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 0)
        self.reg(f"/reg optin {FRIEND}")
        self.say("hi again", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 1)

    # ---------------------------------------------------------------- pause
    def test_pause_resume(self):
        self.say("before", FRIEND)  # mid 1
        out = self.reg("/reg pause").admin_reply
        self.assertIn(GROUP, out)
        self.assertEqual(self.saved_paused, {GROUP})
        self.say("during", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 1)
        # a recall during the pause still blanks what was recorded before it
        self.p.handle(ev({"post_type": "notice", "notice_type": "group_recall", "group_id": int(GROUP),
                          "user_id": int(FRIEND), "operator_id": int(FRIEND), "message_id": 1,
                          "self_id": int(BOT), "time": self.t + 50}))
        self.assertEqual(self.count("SELECT recalled FROM messages"), 1)
        # other notices (pokes) are ignored while paused
        out = self.p.handle(ev({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                                "group_id": int(GROUP), "user_id": int(FRIEND), "target_id": int(BOT),
                                "self_id": int(BOT), "time": self.t + 60}))
        self.assertEqual(out.reasons["gate"], "paused")
        self.reg("/reg resume")
        self.assertEqual(self.saved_paused, set())
        self.say("after", FRIEND)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 2)

    def test_pause_unknown_group_refused(self):
        self.assertIn("不在观察列表", self.reg("/reg pause 12345").admin_reply)
        self.assertEqual(self.cfg.paused_groups, set())


class ForgetFilesTests(unittest.TestCase):
    """forget with real local files (PR #12 review)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.s = Store(d / "r.db")
        self.cfg = GateConfig(group_ids={GROUP})
        self.dumper = RawDumper(d / "raw", enabled=True)
        self.rec_dir = d / "recordings"

        self.runs_dir = d / "runs"

        def files(uid, sender):
            n, broken = forget_sender(self.rec_dir, sender) if sender else (0, [])
            return {"raw": self.dumper.forget(uid), "recordings": n, "broken": broken,
                    "reports": forget_reports(self.runs_dir)}

        self.people = PeopleAdmin(self.s, self.cfg, watched=lambda: self.cfg.group_ids,
                                  save_paused=lambda p: None, forget_files=files)
        self.p = Pipeline(self.s, self.cfg, admin_handler=AdminCommands(lambda: {}, lambda: "", lambda o: None,
                                                                        people=self.people))
        self.t = int(time.time()) - 100

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def feed(self, raw):
        out = self.p.handle(ev(raw))
        if out.action != "drop":
            self.dumper.write(raw)
        return out

    def reg(self, text):
        return self.p.handle(ev(dm(text), admin=True)).admin_reply

    def test_raw_cleaned_even_without_a_person_row(self):
        self.feed(group_msg("/签到", uid=OTHER, mid=1, ts=self.t))  # command: no people row, but dumped
        self.assertIsNone(self.s.person_id_of("aiocqhttp", OTHER))
        self.assertEqual(len(self.dumper.tail()), 1)
        out = self.reg(f"/reg forget {OTHER} confirm")
        self.assertIn("原始事件删了 1 条", out)
        self.assertEqual(self.dumper.tail(), [])

    def test_broken_recording_leaves_db_for_a_retry_and_stop_holds(self):
        self.feed(group_msg("我的话", uid=FRIEND, mid=1, ts=self.t))
        save(from_store(self.s, SCOPE), self.rec_dir)
        (self.rec_dir / "zz-broken.json").write_text("{not json", encoding="utf-8")
        out = self.reg(f"/reg forget {FRIEND} confirm stop")
        self.assertIn("zz-broken.json", out)
        self.assertIn("数据库还没动", out)
        # stop took effect at once: new messages aren't recorded
        self.feed(group_msg("还在吗", uid=FRIEND, mid=2, ts=self.t + 5))
        self.assertEqual([r[0] for r in self.s.db.execute("SELECT text FROM messages")], ["我的话"])
        self.assertEqual(self.cfg.opted_out_uids, {FRIEND})
        # fix the file, retry: everything goes, including the saved recording's lines
        (self.rec_dir / "zz-broken.json").unlink()
        out = self.reg(f"/reg forget {FRIEND} confirm stop")
        self.assertIn("已删除", out)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertNotIn("我的话", next(self.rec_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(self.cfg.opted_out_uids, {FRIEND})

    def test_html_reports_with_their_words_are_removed(self):
        # PR #12 review: the HTML trace viewer embeds message text
        self.feed(group_msg("独一无二的一句话", uid=FRIEND, mid=1, ts=self.t))
        bench_command(self.s, Path(self.tmp.name), [])
        html = list((Path(self.tmp.name) / "bench" / "runs").glob("bench-*.html"))
        self.assertTrue(html and "独一无二的一句话" in html[0].read_text(encoding="utf-8"))
        self.runs_dir = Path(self.tmp.name) / "bench" / "runs"
        out = self.reg(f"/reg forget {FRIEND} confirm")
        self.assertIn("网页报告", out)
        self.assertEqual(list(self.runs_dir.glob("bench-*.html")), [])
        self.assertTrue(list(self.runs_dir.glob("bench-*.json")))  # numbers-only summary stays

    def test_file_error_is_reported_not_raised(self):
        self.feed(group_msg("我的话", uid=FRIEND, mid=1, ts=self.t))
        self.people.forget_files = lambda uid, sender: (_ for _ in ()).throw(PermissionError("locked"))
        out = self.reg(f"/reg forget {FRIEND} confirm stop")
        self.assertIn("PermissionError", out)
        self.assertEqual(self.cfg.opted_out_uids, {FRIEND})
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)


class MigrationTests(unittest.TestCase):
    def test_v3_backfills_evidence_authors(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.db"
            db = sqlite3.connect(path)
            db.executescript(Path("services/schema.sql").read_text(encoding="utf-8") + "\nPRAGMA user_version=1;")
            db.execute("INSERT INTO scopes VALUES (?, 'aiocqhttp', 'group', NULL, 0)", (SCOPE,))
            db.execute("INSERT INTO people (person_id, platform, platform_uid, first_seen) VALUES (7, 'aiocqhttp', ?, 0)",
                       (FRIEND,))
            db.execute("INSERT INTO messages (msg_id, scope_id, person_id, ts, text) VALUES ('m1', ?, 7, 0, '我养猫')",
                       (SCOPE,))
            db.execute("""INSERT INTO claims (claim_id, scope_id, subject_id, type, text, status, source, created,
                          last_evidence) VALUES (1, ?, 7, 'self_disclosure', '养猫', 'validated', 'self', 0, 0)""", (SCOPE,))
            db.execute("INSERT INTO evidence VALUES (1, 'm1', '我养猫', 'supports')")
            db.commit()
            db.close()
            s = Store(path)
            self.assertEqual(s.db.execute("SELECT author_id FROM evidence").fetchone()[0], 7)
            s.close()


class FileScrubTests(unittest.TestCase):
    def test_raw_dump_forget(self):
        with tempfile.TemporaryDirectory() as d:
            dumper = RawDumper(Path(d), enabled=True)
            dumper.write(group_msg("mine", uid=FRIEND))
            dumper.write(group_msg("theirs", uid=OTHER))
            dumper.write({"post_type": "notice", "notice_type": "group_recall", "operator_id": int(FRIEND)})
            self.assertEqual(dumper.forget(FRIEND), 2)
            left = dumper.tail()
            self.assertEqual(len(left), 1)
            self.assertNotIn("mine", json.dumps(left, ensure_ascii=False))

    def test_raw_forget_keeps_file_age(self):
        # PR #12 review: rewriting must not reset the retention clock for everyone else
        import os
        with tempfile.TemporaryDirectory() as d:
            dumper = RawDumper(Path(d), enabled=True, keep_days=3)
            dumper.write(group_msg("mine", uid=FRIEND))
            dumper.write(group_msg("theirs", uid=OTHER))
            f = next(Path(d).glob("raw-*.jsonl"))
            old = time.time() - 5 * 86400
            os.utime(f, (old, old))
            self.assertEqual(dumper.forget("99999"), 0)  # unrelated uid: untouched
            self.assertEqual(int(f.stat().st_mtime), int(old))
            self.assertEqual(dumper.forget(FRIEND), 1)  # real rewrite: age kept
            self.assertEqual(int(f.stat().st_mtime), int(old))
            dumper.prune()
            self.assertFalse(f.exists())

    def test_saved_recordings_forget(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "r.db")
            p = Pipeline(s, GateConfig(group_ids={GROUP}))
            t = int(time.time()) - 100
            p.handle(ev(group_msg("我的话", uid=FRIEND, mid=1, ts=t)))
            p.handle(ev(group_msg("别人的话", uid=OTHER, mid=2, ts=t + 1)))
            s.set_label(f"{SCOPE}:1", False, "quiet")
            folder = Path(d) / "recordings"
            save(from_store(s, SCOPE), folder)
            pid = s.person_id_of("aiocqhttp", FRIEND)
            self.assertEqual(forget_sender(folder, str(pid)), (1, []))
            blob = next(folder.glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("我的话", blob)
            self.assertIn("别人的话", blob)
            self.assertNotIn(f"{SCOPE}:1", json.loads(blob)["labels"])
            s.close()


if __name__ == "__main__":
    unittest.main()
