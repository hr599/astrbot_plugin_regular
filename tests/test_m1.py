"""M1 tests: decide baseline, shadow traces, labels, bench. No AstrBot needed."""

import contextlib
import io
import json
import random
import sqlite3
import tempfile
import time
import unittest

from core.decide import POLICY_VERSION
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
from core.labeling import GUIDE, Labeler, parse_answers, tier
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
        self.assertEqual((t["decision"], t["harness_version"]), ("quiet", POLICY_VERSION))

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
        run = replay(from_store(self.s, SCOPE), merge_turns=False)  # per-message context, no burst merging
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


class SyntheticReviewFixtureTests(unittest.TestCase):
    def setUp(self):
        self.rec = next(r for r in load_fixtures() if r.name == "synthetic_review_v1")

    def test_loads_with_own_clock(self):
        crisis = next(m for m in self.rec.messages if m.msg_id == "C07-03")
        self.assertEqual(time.strftime("%H:%M", time.gmtime(crisis.ts + 8 * 3600)), "02:41")
        ts = [m.ts for m in self.rec.messages]
        self.assertEqual(ts, sorted(ts))  # one conversation per day, in order

    def test_counts_and_roles(self):
        self.assertEqual(len(self.rec.messages), 163)
        self.assertEqual(len(self.rec.labels), 154)
        self.assertTrue(any(m.from_bot for m in self.rec.messages))
        self.assertTrue(all(self.rec.labels.get(m.msg_id) is None for m in self.rec.messages if m.is_bot))

    def test_scenes_are_isolated(self):
        """PR #5 review: each scenario's first message gets no context, and every
        later one sees only its own scene's earlier messages."""
        seen = []

        def spy(m, ctx, now, **kw):
            seen.append((m, list(ctx)))
            return decide(m, ctx, now, **kw)

        replay(self.rec, policy=spy)
        firsts = {}
        for m, ctx in seen:
            if m.scene not in firsts:
                firsts[m.scene] = m.msg_id
                self.assertEqual(ctx, [], m.msg_id)
            else:
                self.assertTrue(all(c.scene == m.scene for c in ctx), m.msg_id)
                self.assertTrue(all(c.ts <= m.ts for c in ctx), m.msg_id)
        self.assertEqual(len(firsts), 16)
        c7 = next(ctx for m, ctx in seen if m.msg_id == "C07-01")
        self.assertEqual(c7, [])
        c7_3 = next(ctx for m, ctx in seen if m.msg_id == "C07-03")
        self.assertEqual([c.msg_id for c in c7_3], ["C07-01", "C07-02"])

    def test_store_recordings_unaffected_by_scenes(self):
        msgs = [Msg(str(i), i, "a", f"m{i}") for i in range(5)]
        seen = []
        replay(Recording("t", "store", msgs), policy=lambda m, c, n, **k: (seen.append(len(c)), decide(m, c, n))[1],
               merge_turns=False)
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_regenerates_identically(self):
        import subprocess
        import sys
        before = Path("bench/fixtures/synthetic_review_v1.jsonl").read_bytes()
        subprocess.run([sys.executable, "tools/make_review_fixture.py"], check=True, capture_output=True)
        self.assertEqual(Path("bench/fixtures/synthetic_review_v1.jsonl").read_bytes(), before)


