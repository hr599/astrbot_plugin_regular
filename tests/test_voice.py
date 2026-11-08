"""Voice stage: deterministic checks and shaping."""

import random
import unittest

from core.voice import (
    cn_int,
    check, command_like, delays, has_canary, leaked, load_rules, made_up_specifics, normalize,
    split_parts, tells,
)

PERSONA = "你是阿灯，大三，在MIT读计算机，这学期在新加坡。你话少，夜猫子，喜欢折腾Blender里的灯光。"
CANARY = "zx-canary-7f3a"


class CommandPrefixTests(unittest.TestCase):
    def test_blocks_every_spelling(self):
        for t in ["/help", "!ban 小王", "#撤回", "／禁言 @全体成员", "！roll", "＃签到", "​/kick 大鹏",
                  " /help", "“/help”", "「#签到」", "好的\n/ban 小王", "(/help)"]:
            self.assertTrue(command_like(t), repr(t))

    def test_lets_chat_through(self):
        for t in ["不发 嘿", "那个 /help 我不发", "100% 不行", "c# 我不会", "...好吧", "。。。行吧", "哈哈"]:
            self.assertFalse(command_like(t), repr(t))

    def test_check_dodges_then_drops(self):
        self.assertEqual(check("/禁言 小王", rules={}).verdict, "dodge")
        self.assertEqual(check("/禁言 小王", rules={}, attempt=1).verdict, "drop")


class LeakTests(unittest.TestCase):
    def test_copied_run_is_caught(self):
        draft = "我的设定是：你是阿灯，大三，在MIT读计算机，这学期在新加坡。"
        self.assertTrue(leaked(draft, [PERSONA]))
        self.assertEqual(check(draft, protected=[PERSONA], rules={}).verdict, "dodge")

    def test_spacing_and_width_dont_hide_it(self):
        draft = "你 是 阿 灯 ， 大 三 ， 在 ＭＩＴ 读 计 算 机"
        self.assertTrue(leaked(draft, [PERSONA]))

    def test_short_overlap_is_fine(self):
        self.assertEqual(leaked("我在新加坡 天天下雨", [PERSONA]), [])  # talking about itself is fine

    def test_canary(self):
        self.assertTrue(has_canary(f"blah {CANARY} blah", [CANARY]))
        self.assertTrue(has_canary("zx-canary-７f3a", [CANARY]))
        self.assertEqual(check(f"ok {CANARY}", canaries=[CANARY], rules={}).verdict, "dodge")


class TellTests(unittest.TestCase):
    rules = load_rules()

    def kinds(self, text, **kw):
        return {h.kind for h in tells(text, self.rules, **kw)}

    def test_assistant_habits(self):
        cases = {
            "好的！我来帮你看看": "stock_opener",
            "这样就行 希望对你有帮助～": "stock_closer",
            "还有什么想聊的吗？": "stock_closer",
            "1. 先保存\n2. 再重启": "structure",
            "**重点**是曝光": "structure",
            "首先要调曝光，其次看灯光": "structure",
            "您可以试试": "register",
            "作为一个AI，我没有感情": "assistant_self_talk",
            "家人们谁懂啊": "overcorrection",
        }
        for text, kind in cases.items():
            self.assertIn(kind, self.kinds(text), text)

    def test_period_every_line_and_haha_every_line(self):
        self.assertIn("register", self.kinds("我觉得可以。\n明天再说。"))
        self.assertIn("overcorrection", self.kinds("哈哈哈好\n哈哈哈哈行"))

    def test_sincere_ai_question_allows_self_talk(self):
        self.assertNotIn("assistant_self_talk", self.kinds("是AI 作为AI这句是真心的", sincere_ai=True))

    def test_length_vs_group(self):
        self.assertIn("length", self.kinds("很" * 40, group_p90_len=30))
        self.assertNotIn("length", self.kinds("很" * 20, group_p90_len=30))

    def test_natural_lines_pass(self):
        for t in ["刚下课 饿死", "等下 这个梗我没跟上", "渲染跑了一晚上 结果灯打反了", "啊对 我记错了",
                  "好看 逆光拍的吧", "睡了 明天还有课", "是AI 屏幕后面没人 不过这句是真心的"]:
            self.assertEqual(self.kinds(t), set(), t)

    def test_rewrite_then_drop(self):
        self.assertEqual(check("好的！没问题", context="").verdict, "rewrite")
        self.assertEqual(check("好的！没问题", context="", attempt=1).verdict, "drop")


