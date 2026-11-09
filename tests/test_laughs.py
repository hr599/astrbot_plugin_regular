"""Live, Nov 9: nearly every reply ended in "hhh"."""

import asyncio
import unittest

from core.voice import trim_laughs
from tests.test_live import E, Live


class TrimTests(unittest.TestCase):
    def test_one_laugh_ending_in_a_while_is_fine(self):
        self.assertEqual(trim_laughs(["我也没写完 hhh"], ["好", "记下了"]), (["我也没写完 hhh"], False))

    def test_a_second_one_is_trimmed(self):
        for draft, want in (("我也没写完 hhh", "我也没写完"), ("又是烤冷面哈哈哈", "又是烤冷面"),
                            ("行吧233", "行吧"), ("笑死 HHH~", "笑死"), ("那你先睡 hh", "那你先睡")):
            with self.subTest(draft=draft):
                self.assertEqual(trim_laughs([draft], ["我在 hhh"]), ([want], True))

    def test_laughs_mid_line_and_bare_laughs(self):
        self.assertEqual(trim_laughs(["哈哈哈哈 你认真的吗"], ["好 hhh"]), (["哈哈哈哈 你认真的吗"], False))
        self.assertEqual(trim_laughs(["hhh"], ["好 hhh"]), (["hhh"], False))  # the whole reply is a laugh
        self.assertEqual(trim_laughs(["真的假的", "hhh"], ["好 hhh"]), (["真的假的"], True))
        self.assertEqual(trim_laughs(["哈哈哈哈哈"], ["好 hhh"]), (["哈哈哈哈哈"], False))

    def test_numbers_and_words_ending_like_a_laugh_stay(self):
        """PR #41 review: "房间号是1233" went out as "房间号是1"."""
        for draft in ("房间号是1233", "ddl是2233号", "房间号是1233 ", "Ahh", "github用户名是xhh", "密码是A233"):
            with self.subTest(draft=draft):
                self.assertEqual(trim_laughs([draft], ["我在 hhh"]), ([draft], False))

    def test_only_the_last_few_lines_count(self):
        self.assertFalse(trim_laughs(["好 hhh"], ["a hhh", "b", "c", "d", "e"])[1])


class LiveTests(unittest.TestCase):
    def test_he_doesnt_end_every_reply_with_a_laugh(self):
        from tests.test_live import at_bot

        async def go():
            w = Live(["我也没写完 hhh", "那你先睡 hhh", "明天见 hhh"])
            for i, t in enumerate(["作业写完没", "我先睡了", "拜拜"]):
                w.now = E + 600 * i
                w.post(1 + i, at_bot(t), dt=600 * i)
                await w.settle()
            return w
        w = asyncio.run(go())
        sent = ["".join(j.parts) for j in w.sent]
        self.assertEqual(sent, ["我也没写完 hhh", "那你先睡", "明天见"])
        self.assertEqual([i.get("trimmed") for d, i in w.traces() if d == "reply"],
                         [None, "laugh ending", "laugh ending"])

    def test_a_number_ending_in_233_is_sent_whole(self):
        from tests.test_live import at_bot

        async def go():
            w = Live(["我在 hhh", "房间号是1233"])
            for i, t in enumerate(["在吗", "房间号是1233，帮我复述一下"]):
                w.now = E + 600 * i
                w.post(1 + i, at_bot(t), dt=600 * i)
                await w.settle()
            return w
        w = asyncio.run(go())
        self.assertEqual(["".join(j.parts) for j in w.sent], ["我在 hhh", "房间号是1233"])


if __name__ == "__main__":
    unittest.main()
