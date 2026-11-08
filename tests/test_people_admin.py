"""Task H: /reg forget, optout/optin, pause/resume. No AstrBot needed."""

import json
import tempfile
import time
import unittest
from pathlib import Path

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
            db.execute("INSERT INTO evidence VALUES (?,?,?,'supports')", (cid, msg, quote))
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
            self.assertEqual(forget_sender(folder, str(pid)), 1)
            blob = next(folder.glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("我的话", blob)
            self.assertIn("别人的话", blob)
            self.assertNotIn(f"{SCOPE}:1", json.loads(blob)["labels"])
            s.close()


if __name__ == "__main__":
    unittest.main()
