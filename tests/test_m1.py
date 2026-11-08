"""M1 tests: decide baseline, shadow traces, labels, bench. No AstrBot needed."""

import contextlib
import io
import json
import random
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from bench.__main__ import aliases_from_config, main as bench_main
from bench.commands import bench_command, latest_summary
from bench.report import write_report
from bench.runner import is_direct, merge
from bench.splits import is_test
from bench.runner import replay
from bench.situations import Recording, from_store, load_fixtures, load_saved, save
from core.admin import AdminCommands
from core.decide import Msg, decide, name_addressed
from core.gate import GateConfig
from core.labeling import Labeler, parse_answers, tier
from core.onebot import reply_sender
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
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        now = int(time.time())
        for i, t in enumerate(["今晚吃啥", "火锅？", "阿灯 你吃吗", "随便"]):
            self.p.handle(ev(group_msg(t, mid=i + 1, ts=now - 100 + i * 10)))
        self.lab = Labeler(self.s, rng=random.Random(0))

    def test_batch_guide_and_answers(self):
        msg = self.lab.next()
        self.assertIn("标注说明", msg)  # guide on first batch only
        self.assertEqual(len(self.lab.pending), 4)
        self.assertEqual(msg.count("→"), 4)
        self.assertNotIn("标注说明", Labeler(self.s, rng=random.Random(1)).next().split("【1】")[1])
        targets = list(self.lab.pending)
        reply = self.lab.answer("1 5 s 3")
        self.assertIn("记下 3 条，跳过 1", reply)
        labels = self.s.labels()
        self.assertEqual(labels[targets[0]], {"addressed": True, "should": "reply"})
        self.assertEqual(labels[targets[3]], {"addressed": False, "should": "reply"})
        self.assertNotIn(targets[2], labels)

    def test_answer_formats_and_errors(self):
        self.assertEqual(parse_answers("5 5 3"), ["5", "5", "3"])
        self.assertEqual(parse_answers("553s"), ["5", "5", "3", "s"])
        self.assertEqual(parse_answers("5，5,3"), ["5", "5", "3"])
        self.assertIsNone(parse_answers("5 9"))
        self.lab.next()
        self.assertIn("4 条，收到 2", self.lab.answer("5 5"))
        self.assertIn("1-5 或 s", self.lab.answer("x y z w"))
        self.assertEqual(len(self.lab.pending), 4)  # nothing consumed by bad input

    def test_runs_out(self):
        self.lab.next()
        self.assertIn("没有待标注", self.lab.answer("5555"))

    def test_obvious_moments_not_asked(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        p.handle(ev(group_msg([{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 在吗"}}], mid=1)))
        self.assertIn("没有待标注", Labeler(s).next())

    # PR #4 review: show -> recall -> answer must not label the recalled message
    def test_recalled_while_pending_is_not_labeled(self):
        self.lab.next()
        target = self.lab.pending[0]
        self.s.recall(target)
        reply = self.lab.answer("1 5 5 5")
        self.assertIn("1 条已撤回或过期", reply)
        self.assertNotIn(target, self.s.labels())
        rec = from_store(self.s, SCOPE)
        self.assertNotIn(target, rec.labels)

    # PR #4 review: show -> purged by retention -> skip must not hit a foreign-key error
    def test_purged_while_pending_skip_is_safe(self):
        self.lab.next()
        self.s.purge_before(int(time.time()) + 10)
        reply = self.lab.answer("s s s s")
        self.assertIn("4 条已撤回或过期", reply)

    def test_recalled_message_label_dropped(self):
        self.lab.next()
        target = self.lab.pending[0]
        self.lab.answer("5 5 5 5")
        self.s.recall(target)
        self.assertNotIn(target, self.s.labels())

    def test_tiers(self):
        rows = {r["text"]: r for r in self.s.messages_in(SCOPE)}
        self.assertEqual(tier(rows["阿灯 你吃吗"]), "A")
        self.assertEqual(tier(rows["火锅？"]), "B")
        self.assertEqual(tier(rows["随便"]), "C")

    def test_admin_routes_label(self):
        admin = AdminCommands(lambda: {}, lambda: "", lambda on: None, labeler=self.lab)
        self.assertIn("→", admin(ev(dm("/reg label"), admin=True)))
        self.assertIn("记下 4 条", admin(ev(dm("/reg label 5 5 5 5"), admin=True)))
        self.assertIn("标注说明", admin(ev(dm("/reg label help"), admin=True)))


class SameSecondContextTests(unittest.TestCase):
    """PR #4 review: labelers must see only what had arrived before the moment."""

    def setUp(self):
        self.s = Store(":memory:")
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        p.handle(ev(group_msg("EARLIER", mid=1, ts=100)))
        p.handle(ev(group_msg("FIRST", mid=2, ts=200)))
        p.handle(ev(group_msg("LATER", mid=3, ts=200)))

    def test_before_uses_storage_order(self):
        self.assertEqual([r["text"] for r in self.s.before(f"{SCOPE}:2", 5)], ["EARLIER"])
        self.assertEqual([r["text"] for r in self.s.before(f"{SCOPE}:3", 5)], ["EARLIER", "FIRST"])

    def test_label_prompt_matches_replay_context(self):
        lab = Labeler(self.s, rng=random.Random(0), batch=3)
        msg = lab.next()
        block_first = [b for b in msg.split("【") if "→" in b and "FIRST" in b.split("→")[1]][0]
        self.assertNotIn("LATER", block_first)
        run = replay(from_store(self.s, SCOPE))
        ctx = {st.msg.text: st.decision.reasons["context_len"] for st in run.steps}
        self.assertEqual((ctx["FIRST"], ctx["LATER"]), (1, 2))


class QuoteOfBotTests(unittest.TestCase):
    """PR #4 review: the bot's own messages are never stored, so a quote of the bot
    must be recognised from the quoted sender, live and in replay."""

    class Reply:  # stands in for AstrBot's Reply component (matched by class name)
        def __init__(self, sender_id):
            self.sender_id = sender_id

    def test_reply_sender_from_components(self):
        self.assertEqual(reply_sender([object(), self.Reply(int(BOT))]), BOT)
        self.assertEqual(reply_sender([self.Reply(0)]), "")
        self.assertEqual(reply_sender([]), "")

    def test_real_path_quote_only(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        # the bot's own message is dropped at the gate, exactly as in production
        self.assertEqual(p.handle(ev(group_msg("我是阿灯说的话", uid=BOT, mid=70))).reasons["gate"], "own_message")
        quote = ev(group_msg([{"type": "reply", "data": {"id": "70"}},
                              {"type": "text", "data": {"text": "哈哈哈"}}], mid=71))
        quote.reply_sender_uid = reply_sender([self.Reply(int(BOT))])  # what the adapter sets
        out = p.handle(quote)
        self.assertTrue(out.reasons["reply_to_bot"])
        self.assertEqual(out.reasons["shadow"]["would"], "reply")
        run = replay(from_store(s, SCOPE))
        self.assertEqual((run.metrics["explicit"], run.metrics["explicit_missed"]), (1, 0))

    def test_quote_of_a_friend_is_not_for_bot(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        p.handle(ev(group_msg("原话", mid=1)))
        q = ev(group_msg([{"type": "reply", "data": {"id": "1"}}, {"type": "text", "data": {"text": "对"}}], mid=2))
        q.reply_sender_uid = FRIEND
        self.assertFalse(p.handle(q).reasons["reply_to_bot"])


class CommandLikeTests(unittest.TestCase):
    def test_not_stored_only_traced(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        out = p.handle(ev(group_msg("/help 秘密")))
        self.assertEqual(out.action, "dropped_command")
        self.assertEqual(s.stats()["messages"], 0)
        t = s.recent_traces(1)[0]
        self.assertNotIn("秘密", t["reasons"])
        self.assertTrue(json.loads(t["reasons"])["command_like"])

    def test_dots_are_chat(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        self.assertEqual(p.handle(ev(group_msg("。。。好吧"))).action, "logged")

    def test_migration_drops_old_command_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "regular.db"
            db = sqlite3.connect(path)
            db.executescript(Path("services/schema.sql").read_text(encoding="utf-8") + "\nPRAGMA user_version=1;")
            db.execute("INSERT INTO scopes (scope_id, platform, kind) VALUES (?,?,?)", (SCOPE, "llbot", "group"))
            for i, (t, c) in enumerate([("/help me", 1), ("hello", 0)]):
                cur = db.execute("INSERT INTO messages (msg_id, scope_id, ts, text, is_command_like) VALUES (?,?,?,?,?)",
                                 (f"{SCOPE}:{i}", SCOPE, i, t, c))
                db.execute("INSERT INTO messages_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, t))
            db.commit()
            db.close()
            st = Store(path)
            self.assertEqual([r["text"] for r in st.recent(SCOPE)], ["hello"])
            self.assertEqual(st.search(SCOPE, "help me"), [])
            st.close()


class ReviewRound2Tests(unittest.TestCase):
    """PR #4 review, round 2."""

    def test_upgrade_rejudges_old_command_flags(self):
        # a v1 db as M0 wrote it: M0's rule also flagged "." and "。" openers
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "regular.db"
            db = sqlite3.connect(path)
            db.executescript(Path("services/schema.sql").read_text(encoding="utf-8") + "\nPRAGMA user_version=1;")
            db.execute("INSERT INTO scopes (scope_id, platform, kind) VALUES (?,?,?)", (SCOPE, "llbot", "group"))
            rows = [(".NET 项目更新了", 1), ("。。。好吧", 1), ("/help 我", 1), ("#签到", 1), ("普通聊天内容", 0)]
            for i, (t, c) in enumerate(rows):
                cur = db.execute("INSERT INTO messages (msg_id, scope_id, ts, text, is_command_like) VALUES (?,?,?,?,?)",
                                 (f"{SCOPE}:{i}", SCOPE, i, t, c))
                db.execute("INSERT INTO messages_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, t))
            db.commit()
            db.close()
            st = Store(path)
            kept = {r["text"]: r["is_command_like"] for r in st.recent(SCOPE)}
            self.assertEqual(kept, {".NET 项目更新了": 0, "。。。好吧": 0, "普通聊天内容": 0})
            self.assertEqual(len(st.search(SCOPE, "项目更新")), 1)  # still searchable
            self.assertEqual(st.search(SCOPE, "help 我"), [])
            st.close()

    def _react_only(self, msg, ctx, now, **kw):
        d = decide(msg, ctx, now, **kw)
        if d.action == "reply":
            d.action = "react"
        return d

    def test_reaction_to_direct_address_is_missed(self):
        msgs = [Msg("1", 0, "a", "@我", mentions_bot=True), Msg("2", 1, "b", "哈", reply_to_bot=True),
                Msg("3", 2, "c", "[戳一戳]", kind="poke", poke_bot=True)]
        run = replay(Recording("t", "fixture", msgs), policy=self._react_only)
        self.assertEqual((run.metrics["explicit"], run.metrics["explicit_missed"]), (3, 3))
        self.assertFalse(run.metrics["gates"]["explicit_never_missed"])
        with tempfile.TemporaryDirectory() as d:
            html = write_report([run], Path(d), merge([run])).read_text(encoding="utf-8")
        self.assertEqual(html.count("<tr class='miss speak'>"), 3)  # highlighted as misses

    def test_other_bots_outside_the_gate(self):
        msgs = [Msg("1", 0, "小橘", "哈哈", reply_to_bot=True, from_bot=True)]
        run = replay(Recording("t", "fixture", msgs), policy=lambda m, c, n, **k: decide(Msg("q", 0, "x", ""), c, n))
        self.assertEqual(run.metrics["explicit"], 0)

    def test_fixture_labels_follow_the_contract(self):
        for r in load_fixtures():
            for x in r.messages:
                lab = r.labels.get(x.msg_id)
                if lab and is_direct(x):
                    self.assertEqual(lab["should"], "reply", f"{r.name}:{x.msg_id}")


class NameAddressTests(unittest.TestCase):
    def test_name_is_likely_not_forced(self):
        d = decide(m("阿灯 你觉得呢"), [], 0)
        self.assertEqual((d.action, d.address_kind, d.reasons["rule"]), ("quiet", "name", "likely_not_forced"))
        self.assertGreaterEqual(d.addressed, 0.5)  # counted as detected by the bench


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

    def test_held_out_split(self):
        s = Store(":memory:")
        p = Pipeline(s, GateConfig(group_ids={GROUP}))
        for i in range(40):
            p.handle(ev(group_msg(f"msg {i}", mid=i + 1, ts=1000 + i)))
            s.set_label(f"{SCOPE}:{i + 1}", False, "quiet")
        mt = replay(from_store(s, SCOPE)).metrics
        test_n = sum(is_test(f"{SCOPE}:{i + 1}") for i in range(40))
        self.assertGreater(test_n, 0)
        self.assertEqual((mt["labeled"], mt["labeled_dev"], mt["held_out"]["labeled"]), (40, 40 - test_n, test_n))
        self.assertEqual(is_test(f"{SCOPE}:1"), is_test(f"{SCOPE}:1"))  # stable
        fx = replay(load_fixtures()[0]).metrics  # fixtures aren't split
        self.assertEqual((fx["labeled"], fx["held_out"]), (fx["labeled_dev"], None))

    # PR #4 review: CLI must score with the same aliases as the live bot and /reg bench
    def test_cli_and_dm_agree_with_custom_alias(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data" / "plugin_data" / "astrbot_plugin_regular"
            data.mkdir(parents=True)
            (root / "data" / "config").mkdir(parents=True)
            (root / "data" / "config" / "astrbot_plugin_regular_config.json").write_text(
                json.dumps({"bot_aliases": ["小灯泡"]}, ensure_ascii=False), encoding="utf-8")
            st = Store(data / "regular.db")
            p = Pipeline(st, GateConfig(group_ids={GROUP}), aliases=("小灯泡",))
            out = p.handle(ev(group_msg("小灯泡你觉得呢", mid=1)))
            self.assertEqual(out.reasons["shadow"]["address_kind"], "name")
            st.set_label(f"{SCOPE}:1", True, "quiet")
            st.close()
            self.assertEqual(aliases_from_config(data / "regular.db"), ("小灯泡",))
            st = Store(data / "regular.db")
            bench_command(st, data, [], aliases=("小灯泡",))
            st.close()
            dm_live = json.loads(sorted((data / "bench" / "runs").glob("*.json"))[-1].read_text(encoding="utf-8"))
            cli_out = root / "cli"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                bench_main(["--db", str(data / "regular.db"), "--out", str(cli_out)])
            self.assertIn("aliases (plugin config): 小灯泡", buf.getvalue())
            cli = json.loads(next(cli_out.glob("*.json")).read_text(encoding="utf-8"))
            pick = lambda d: [r["metrics"] for r in d["runs"] if r["source"] == "store"][0]
            for k in ("addressed_recall", "action_agreement", "would_speak"):
                self.assertEqual(pick(dm_live)[k], pick(cli)[k], k)
            self.assertEqual(cli["totals"]["params"]["aliases"], ["小灯泡"])

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
