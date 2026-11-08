"""M1 tests: decide baseline, shadow traces, labels, bench. No AstrBot needed."""

import json
import random
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from bench.commands import bench_command, latest_summary
from bench.runner import replay
from bench.situations import Recording, from_store, load_fixtures, load_saved, save
from core.admin import AdminCommands
from core.decide import Msg, decide, name_addressed
from core.gate import GateConfig
from core.labeling import Labeler, tier
from core.pipeline import Pipeline
from services.debug import build_bundle
from services.store import SCHEMA_VERSION, Store
from tests.test_m0 import BOT, FRIEND, GROUP, ME, SCOPE, dm, ev, group_msg


def m(text="", **kw):
    return Msg(msg_id=kw.pop("msg_id", "x"), ts=kw.pop("ts", 0), sender=kw.pop("sender", "a"),
               text=text, **kw)


class DecideTests(unittest.TestCase):
    def test_explicit_addresses_reply(self):
        for kw in ({"mentions_bot": True}, {"reply_to_bot": True}, {"poke_bot": True, "kind": "poke"}):
            d = decide(m("x", **kw), [], 0)
            self.assertEqual((d.action, d.address_kind), ("reply", "explicit"), kw)

    def test_name_vocative_only(self):
        yes = ["阿灯你好", "灯灯在吗", "灯，你觉得呢", "灯你说", "灯", "你们说呢 灯?"]
        no = ["开灯了", "这灯光不错", "路灯坏了", "我跟灯说了", "把灯关了", "黑灯瞎火"]
        for t in yes:
            self.assertTrue(name_addressed(t), t)
        for t in no:
            self.assertFalse(name_addressed(t), t)

    def test_custom_aliases(self):
        self.assertTrue(name_addressed("小灯泡你在吗", ("小灯泡",)))
        self.assertFalse(name_addressed("阿灯你在吗", ("小灯泡",)))

    def test_quiet_and_own(self):
        self.assertEqual(decide(m("今晚吃啥"), [], 0).action, "quiet")
        self.assertEqual(decide(m("阿灯", is_bot=True), [], 0).action, "quiet")

    def test_uses_given_clock(self):
        self.assertEqual(decide(m("x"), [], 12345).reasons["now"], 12345)


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}))

    def test_trace_records_would_but_stays_quiet(self):
        out = self.p.handle(ev(group_msg([{"type": "at", "data": {"qq": BOT}},
                                          {"type": "text", "data": {"text": " 在吗"}}])))
        self.assertEqual(out.reasons["shadow"]["would"], "reply")
        self.assertIsNone(out.admin_reply)
        t = self.s.recent_traces(1)[0]
        self.assertEqual((t["decision"], t["harness_version"]), ("quiet", "m1-baseline"))

    def test_quote_of_bot_message(self):
        self.s.scope(SCOPE, "aiocqhttp", "group")
        self.s.add_message(msg_id=f"{SCOPE}:50", scope_id=SCOPE, person_id=None, ts=1, text="bot", is_bot=True)
        quote = [{"type": "reply", "data": {"id": "50"}}, {"type": "text", "data": {"text": "哈哈"}}]
        self.assertEqual(self.p.handle(ev(group_msg(quote, mid=51))).reasons["shadow"]["signals"],
                         ["quote_of_bot"])

    def test_poke_shadow(self):
        notice = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                  "group_id": int(GROUP), "user_id": int(FRIEND), "target_id": int(BOT),
                  "self_id": int(BOT), "time": int(time.time())}
        self.assertEqual(self.p.handle(ev(notice)).reasons["shadow"]["would"], "reply")


class MigrationTests(unittest.TestCase):
    def test_v1_db_upgrades_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "regular.db"
            db = sqlite3.connect(path)
            db.executescript(Path("services/schema.sql").read_text(encoding="utf-8") + "\nPRAGMA user_version=1;")
            db.execute("INSERT INTO scopes (scope_id, platform, kind) VALUES (?,?,?)", (SCOPE, "llbot", "group"))
            db.execute("INSERT INTO messages (msg_id, scope_id, ts, text) VALUES (?,?,?,?)", (f"{SCOPE}:1", SCOPE, 1, "hi"))
            db.commit()
            db.close()
            s = Store(path)
            self.assertEqual(s.stats()["schema_version"], SCHEMA_VERSION)
            self.assertEqual(s.stats()["messages"], 1)
            s.set_label(f"{SCOPE}:1", False, "quiet")
            self.assertEqual(s.labels()[f"{SCOPE}:1"]["should"], "quiet")
            s.close()  # Windows can't delete an open db file


class LabelingTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        now = int(time.time())
        for i, t in enumerate(["今晚吃啥", "火锅？", "阿灯 你吃吗", "随便"]):
            p.handle(ev(group_msg(t, mid=i + 1, ts=now - 100 + i * 10)))
        self.lab = Labeler(self.s, rng=random.Random(0))

    def test_next_shows_context_and_answer_saves(self):
        msg = self.lab.next()
        self.assertIn("→", msg)
        self.assertIn("/reg label 1-5", msg)
        target = self.lab.pending
        reply = self.lab.answer("1")
        self.assertIn("记下了", reply)
        self.assertEqual(self.s.labels()[target], {"addressed": True, "should": "reply"})

    def test_skip_and_bad_input(self):
        self.lab.next()
        first = self.lab.pending
        self.assertIn("1-5", self.lab.answer("9"))
        self.lab.answer("s")
        self.assertNotEqual(self.lab.pending, first)
        self.assertNotIn(first, self.s.labels())

    def test_runs_out(self):
        for _ in range(4):
            self.lab.next()
            self.lab.answer("5")
        self.assertIn("没有待标注", self.lab.next())

    def test_recalled_message_label_dropped(self):
        self.lab.next()
        target = self.lab.pending
        self.lab.answer("5")
        self.s.recall(target)
        self.assertNotIn(target, self.s.labels())

    def test_tiers(self):
        rows = {r["text"]: r for r in self.s.messages_in(SCOPE)}
        self.assertEqual(tier(rows["阿灯 你吃吗"]), "A")
        self.assertEqual(tier(rows["火锅？"]), "B")
        self.assertEqual(tier(rows["随便"]), "C")

    def test_admin_routes_label(self):
        admin = AdminCommands(lambda: {}, lambda: "", lambda on: None, labeler=self.lab)
        out = admin(ev(dm("/reg label"), admin=True))
        self.assertIn("→", out)
        self.assertIn("记下了", admin(ev(dm("/reg label 5"), admin=True)))


class BenchTests(unittest.TestCase):
    def test_fixtures_load_and_gate_holds(self):
        recs = load_fixtures()
        self.assertGreaterEqual(len(recs), 2)
        for r in recs:
            run = replay(r)
            self.assertEqual(run.metrics["explicit_missed"], 0, r.name)
            self.assertEqual(run.metrics["labeled"], len(r.labels) - sum(
                1 for k in r.labels if any(x.msg_id == k and x.is_bot for x in r.messages)))

    def test_replay_is_deterministic(self):
        r = load_fixtures()[0]
        a, b = replay(r), replay(r)
        self.assertEqual([s.decision.action for s in a.steps], [s.decision.action for s in b.steps])

    def test_metrics_math(self):
        msgs = [Msg("1", 0, "a", "@我", mentions_bot=True), Msg("2", 1, "b", "随便"),
                Msg("3", 2, "a", "上次那个是谁")]
        labels = {"1": {"addressed": True, "should": "reply"}, "2": {"addressed": False, "should": "quiet"},
                  "3": {"addressed": True, "should": "reply"}}
        mt = replay(Recording("t", "fixture", msgs, labels)).metrics
        self.assertEqual((mt["addressed_precision"], mt["addressed_recall"]), (1.0, 0.5))
        self.assertEqual(mt["action_agreement"], round(2 / 3, 3))
        self.assertEqual(mt["confusion"]["reply"]["quiet"], 1)
        self.assertEqual(mt["bot_share"], round(1 / 4, 3))

    def test_missed_explicit_is_caught(self):
        def never(m, ctx, now, **kw):
            return decide(Msg("q", 0, "x", ""), ctx, now)
        r = Recording("t", "fixture", [Msg("1", 0, "a", "@我", mentions_bot=True)])
        run = replay(r, policy=never)
        self.assertEqual(run.metrics["explicit_missed"], 1)
        self.assertFalse(run.metrics["gates"]["explicit_never_missed"])

    def test_store_recording_save_load_and_command(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        now = int(time.time())
        p.handle(ev(group_msg("阿灯 在吗", mid=1, ts=now - 50)))
        p.handle(ev(group_msg("秘密内容", mid=2, ts=now - 40)))
        s.set_label(f"{SCOPE}:1", True, "reply")
        rec = from_store(s, SCOPE)
        self.assertEqual(len(rec.messages), 2)
        self.assertEqual(replay(rec).metrics["labeled"], 1)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            save(rec, d / "bench" / "recordings")
            self.assertEqual(len(load_saved(d / "bench" / "recordings")[0].messages), 2)
            out = bench_command(s, d, [])
            self.assertIn("m1-baseline", out)
            self.assertIn("漏回 ✓", out)
            html = next((d / "bench" / "runs").glob("*.html")).read_text(encoding="utf-8")
            self.assertIn("秘密内容", html)  # the local viewer has the text...
            summ = latest_summary(d)
            path = build_bundle(d / "debug", status={}, traces=[], raw_events=[], bench=summ)
            with zipfile.ZipFile(path) as z:
                blob = z.read("bench.json").decode()
            self.assertNotIn("秘密内容", blob)  # ...the shareable summary doesn't
            self.assertNotIn(GROUP, blob)
            self.assertIn("explicit_missed", blob)
            out = bench_command(s, d, ["save", "wk1"])
            self.assertIn("2 条，1 个标注", out)

    def test_poke_traces_join_recording(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        p.handle(ev(group_msg("hi", mid=1)))
        p.handle(ev({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                     "group_id": int(GROUP), "user_id": int(FRIEND), "target_id": int(BOT),
                     "self_id": int(BOT), "time": int(time.time())}))
        rec = from_store(s, SCOPE)
        self.assertEqual([x.kind for x in rec.messages].count("poke"), 1)
        self.assertEqual(replay(rec).metrics["explicit_missed"], 0)


if __name__ == "__main__":
    unittest.main()