class GroupScopeTests(unittest.TestCase):
    """PR #4 review, round 3: label and save from the watched groups, not scopes[0]."""

    A, B = "555", "777"

    def _store(self, groups):
        s = Store(":memory:")
        return s, Pipeline(s, GateConfig(group_ids=set(groups)))

    def test_empty_first_group_doesnt_block(self):
        s, p = self._store({self.A, self.B})
        p.handle(ev(group_msg("/help", group=self.A, mid=1)))  # creates scope A, stores nothing
        p.handle(ev(group_msg("B 群的聊天", group=self.B, mid=2)))
        lab = Labeler(s, rng=random.Random(0), scopes=lambda: s.group_scopes({self.A, self.B}))
        msg = lab.next()
        self.assertEqual(len(lab.pending), 1)
        self.assertIn("B 群的聊天", msg)
        self.assertIn(f"群 {self.B}", msg)  # group named when several are watched

    def test_switched_group_uses_the_new_one(self):
        s, p = self._store({self.A})
        p.handle(ev(group_msg("旧群 A 的聊天", group=self.A, mid=1)))
        p = Pipeline(s, GateConfig(group_ids={self.B}))  # config changed to B, same db
        p.handle(ev(group_msg("新群 B 的聊天", group=self.B, mid=2)))
        self.assertEqual(p.handle(ev(group_msg("A 的新消息", group=self.A, mid=3))).action, "drop")
        lab = Labeler(s, rng=random.Random(0), scopes=lambda: s.group_scopes({self.B}))
        msg = lab.next()
        self.assertIn("新群 B 的聊天", msg)
        self.assertNotIn("旧群 A", msg)
        self.assertNotIn("群 ", msg.split("【1】")[1].split("\n")[0])  # one group: no tag
        lab.answer("5")
        self.assertIn("没有待标注", lab.next())  # finished B; never falls back to old A

    def test_save_uses_watched_groups(self):
        s, p = self._store({self.A})
        p.handle(ev(group_msg("旧群 A", group=self.A, mid=1)))
        p = Pipeline(s, GateConfig(group_ids={self.B}))
        p.handle(ev(group_msg("新群 B", group=self.B, mid=2)))
        with tempfile.TemporaryDirectory() as d:
            out = bench_command(s, Path(d), ["save", "wk"], group_ids={self.B})
            self.assertIn(f"群 {self.B}：存好了 1 条", out)
            rec = load_saved(Path(d) / "bench" / "recordings")
            self.assertEqual([m.text for m in rec[0].messages], ["新群 B"])
            out = bench_command(s, Path(d), ["save", "wk2"], group_ids={self.A, self.B})
            self.assertIn(f"群 {self.A}：存好了", out)
            self.assertIn(f"群 {self.B}：存好了", out)

    def test_save_skips_empty(self):
        s, p = self._store({self.A})
        p.handle(ev(group_msg("/help", group=self.A, mid=1)))
        with tempfile.TemporaryDirectory() as d:
            out = bench_command(s, Path(d), ["save"], group_ids={self.A})
            self.assertIn("没有记录，没存", out)
            self.assertEqual(load_saved(Path(d) / "bench" / "recordings"), [])

    def test_group_scopes_match_exact_id(self):
        s = Store(":memory:")
        for g in ("555", "1555", "5550"):
            s.scope(f"llbot:GroupMessage:{g}", "llbot", "group")
        self.assertEqual(s.group_scopes({"555"}), ["llbot:GroupMessage:555"])
        self.assertEqual(len(s.group_scopes(None)), 3)


