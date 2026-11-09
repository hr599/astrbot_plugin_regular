"""Crisis moments: a sincerity check, a caring reply, and a private DM to the owner."""

import asyncio
import types
import unittest

from adapter.actions import send_private_text
from core import crisis
from core.decide import Msg
from tests.test_live import E, Live, at_bot
from tests.test_m0 import SCOPE

SAD = "我真的撑不下去了"


def live(**kw):
    w = Live(["我在 想聊的话随时说"] * 5, **kw)
    w.dms = []

    async def notify(scope_id, text):
        w.dms.append((scope_id, text))
    w.speaker.notify = notify
    return w


def run(w, *posts):
    async def go():
        for p in posts:
            w.post(*p)
            await asyncio.sleep(0.01)
        await w.settle()
    asyncio.run(go())
    return w


class CheckTests(unittest.TestCase):
    def test_parse(self):
        for t, want in (('{"sincere": true}', True), ('{"sincere": false}', False), ('```\n{"sincere": true}\n```', True),
                        ("true", None), ('{"sincere": "yes"}', None), ('{"sincere": true, "x": 1}', None)):
            with self.subTest(t=t):
                self.assertEqual(crisis.parse(t), want)

    def test_dm_text_and_once_a_day(self):
        m = Msg(f"{SCOPE}:1", E, "7", SAD + "。" * 80, name="小王")
        t = crisis.dm_text("555", m, E)
        self.assertIn("小王", t)
        self.assertIn("12356", t)
        self.assertIn("…", t)  # the message is shortened
        w = Live()
        self.assertFalse(crisis.already_today(w.store, SCOPE, "7", E))
        crisis.record_dm(w.store, SCOPE, m.msg_id, "7", E, {}, "t")
        self.assertTrue(crisis.already_today(w.store, SCOPE, "7", E + 60))
        self.assertFalse(crisis.already_today(w.store, SCOPE, "8", E + 60))
        self.assertFalse(crisis.already_today(w.store, SCOPE, "7", E + 86400))  # a new day


class LiveCrisisTests(unittest.TestCase):
    def test_sincere_gets_care_and_a_dm_once_a_day(self):
        w = run(live(), (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.checks), 1)
        self.assertEqual(len(w.dms), 1)
        self.assertIn("小王", w.dms[0][1])
        self.assertEqual(len(w.sent), 1)  # and a caring reply in the group
        self.assertEqual(w.traces()[-1][1]["crisis"], "sincere")
        w.now = E + 600  # ten minutes later, same day
        run(w, (2, "真的好累 撑不下去了", "20002", "小王", 600))
        self.assertEqual(len(w.dms), 1)  # once per person per day
        self.assertEqual(len(w.sent), 2)

    def test_not_sincere_is_an_ordinary_message(self):
        w = live()
        w.sincere = '{"sincere": false}'
        run(w, (1, SAD, "20002", "小王"))
        self.assertEqual(w.dms, [])
        self.assertNotEqual(w.traces()[-1][1].get("rule"), "crisis")
        self.assertEqual(w.traces()[-1][1]["crisis"], "not")

    def test_cant_tell_errs_on_the_side_of_caring(self):
        for bad in (RuntimeError("timeout"), "嗯", '{"sincere": "maybe"}'):
            with self.subTest(bad=bad):
                w = live()
                w.sincere = (lambda b=bad: b) if isinstance(bad, Exception) else bad
                run(w, (1, SAD, "20002", "小王"))
                self.assertEqual(len(w.dms), 1)
                self.assertEqual(len(w.sent), 1)

    def test_jokes_dont_trigger_the_check(self):
        w = run(live(), (1, "这作业要我命 哈哈哈 不想活了", "20002", "小王"))
        self.assertEqual((w.checks, w.dms), ([], []))

    def test_an_at_with_crisis_words_also_tells_the_owner(self):
        w = run(live(), (1, at_bot(SAD), "20002", "小王"))
        self.assertEqual(len(w.dms), 1)
        self.assertEqual(len(w.sent), 1)

    def test_dry_mode_still_tells_the_owner(self):
        w = run(live(mode="dry"), (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.dms), 1)

    def test_a_failed_dm_is_traced_and_the_reply_still_goes(self):
        w = live()

        async def broken(scope_id, text):
            raise RuntimeError("no owner")
        w.speaker.notify = broken
        run(w, (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.sent), 1)
        row = w.store.db.execute("SELECT reasons FROM traces WHERE ended_at='crisis_dm'").fetchone()
        self.assertIn("RuntimeError", row["reasons"])
        self.assertNotIn(SAD, row["reasons"])  # the trace holds no text


class CaptionWaitTests(unittest.TestCase):
    def test_waits_for_a_photo_being_described(self):
        w = Live(["好香"])
        photo = [{"type": "image", "data": {"url": "https://gchat.qpic.cn/a", "subType": 0}},
                 {"type": "text", "data": {"text": " @"}}]
        w.speaker.captioning.add(f"{SCOPE}:1")

        async def go():
            w.post(1, [{"type": "at", "data": {"qq": "10001"}}] + photo[:1])

            async def describe_later():
                await asyncio.sleep(0.3)
                w.store.put_media("sha256:x", "image", "一碗烤冷面", "", E)
                w.store.link_media(f"{SCOPE}:1", 0, "sha256:x", E)
                w.speaker.captioning.discard(f"{SCOPE}:1")
            asyncio.ensure_future(describe_later())
            await asyncio.sleep(0.01)
            for _ in range(100):
                await asyncio.sleep(0.03)
                if w.prompts:
                    break
            await w.settle()
        asyncio.run(go())
        self.assertIn("[图片：一碗烤冷面]", w.prompts[0])

    def test_gives_up_after_a_while(self):
        import core.live as live_mod
        w = Live(["嗯"])
        w.speaker.captioning.add(f"{SCOPE}:1")  # never finishes
        old = live_mod.CAPTION_WAIT_S
        live_mod.CAPTION_WAIT_S = 0.5
        try:
            async def go():
                w.post(1, [{"type": "at", "data": {"qq": "10001"}},
                           {"type": "image", "data": {"url": "https://gchat.qpic.cn/a", "subType": 0}}])
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if w.prompts:
                        break
                await w.settle()
            asyncio.run(go())
        finally:
            live_mod.CAPTION_WAIT_S = old
        self.assertIn("[图片]", w.prompts[0])
        self.assertEqual(len(w.sent), 1)


class AdapterTests(unittest.TestCase):
    def test_send_private_msg(self):
        calls = []

        async def call_action(action, **kw):
            calls.append((action, kw))
        inst = types.SimpleNamespace(get_client=lambda: types.SimpleNamespace(call_action=call_action))
        ctx = types.SimpleNamespace(get_platform_inst=lambda pid: inst)
        asyncio.run(send_private_text(ctx, "llbot", "20002", "hi"))
        self.assertEqual(calls, [("send_private_msg", {"user_id": 20002,
                                                       "message": [{"type": "text", "data": {"text": "hi"}}]})])
        with self.assertRaises(ValueError):
            asyncio.run(send_private_text(ctx, "llbot", "x", "hi"))


if __name__ == "__main__":
    unittest.main()
