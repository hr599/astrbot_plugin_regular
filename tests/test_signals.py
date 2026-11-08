"""Task D: cheap per-message signals (record-only)."""

import json
import time
import unittest

from bench.situations import from_store, load_fixtures
from core.gate import GateConfig
from core.pipeline import Pipeline
from core.signals import compute, crisis_cue, decay, heat_step, serious_cue
from services.debug import redact
from services.store import Store
from tests.test_m0 import BOT, FRIEND, GROUP, ME, SCOPE, ev, group_msg

OTHER = "60006"


class CueTests(unittest.TestCase):
    def test_serious_announcements(self):
        for t in ["我爷爷今天早上走了", "我们分手了", "我爸被裁了", "我妈住院了 下午做检查",
                  "我家狗狗今天走了 养了十二年", "考研又没上 我觉得我这辈子就这样了", "啊 节哀"]:
            self.assertIsNotNone(serious_cue(t), t)

    def test_serious_lookalikes(self):
        for t in ["这游戏我最喜欢的角色去世了 哭死", "被裁判吹了个越位 气死", "分手快乐这首歌好好听",
                  "住院部门口那家奶茶好喝", "我先走了", "走了走了 下班", "车走了", "今晚吃啥"]:
            self.assertIsNone(serious_cue(t), t)

    def test_crisis_sincere_vs_joking(self):
        sincere = ["活着好累啊 真的不想再撑了", "如果我消失了 应该也没人会在意吧", "我是真的撑不住了",
                   "有时候真想一了百了", "说真的 最近真的撑不下去了", "有时候觉得不在了也挺好"]
        for t in sincere:
            cue, joking = crisis_cue(t)
            self.assertIsNotNone(cue, t)
            self.assertFalse(joking, t)
        cue, joking = crisis_cue("作业多到不想活了哈哈哈")
        self.assertTrue(cue and joking)
        for t in ["累死了 今天走了两万步", "这破作业要我命", "笑死 我没了", "又输了 我不活了", "一起去世"]:
            self.assertIsNone(crisis_cue(t)[0], t)

    def test_no_cues_on_plain_fixture_lines(self):
        rec = next(r for r in load_fixtures() if r.name == "synthetic_review_v1")
        for m in rec.messages:
            lab = rec.labels.get(m.msg_id) or {}
            if not set(lab.get("tags", [])) & {"heavy", "crisis_sincere", "crisis_joke", "crisis_ambiguous"}:
                self.assertIsNone(serious_cue(m.text), m.text)
                self.assertIsNone(crisis_cue(m.text)[0], m.text)

    def test_salience(self):
        self.assertEqual(compute("今晚吃啥").salience, 0)
        laugh = compute("哈哈哈哈 笑死")
        self.assertIn("laughter", laugh.cues)
        self.assertGreater(compute("我下个月要搬家了").salience, 0)
        self.assertIn("memory_cue", compute("这个别提了").cues)
        hi = compute("不对 你记错了", addressed=True)
        self.assertIn("correction_of_bot", hi.cues)
        self.assertLessEqual(compute("哈哈哈 记住 我要搬家了 我爷爷走了 撑不下去", addressed=True,
                                     reply_depth=5).salience, 1.0)

    def test_rude_only_counts_when_aimed_at_bot(self):
        self.assertTrue(compute("闭嘴吧 破机器人", addressed=True).rude_to_bot)
        self.assertFalse(compute("闭嘴吧你", addressed=False).rude_to_bot)  # between friends: not ours

    def test_heat(self):
        h = heat_step(0, 0, back_and_forth=True, insult=False, heat_cue=True)
        self.assertGreater(h, 0.2)
        self.assertAlmostEqual(decay(h, 300), h / 2, places=6)  # 5-minute half-life
        self.assertLess(decay(1.0, 600), 0.3)  # cools within about 10 minutes
        self.assertEqual(heat_step(0.95, 0, back_and_forth=True, insult=True, heat_cue=True), 1.0)


class PipelineSignalTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}, bot_uids={OTHER}))
        self.t = int(time.time()) - 1000

    def msg(self, text, uid=FRIEND, mid=1, dt=0, segs=None):
        return self.p.handle(ev(group_msg(segs or text, uid=uid, mid=mid, ts=self.t + dt)))

    def test_salience_stored_and_traced(self):
        out = self.msg("哈哈哈哈 笑死", mid=1)
        self.assertIn("laughter", out.reasons["signals"]["cues"])
        row = self.s.db.execute("SELECT salience FROM messages WHERE msg_id=?", (f"{SCOPE}:1",)).fetchone()
        self.assertGreater(row[0], 0)

    def test_answers_and_reply_depth(self):
        self.msg("A 说的", uid=FRIEND, mid=1)
        out = self.msg("B 接一句", uid=ME, mid=2, dt=20)
        a_pid = self.s.person_of(f"{SCOPE}:1")
        self.assertEqual(out.reasons["signals"]["answers"], a_pid)
        q = [{"type": "reply", "data": {"id": "2"}}, {"type": "text", "data": {"text": "对"}}]
        out = self.msg("", uid=FRIEND, mid=3, dt=40, segs=q)
        self.assertIn("reply_depth:1", out.reasons["signals"]["cues"])
        self.assertEqual(out.reasons["signals"]["answers"], self.s.person_of(f"{SCOPE}:2"))
        out = self.msg("隔了很久", uid=ME, mid=4, dt=900)
        self.assertNotIn("answers", out.reasons["signals"])  # too slow to count as an answer

    def test_heat_rises_in_a_quick_back_and_forth_and_persists(self):
        lines = [("上次明明是你", FRIEND), ("我没有", ME), ("你每次都这样", FRIEND), ("凭什么说我", ME),
                 ("别吵了别吵了", "70007")]
        heats = [self.msg(t, uid=u, mid=i + 1, dt=i * 15).reasons["signals"]["heat"] for i, (t, u) in enumerate(lines)]
        self.assertGreater(heats[-1], 0.5)
        self.assertEqual(round(self.s.heat(SCOPE)[0], 3), heats[-1])
        calm = self.msg("吃饭去", uid=ME, mid=10, dt=60 + 1200).reasons["signals"]["heat"]
        self.assertLess(calm, 0.1)  # 20 quiet minutes later it has cooled

    def test_other_bots_tagged(self):
        out = self.msg("今日签到排行", uid=OTHER, mid=1)
        self.assertTrue(out.reasons["from_bot"])
        self.assertTrue(out.reasons["signals"]["from_bot"])
        self.msg("人类的话", uid=FRIEND, mid=2, dt=10)
        out = self.msg("机器人接话", uid=OTHER, mid=3, dt=20)
        self.assertNotIn("answers", out.reasons["signals"])  # bots never feed who-answers-whom
        # a human answering a bot (quick follow-up or a quote) doesn't count either
        out = self.msg("人类回机器人", uid=FRIEND, mid=4, dt=30)
        self.assertNotIn("answers", out.reasons["signals"])
        q = [{"type": "reply", "data": {"id": "3"}}, {"type": "text", "data": {"text": "签到"}}]
        out = self.msg("", uid=ME, mid=5, dt=500, segs=q)
        self.assertNotIn("answers", out.reasons["signals"])
        self.msg("", uid=OTHER, mid=6, dt=510, segs="收到")
        rec = from_store(self.s, SCOPE, bot_uids={OTHER})
        self.assertEqual([m.from_bot for m in rec.messages], [True, False, True, False, False, True])

    def test_reaction_tracking(self):
        self.msg("好看吗", mid=5)
        base = {"post_type": "notice", "notice_type": "group_msg_emoji_like", "group_id": int(GROUP),
                "user_id": int(ME), "message_id": 5, "self_id": int(BOT), "time": self.t + 30,
                "likes": [{"emoji_id": "128077", "count": 1}]}
        out = self.p.handle(ev(dict(base, is_add=True)))
        self.assertEqual((out.reasons["is_add"], out.reasons["target_found"], out.reasons["emoji_ids"]),
                         (True, True, ["128077"]))
        out = self.p.handle(ev(dict(base, is_add=False, message_id=999)))
        self.assertEqual((out.reasons["is_add"], out.reasons["target_found"]), (False, False))

    def test_bundle_shows_cue_names_not_text(self):
        out = self.msg("我爷爷今天早上走了", mid=1)
        r = redact({"signals": out.reasons["signals"]})
        blob = json.dumps(r, ensure_ascii=False)
        self.assertIn("serious", blob)  # cue names are ours: visible
        self.assertNotIn("爷爷", blob)  # matched text is the friend's: masked

    def test_recall_leaves_no_cue_text_in_traces(self):
        # Codex P2 on #10: a stored copy of the matched words outlived 撤回
        self.msg("我爷爷今天早上走了", mid=1)
        self.p.handle(ev({"post_type": "notice", "notice_type": "group_recall", "group_id": int(GROUP),
                          "user_id": int(FRIEND), "operator_id": int(FRIEND), "message_id": 1,
                          "self_id": int(BOT), "time": self.t + 5}))
        rows = [r[0] for r in self.s.db.execute("SELECT reasons FROM traces")]
        self.assertTrue(rows)
        self.assertFalse(any("爷爷" in (r or "") or "走了" in (r or "") for r in rows))


if __name__ == "__main__":
    unittest.main()