class ReviewRound4Tests(unittest.TestCase):
    """Limits accepted in PR #4's last review, fixed in a follow-up."""

    def setUp(self):
        self.s = Store(":memory:")
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        now = int(time.time())
        for i in range(12):
            p.handle(ev(group_msg(f"消息 {i}", mid=i + 1, ts=now - 200 + i * 10)))

    @staticmethod
    def _shown(reply: str) -> list[str]:
        """The texts an admin was asked about, in order: the lines marked →."""
        return [line.split(": ", 1)[1] for line in reply.splitlines() if line.startswith(" → ")]

    def test_two_admins_interleaved(self):
        """Works only from what each admin saw in their DM, so it checks behavior."""
        lab = Labeler(self.s, rng=random.Random(3))
        admin = AdminCommands(lambda: {}, lambda: "", lambda on: None, labeler=lab)
        A, B = "40004", "50005"
        a_seen = self._shown(admin(ev(dm("/reg label", uid=A), admin=True)))
        b_seen = self._shown(admin(ev(dm("/reg label", uid=B), admin=True)))
        self.assertTrue(a_seen and b_seen)
        self.assertFalse(set(a_seen) & set(b_seen))  # never the same message on two screens at once
        admin(ev(dm("/reg label " + " ".join("1" * len(a_seen)), uid=A), admin=True))
        by_text = {r["text"]: r["msg_id"] for r in self.s.messages_in(SCOPE)}
        labels = self.s.labels()
        self.assertEqual({t for t in by_text if by_text[t] in labels}, set(a_seen))  # exactly A's
        admin(ev(dm("/reg label " + " ".join("5" * len(b_seen)), uid=B), admin=True))
        labels = self.s.labels()
        for t in a_seen:
            self.assertEqual(labels[by_text[t]]["should"], "reply", t)
        for t in b_seen:
            self.assertEqual(labels[by_text[t]]["should"], "quiet", t)

    def test_guide_once_per_admin(self):
        lab = Labeler(self.s, rng=random.Random(0))
        self.assertIn("标注说明", lab.next("a"))
        self.assertIn("标注说明", lab.next("b"))
        self.assertNotIn("标注说明", lab.next("a"))

    def test_none_vs_empty_group_ids(self):
        self.assertEqual(self.s.group_scopes(None), [SCOPE])
        self.assertEqual(self.s.group_scopes(set()), [])
        self.assertEqual(self.s.group_scopes([]), [])
        self.assertEqual(self.s.group_scopes({GROUP}), [SCOPE])

    def test_cleared_config_label_and_save(self):
        cleared = set()  # group_ids emptied in the plugin config, same db
        lab = Labeler(self.s, scopes=lambda: self.s.group_scopes(cleared))
        self.assertIn("group_ids", lab.next())
        self.assertEqual(lab.pending, [])
        with tempfile.TemporaryDirectory() as d:
            out = bench_command(self.s, Path(d), ["save"], group_ids=cleared)
            self.assertIn("group_ids", out)
            self.assertEqual(load_saved(Path(d) / "bench" / "recordings"), [])
            out = bench_command(self.s, Path(d), [], group_ids=cleared)  # run still works
            self.assertNotIn("live", out)  # no old group replayed as live


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
            people = {x.msg_id for x in r.messages if not x.is_bot and x.kind == "message"}
            self.assertEqual(run.metrics["labeled"], sum(1 for k in r.labels if k in people))

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
            self.assertIn("m2-rules", out)
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

    def test_implied_rates_undo_the_label_sampling(self):
        from bench.splits import is_test
        # 100 plain messages (tier C) and 20 questions (tier B); /reg label over-samples B
        msgs, labels = [], {}
        i = 0
        while len([m for m in msgs if m.text == "吃了"]) < 100 or len([m for m in msgs if m.text == "去哪？"]) < 20:
            text = "吃了" if len([m for m in msgs if m.text == "吃了"]) < 100 else "去哪？"
            msgs.append(Msg(f"x{i}", 1000 + i, "a", text))
            i += 1
        dev = [m for m in msgs if not is_test(m.msg_id)]
        for m in [m for m in dev if m.text == "吃了"][:5]:
            labels[m.msg_id] = {"addressed": False, "should": "quiet"}
        qs = [m for m in dev if m.text == "去哪？"][:10]
        for k, m in enumerate(qs):
            labels[m.msg_id] = {"addressed": False, "should": "reply" if k % 2 else "react"}
        imp = replay(Recording("live", "store", msgs, labels)).metrics["implied"]
        self.assertEqual(imp["labelled_raw_act_rate"], round(10 / 15, 3))  # the sample says 67%
        self.assertAlmostEqual(imp["act_rate_unaddressed"], 20 / 120, places=3)  # the chat says 17%
        self.assertEqual(imp["reply_share"], round(10 / 130, 3))  # 10 replies among 120 + 10 messages
        self.assertEqual(imp["tiers"]["C"]["labels"], 5)
        # fixtures (hand-built scenes, not a sample of real chat) get no estimate
        self.assertIsNone(replay(Recording("f", "fixture", msgs, labels)).metrics["implied"])

    def test_implied_rate_uncertainty_and_pokes(self):
        # PR #22 review: sparse tiers share the pooled sample, so the SE must be the
        # pooled proportion's, not three "independent" ones; pokes at 阿灯 add replies
        msgs, labels = [], {}
        texts = ["阿灯好", "去哪？", "吃了"]  # tiers A, B, C, equal sizes
        for i in range(60):
            msgs.append(Msg(f"y{i}", 1000 + i * 10, "a", texts[i % 3]))
        from bench.splits import is_test
        dev = [m for m in msgs if not is_test(m.msg_id)]
        for t in texts:  # two labels per tier: one acts, one doesn't -> every tier borrows the pool
            ms = [m for m in dev if m.text == t][:2]
            labels[ms[0].msg_id] = {"addressed": False, "should": "reply"}
            labels[ms[1].msg_id] = {"addressed": False, "should": "quiet"}
        imp = replay(Recording("live", "store", msgs, labels)).metrics["implied"]
        self.assertAlmostEqual(imp["act_rate_unaddressed"], 0.5, places=3)
        # pooled p=0.5 over 6 labels: sd of the proportion = 0.204; bootstrap lands near it
        self.assertGreater(imp["act_rate_se"], 0.16)
        pokes = [Msg(f"p{i}", 5000 + i * 300, "a", "[戳一戳]", kind="poke", poke_bot=True) for i in range(6)]
        with_pokes = replay(Recording("live", "store", msgs + pokes, labels)).metrics["implied"]
        # 60 messages at a 50% reply rate = 30 replies (+ 6 for the pokes); pokes add no messages
        self.assertEqual(imp["reply_share"], round(30 / 90, 3))
        self.assertEqual(with_pokes["reply_share"], round(36 / 96, 3))

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

    # PR #10 review: the CLI tags other bots from the same config as the plugin and /reg bench
    def test_cli_reads_other_bot_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data" / "plugin_data" / "astrbot_plugin_regular"
            data.mkdir(parents=True)
            (root / "data" / "config").mkdir(parents=True)
            (root / "data" / "config" / "astrbot_plugin_regular_config.json").write_text(
                json.dumps({"other_bot_ids": ["70007"]}), encoding="utf-8")
            st = Store(data / "regular.db")
            p = Pipeline(st, GateConfig(group_ids={GROUP}, bot_uids={"70007"}))
            p.handle(ev(group_msg([{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 签到"}}],
                                  uid="70007", mid=1)))
            bench_command(st, data, [], bot_uids={"70007"})
            st.close()
            dm_live = json.loads(sorted((data / "bench" / "runs").glob("*.json"))[-1].read_text(encoding="utf-8"))
            cli_out = root / "cli"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                bench_main(["--db", str(data / "regular.db"), "--out", str(cli_out)])
            self.assertIn("other bots (plugin config): 1", buf.getvalue())
            cli = json.loads(next(cli_out.glob("*.json")).read_text(encoding="utf-8"))
            pick = lambda d: [r["metrics"] for r in d["runs"] if r["source"] == "store"][0]
            self.assertEqual(pick(dm_live)["explicit"], 0)  # another bot's @ isn't a direct address
            self.assertEqual(pick(cli)["explicit"], pick(dm_live)["explicit"])

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


class LabelingViewTests(unittest.TestCase):
    """Nov 9 compose drafts: the reply labels ran in long streaks inside one thread and
    included replies meant for someone else. The labeler saw 3 lines of context and
    couldn't see quotes."""

    def setUp(self):
        self.s = Store(":memory:")
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        now = int(time.time())
        self.now = now
        texts = ["今晚吃啥", "火锅？", "太贵了", "那烤冷面", "行吧", "我不去", "为啥", "没钱", "哈哈", "笑死"]
        for i, t in enumerate(texts):
            self.p.handle(ev(group_msg(t, uid=str(20000 + i % 2), mid=i + 1, ts=now - 300 + i * 10,
                                       name="小王" if i % 2 == 0 else "小李")))
        quote = [{"type": "reply", "data": {"id": "3"}}, {"type": "text", "data": {"text": "你说谁贵"}}]
        self.p.handle(ev(group_msg(quote, uid="20002", mid=11, ts=now - 150, name="小张")))

    def test_more_context_and_quotes_shown(self):
        lab = Labeler(self.s, rng=random.Random(0), batch=1)
        self.assertEqual(lab.context, 8)
        rows = self.s.before(f"{SCOPE}:11", 1)
        self.assertTrue(rows)
        line = lab._line(self.s.db.execute("SELECT m.*, NULL AS sender_name FROM messages m WHERE msg_id=?",
                                           (f"{SCOPE}:11",)).fetchone(), " → ")
        self.assertIn("（回复 小王）", line)
        self.assertIn("说给别人的", GUIDE)

    def test_earlier_3s_are_flagged_in_the_context(self):
        self.s.set_label(f"{SCOPE}:9", False, "reply")
        lab = Labeler(self.s, rng=random.Random(0))
        out = "\n".join(lab._blocks([self.s.db.execute(
            "SELECT m.*, NULL AS sender_name FROM messages m WHERE msg_id=?", (f"{SCOPE}:10",)).fetchone()], False))
        self.assertIn("哈哈   ← 你让阿灯说话了", out)
        self.assertNotIn("笑死   ← ", out)  # the target itself isn't flagged

    def test_recheck_goes_through_old_3s_once(self):
        old = self.now - 1000
        for mid in (2, 4, 6):
            self.s.set_label(f"{SCOPE}:{mid}", False, "reply", ts=old)
        self.s.set_label(f"{SCOPE}:5", False, "quiet", ts=old)
        lab = Labeler(self.s, rng=random.Random(0), batch=2)
        first = lab.answer("recheck")
        self.assertIn("复查", first)
        self.assertIn("还剩 3 条", first)
        self.assertEqual(lab.pending, [f"{SCOPE}:2", f"{SCOPE}:4"])
        nxt = lab.answer("5 s")  # change one to quiet, keep one
        self.assertEqual(self.s.labels()[f"{SCOPE}:2"]["should"], "quiet")
        self.assertEqual(self.s.labels()[f"{SCOPE}:4"]["should"], "reply")
        self.assertIn("还剩 1 条", nxt)
        self.assertEqual(lab.pending, [f"{SCOPE}:6"])
        done = lab.answer("3")
        self.assertIn("都复查完了", done)
        # back to normal labeling afterwards
        self.assertNotIn("复查以前", lab.next())