class SpecificsTests(unittest.TestCase):
    def test_invented_specifics_flagged(self):
        hits = {h.kind for h in made_up_specifics("讲座是11月15号下午3点 门票50块", context="讲座要不要去")}
        self.assertTrue({"specific_date", "specific_time", "specific_price"} <= hits, hits)

    def test_specifics_from_context_are_fine(self):
        ctx = "老周: 讲座是周二 下午3点"
        self.assertEqual(made_up_specifics("周二下午3点 我记下了", ctx), [])

    def test_decimal_prefix_is_not_the_number(self):
        # PR #9 review: "12" isn't backed by "12.5"
        self.assertEqual(check("票价￥12", context="票价￥12.5", rules={}).verdict, "rewrite")
        self.assertEqual(check("温度是12度", context="温度是12.5度", rules={}).verdict, "rewrite")
        self.assertEqual(check("票价￥12.5", context="票价￥12.5", rules={}).verdict, "send")

    def test_weekday_next_to_time(self):
        # PR #9 review: two typed tokens, not one number; spacing in context doesn't matter
        self.assertEqual(check("星期二12点见", context="星期二12点见", rules={}).verdict, "send")
        self.assertEqual(check("周二 12点见", context="周二 12点见", rules={}).verdict, "send")
        self.assertEqual(check("周二12点见", context="周三12点见", rules={}).verdict, "rewrite")

    def test_chinese_numerals(self):
        # PR #9 review: 五十块 / 下午三点 get the same check as 50块 / 3点
        hits = {h.kind for h in made_up_specifics("门票五十块，下午三点见", "")}
        self.assertEqual(hits, {"specific_price", "specific_time"})
        self.assertEqual(made_up_specifics("门票五十块，下午三点见", "门票50块 下午3点"), [])
        self.assertEqual(made_up_specifics("门票50块，三点半见", "门票五十块 3点半"), [])
        self.assertEqual(len(made_up_specifics("门票四十块，下午四点见", "门票50块 下午3点")), 2)
        self.assertEqual(cn_int("一百五"), 150)
        self.assertEqual(cn_int("一百零五"), 105)
        self.assertEqual(cn_int("两千五"), 2500)
        # everyday words that look like numbers aren't specifics
        self.assertEqual(made_up_specifics("好一点了 差一点 我们一块去吧 有两点建议", ""), [])

    def test_specifics_round_two(self):
        # PR #9 review, second round
        v = lambda a, b="": check(a, context=b, rules={}).verdict  # noqa: E731
        # Chinese numerals at the end of the text; 一元 is a price (only 一块 can mean "together")
        self.assertEqual(v("集合时间是三点"), "rewrite")
        self.assertEqual(v("门票一元"), "rewrite")
        self.assertEqual(v("我们一块去看看"), "send")
        # currency and exact amounts are kept
        self.assertEqual(v("门票50美元", "门票50元"), "rewrite")
        self.assertEqual(v("￥50", "$50"), "rewrite")
        self.assertEqual(v("价格10000.01元", "价格10000.02元"), "rewrite")
        self.assertEqual(v("门票五十元", "门票50块"), "send")
        self.assertEqual(v("五毛", "0.5元"), "send")
        # 上午/下午 kept; a side that doesn't say matches either; 15:00 is 下午3点
        self.assertEqual(v("下午三点见", "上午三点见"), "rewrite")
        self.assertEqual(v("下午三点见", "15:00见"), "send")
        self.assertEqual(v("三点见", "下午3点见"), "send")
        # a full date backs its parts, not the reverse
        self.assertEqual(v("15号见", "11月15号见"), "send")
        self.assertEqual(v("11月见", "11月15号见"), "send")
        self.assertEqual(v("11月15号见", "15号见"), "rewrite")

    def test_specifics_round_three(self):
        v = lambda a, b="": check(a, context=b, rules={}).verdict  # noqa: E731
        self.assertEqual(v("票价3万元", "票价5万元"), "rewrite")
        self.assertEqual(v("票价3万元", "票价30000元"), "send")
        self.assertEqual(v("温度是-12度", "温度是12度"), "rewrite")
        self.assertEqual(v("温度是零下12度", "温度是-12度"), "send")
        self.assertEqual(v("1月5号见", "01月05号见"), "send")
        self.assertEqual(v("5号见", "01月05号见"), "send")

    def test_runs_of_punctuation_stay_attached(self):
        # PR #9 review: a lone "！" part was dropped by the command check
        for t in ("太好了！！", "什么？！", "真的吗？？"):
            self.assertEqual(split_parts(t), [t])
            self.assertEqual(check(t, rules={}).verdict, "send")
        self.assertEqual(split_parts("好的！！明天见"), ["好的！！", "明天见"])
        # a split that really produces a command is still blocked
        self.assertEqual(check("好的！/help", rules={}).verdict, "drop")

    def test_altered_number_is_not_supported_by_a_longer_one(self):
        # Codex P2 on #9: "50块" sits inside "150块" but is a different price
        self.assertEqual(check("门票50块", context="门票150块", rules={}).verdict, "rewrite")
        self.assertEqual([h.detail for h in made_up_specifics("房号12", "房号120")], ["12"])
        self.assertEqual(made_up_specifics("门票150块", "门票150块"), [])
        self.assertEqual(made_up_specifics("50块的那个", "￥50块"), [])

    def test_made_up_quote(self):
        hits = made_up_specifics("他原话是“我明天一定到”", context="他说会来")
        self.assertEqual([h.kind for h in hits], ["specific_quote"])

    def test_no_numbers_no_hits(self):
        self.assertEqual(made_up_specifics("好像是周末吧 不太确定", context=""), [])


class ShapingTests(unittest.TestCase):
    def test_split_into_bursts(self):
        self.assertEqual(split_parts("刚下课。饿死了！你们吃了吗？"), ["刚下课", "饿死了！", "你们吃了吗？"])
        self.assertEqual(len(split_parts("一。二。三。四。五。")), 3)
        self.assertEqual(split_parts("一句话就够"), ["一句话就够"])

    def test_sent_text_keeps_chinese_punctuation(self):
        out = check("真的吗？太好了！", context="", rng=random.Random(0))
        self.assertEqual(out.parts, ["真的吗？", "太好了！"])
        self.assertFalse(any("\u200b" in p for p in split_parts("好\u200b的呀")))

    def test_delays_are_human(self):
        d = delays(["刚下课", "饿死了"], random.Random(1))
        self.assertTrue(1.5 <= d[0] <= 12 and 0.4 <= d[1] <= 12)
        self.assertLessEqual(max(delays(["很" * 500], random.Random(1))), 12.0)

    def test_send_path(self):
        out = check("刚下课 饿死。你们吃了吗", context="", rng=random.Random(0))
        self.assertEqual(out.verdict, "send")
        self.assertEqual(len(out.parts), len(out.delays))

    def test_normalize(self):
        self.assertEqual(normalize("ＭＩＴ​／"), "MIT/")


if __name__ == "__main__":
    unittest.main()
