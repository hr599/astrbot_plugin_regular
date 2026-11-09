"""Emoji reactions: which emoji, how often, and the live path."""

import asyncio
import random
import types
import unittest

from core import reactions as rx
from core.decide_rules import merge
from tests.test_live import E, Live, at_bot
from tests.test_m0 import SCOPE

RULES = rx.load_rules()


def react_always(w):
    """Rules that turn every not-for-him message into a reaction (test only)."""
    w.p.params = merge(w.p.params, {"react_chime": 0.0, "reply_chime": 99.0})


class PickTests(unittest.TestCase):
    def test_cue_picks_from_its_pool(self):
        rng = random.Random(0)
        for text, media, cue in (("哈哈哈哈笑死", [], "laughter"), ("[图片]", ["image"], "image"),
                                 ("明天几点？", [], "question"), ("好的", [], "default")):
            with self.subTest(text=text):
                self.assertEqual(rx.cue_of(text, media), cue)
                for _ in range(10):
                    self.assertIn(rx.pick(text, media, [], 0, RULES, rng), RULES["by_cue"][cue])

    def test_the_groups_favourites_once_there_are_enough(self):
        favs = ["277", "305"]
        seen = {rx.pick("好的", [], favs, 10, RULES, random.Random(i)) for i in range(40)}
        self.assertTrue(seen & set(favs))                      # sometimes the group's own
        self.assertTrue(seen & set(RULES["by_cue"]["default"]))  # sometimes the defaults
        few = {rx.pick("好的", [], favs, 2, RULES, random.Random(i)) for i in range(40)}
        self.assertFalse(few & set(favs))  # too few reactions to go by

    def test_only_digits_ever_go_out(self):
        # a bad id in the config or among the group's favourites never reaches OneBot
        bad = {**RULES, "by_cue": {"default": ["76; x"]}, "group_share": 0.0}
        self.assertEqual(rx.pick("好的", [], [], 0, bad, random.Random(0)), rx.SAFE)
        mixed = {**RULES, "group_share": 1.0}
        for i in range(20):
            self.assertTrue(rx.pick("好的", [], ["1; x"], 99, mixed, random.Random(i)).isdigit())

    def test_cap(self):
        cap = rx.Cap(2)
        self.assertTrue(cap.take("g", 0))
        self.assertTrue(cap.take("g", 10))
        self.assertFalse(cap.take("g", 20))
        self.assertTrue(cap.take("other", 20))
        self.assertTrue(cap.take("g", 3601))


class FavouritesTests(unittest.TestCase):
    def test_counts_the_groups_reactions_not_removals_or_bots(self):
        w = Live()
        for i, (ids, add, bot) in enumerate([(["277"], True, False), (["277", "76"], True, False),
                                             (["305"], False, False), (["999"], True, True)]):
            w.store.trace(scope_id=SCOPE, msg_id=None, ended_at="gate", decision=None, harness_version="t",
                          reasons={"notice": "emoji_like", "is_add": add, "emoji_ids": ids, "from_bot": bot},
                          ts=E + i)
        favs, total = rx.group_favourites(w.store, SCOPE, E - 10)
        self.assertEqual((favs[0], total), ("277", 3))
        self.assertNotIn("305", favs)
        self.assertNotIn("999", favs)


class LiveReactionTests(unittest.TestCase):
    def live(self, mode="on"):
        w = Live(mode=mode)
        react_always(w)
        w.reacted = []

        async def react(scope_id, msg_id, emoji):
            w.reacted.append((scope_id, msg_id, emoji))
        w.speaker.react = react
        return w

    def test_a_reaction_lands_on_the_message_without_a_model_call(self):
        async def go():
            w = self.live()
            w.post(1, "哈哈哈哈笑死我了")
            await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual(len(w.reacted), 1)
        scope, msg, emoji = w.reacted[0]
        self.assertEqual((scope, msg), (SCOPE, f"{SCOPE}:1"))
        self.assertIn(emoji, RULES["by_cue"]["laughter"])
        self.assertEqual((w.prompts, w.sent), ([], []))
        decision, info = w.traces()[-1]
        self.assertEqual((decision, info["emoji"]), ("react", emoji))

    def test_dry_mode_decides_but_doesnt_react(self):
        async def go():
            w = self.live(mode="dry")
            w.post(1, "哈哈哈")
            await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual(w.reacted, [])
        self.assertTrue(w.traces()[-1][1]["dry"])

    def test_cap_per_hour(self):
        async def go():
            w = self.live()
            w.speaker.rx_cap = rx.Cap(2)
            for i in range(4):
                w.post(i + 1, f"哈哈{i}", uid=str(20002 + i), name=f"人{i}")
            await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual(len(w.reacted), 2)
        self.assertEqual(sum(1 for d, i in w.traces() if i.get("refused")), 2)

    def test_no_reaction_into_a_group_paused_during_the_wait(self):
        # PR #37 review: /reg pause during the 4 s pause; text goes through the outbox's
        # pause check, reactions don't, so the speaker checks it itself
        async def go(pause):
            w = self.live()
            w.post(1, "刚才看见阿灯了")
            await asyncio.sleep(0.01)
            if pause:
                w.p.gate_cfg.paused_groups.add(SCOPE.rsplit(":", 1)[-1])
            await w.settle()
            return w
        paused = asyncio.run(go(True))
        self.assertEqual(paused.reacted, [])
        self.assertEqual(paused.traces()[-1][1]["refused"], "group paused")
        control = asyncio.run(go(False))
        self.assertEqual(len(control.reacted), 1)

    def test_no_reaction_after_a_mode_change_or_recall_during_the_wait(self):
        for how in ("mode", "recall"):
            with self.subTest(how=how):
                async def go():
                    w = self.live()
                    w.post(1, "哈哈哈")
                    await asyncio.sleep(0.01)
                    if how == "mode":
                        w.outbox.set_mode("dry")
                        w.outbox.set_mode("on")
                    else:
                        w.store.recall(f"{SCOPE}:1")
                    await w.settle()
                    return w
                w = asyncio.run(go())
                self.assertEqual(w.reacted, [])

    def test_a_failed_reaction_is_just_quiet(self):
        async def go():
            w = self.live()

            async def broken(*a):
                raise RuntimeError("no such action")
            w.speaker.react = broken
            w.post(1, "哈哈哈")
            await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual(w.traces()[-1][1]["error"], "RuntimeError")

    def test_an_at_is_still_a_reply_not_a_reaction(self):
        async def go():
            w = self.live()
            w.answers = ["在"]
            w.post(1, at_bot("在吗"))
            await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual((len(w.sent), w.reacted), (1, []))


class AdapterTests(unittest.TestCase):
    def test_set_msg_emoji_like_with_numbers_only(self):
        from adapter.actions import react_group_msg
        calls = []

        async def call_action(action, **kw):
            calls.append((action, kw))
        inst = types.SimpleNamespace(get_client=lambda: types.SimpleNamespace(call_action=call_action))
        ctx = types.SimpleNamespace(get_platform_inst=lambda pid: inst)
        asyncio.run(react_group_msg(ctx, "llbot", "12345", "76"))
        self.assertEqual(calls, [("set_msg_emoji_like", {"message_id": 12345, "emoji_id": "76"})])
        with self.assertRaises(ValueError):
            asyncio.run(react_group_msg(ctx, "llbot", "12345", "76; x"))


if __name__ == "__main__":
    unittest.main()
