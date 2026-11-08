"""M2a: /reg bench tune, and shadow decide running the M2 rules live."""

import json
import tempfile
import unittest
from pathlib import Path

from bench.commands import bench_command
from bench.situations import Recording, load_fixtures
from bench.tune import GRID, MIN_DEV_LABELS, PARAMS_FILE, dedupe, get, tune, tune_command
from core.decide_rules import POLICY as M2_POLICY, load_params
from core.gate import GateConfig
from core.pipeline import Pipeline
from services.store import Store
from tests.test_m0 import GROUP, ev, group_msg


def real_like():
    """The synthetic sets, presented as if they were your own saved recordings, so
    the held-out split applies (fixtures are never tuned on)."""
    fx = {r.name: r for r in load_fixtures()}
    return [Recording(n, "saved", fx[n].messages, fx[n].labels) for n in ("synthetic_review_v1", "hard_gates_v1")]


class TuneTests(unittest.TestCase):
    def test_tune_improves_dev_and_keeps_gates(self):
        res = tune(real_like(), load_params())
        self.assertTrue(res["ok"])
        before, after = res["objective"]
        self.assertGreaterEqual(after, before)
        self.assertEqual(res["after"]["explicit_missed"], 0)
        self.assertEqual(res["after"]["bot_loop_breaks"], 0)
        self.assertTrue(set(res["changed"]) <= set(GRID))
        for k, (old, new) in res["changed"].items():
            self.assertEqual(get(load_params(), k), old)
            self.assertIn(new, GRID[k])
        # held-out labels are reported, and never more than the split's share
        self.assertGreater(res["after"]["held_out"]["labeled"], 0)
        self.assertLess(res["after"]["held_out"]["labeled"], res["after"]["dev"]["labeled"])

    def test_fixtures_are_never_tuned_on(self):
        res = tune(load_fixtures(), load_params())
        self.assertFalse(res["ok"])
        self.assertEqual(res["dev_labeled"], 0)

    def test_too_few_labels(self):
        r = real_like()[0]
        few = Recording(r.name, "saved", r.messages, dict(list(r.labels.items())[:20]))
        res = tune([few], load_params())
        self.assertFalse(res["ok"])
        self.assertLess(res["dev_labeled"], MIN_DEV_LABELS)

    def test_dedupe_counts_each_label_once(self):
        r = real_like()[0]
        twice = dedupe([r, Recording("copy", "saved", r.messages, dict(r.labels))])
        self.assertEqual(len(twice), 1)

    def test_apply_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            calls = []
            out = tune_command(real_like(), data, [], on_change=lambda: calls.append(1))
            self.assertIn("调参", out)
            for r in real_like():
                for m in r.messages[:50]:
                    if len(m.text) > 6:
                        self.assertNotIn(m.text, out)
            last = json.loads((data / "bench" / "tune-last.json").read_text(encoding="utf-8"))
            out = tune_command([], data, ["apply"], on_change=lambda: calls.append(1))
            if last["changed"]:
                self.assertTrue((data / PARAMS_FILE).exists())
                p = load_params(data)
                for k, (_, new) in last["changed"].items():
                    self.assertEqual(get(p, k), new)
                self.assertEqual(calls, [1])
                # untouched knobs keep the harness defaults
                self.assertEqual(p["interest_keywords"], load_params()["interest_keywords"])
                self.assertIn("已删掉", tune_command([], data, ["reset"], on_change=lambda: calls.append(1)))
                self.assertFalse((data / PARAMS_FILE).exists())
            self.assertIn("默认", tune_command([], data, ["reset"]))

    def test_apply_before_tune(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("先运行", tune_command([], Path(d), ["apply"]))

    def test_bench_command_routes_tune(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            st = Store(data / "regular.db")
            out = bench_command(st, data, ["tune"])
            st.close()
            self.assertIn(str(MIN_DEV_LABELS), out)  # nothing labelled yet


class LiveShadowTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "regular.db")

    def tearDown(self):
        self.s.close()
        self.d.cleanup()

    def test_m2_shadow_keeps_baseline_for_comparison(self):
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}), params=load_params())
        self.assertEqual(p.harness_version, M2_POLICY)
        out = p.handle(ev(group_msg("阿灯你觉得这个怎么样", mid=1)))
        sh = out.reasons["shadow"]
        self.assertEqual(sh["policy"], M2_POLICY)
        self.assertEqual(sh["would"], "reply")       # vocative: M2 answers
        self.assertEqual(sh["baseline"], "quiet")    # the baseline never answers a name

    def test_without_params_stays_baseline(self):
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        out = p.handle(ev(group_msg("阿灯你觉得这个怎么样", mid=1)))
        self.assertNotIn("baseline", out.reasons["shadow"])
        self.assertNotEqual(p.harness_version, M2_POLICY)


if __name__ == "__main__":
    unittest.main()


class Review26Tests(unittest.TestCase):
    """PR #26 review findings, one test each."""
    EVENING = 1_793_451_600  # Sat 21:00 UTC+8

    def test_second_apply_keeps_earlier_tuned_values(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / PARAMS_FILE).write_text(json.dumps(
                {"reply_chime": 0.8, "chime": {"interest": 0.6}, "heat_quiet": 0.7}), encoding="utf-8")
            base = load_params(data)
            res = tune(real_like(), base)
            (data / "bench").mkdir()
            (data / "bench" / "tune-last.json").write_text(json.dumps({**res, "when": "t"}), encoding="utf-8")
            if not res["changed"]:
                self.skipTest("search kept everything")
            tune_command([], data, ["apply"])
            applied = load_params(data)
            for k in GRID:  # the applied setting is exactly the one that was evaluated
                want = res["changed"][k][1] if k in res["changed"] else get(base, k)
                self.assertEqual(get(applied, k), want, k)
            self.assertEqual(applied["heat_quiet"], 0.7)  # a hand-edited key outside the grid stays

    def test_merged_burst_still_counts_every_at(self):
        from core.decide import Msg, decide
        from core.decide_rules import decide_m2
        from bench.runner import replay
        msgs = [Msg(f"a{i}", self.EVENING + t, "p1", "在吗", mentions_bot=True) for i, t in enumerate((0, 1, 2, 3, 10))]
        for policy in (decide, decide_m2):
            with self.subTest(policy=policy.__name__):
                run = replay(Recording("burst", "fixture", msgs), policy, params=load_params())
                last = run.steps[-1].decision
                self.assertEqual((last.action, last.reasons.get("rule")), ("react", "rate_limited"))

    def test_crisis_with_a_name_still_gets_a_reply(self):
        from core import limits
        from core.decide import Msg
        from core.decide_rules import decide_m2
        for text in ("我真的撑不下去了", "我真的撑不下去了 阿灯救救我"):
            with self.subTest(text=text):
                d = decide_m2(Msg("c", self.EVENING, "p1", text), [], self.EVENING,
                              aliases=("阿灯", "灯"), state=limits.LimitState(), params=load_params())
                self.assertEqual((d.action, d.reasons["rule"]), ("reply", "crisis"))

    def test_reply_in_parts_keeps_the_follow_up(self):
        from core import limits
        from core.decide import Msg
        from core.decide_rules import decide_m2
        E = self.EVENING
        for parts in (1, 2):
            with self.subTest(parts=parts):
                hist = [Msg("h", E, "A", "今天好累")] + [Msg(f"b{i}", E + 2 + i, "bot", "辛苦了", is_bot=True)
                                                        for i in range(parts)]
                st = limits.LimitState.from_moments(hist)
                d = decide_m2(Msg("f", E + 10, "A", "然后呢"), hist, E + 10, aliases=("阿灯",), state=st,
                              params=load_params())
                self.assertEqual(d.action, "reply")
                self.assertEqual(d.address_kind, "follow_up")


class Review26LiveTests(unittest.TestCase):
    EVENING = 1_793_451_600

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "regular.db")

    def tearDown(self):
        self.s.close()
        self.d.cleanup()

    def test_live_and_replay_agree_on_an_at_for_someone_else(self):
        from bench.runner import replay
        from bench.situations import from_store
        from core.decide_rules import decide_m2
        from tests.test_m0 import SCOPE
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}), params=load_params())
        segs = [{"type": "at", "data": {"qq": "20002"}}, {"type": "text", "data": {"text": " 有人会blender吗"}}]
        live = p.handle(ev(group_msg(segs, mid=1, ts=self.EVENING))).reasons["shadow"]["would"]
        rec = from_store(self.s, SCOPE)
        replayed = replay(rec, decide_m2, params=load_params()).steps[-1].decision.action
        self.assertEqual(live, replayed)
        self.assertEqual(live, "quiet")

    def test_serious_mode_survives_a_restart_after_midnight(self):
        E = self.EVENING
        outs = []
        for restart in (False, True):
            with tempfile.TemporaryDirectory() as d:
                s = Store(Path(d) / "regular.db")
                p = Pipeline(s, GateConfig(group_ids={GROUP}), params=load_params())
                p.handle(ev(group_msg("我家猫去世了", mid=1, ts=E + 10_500)))      # 23:55 UTC+8
                p.handle(ev(group_msg("抱抱", uid="30003", mid=2, ts=E + 10_700)))  # 23:58
                if restart:
                    p = Pipeline(s, GateConfig(group_ids={GROUP}), params=load_params())
                sh = p.handle(ev(group_msg("有人会blender吗", uid="40004", mid=3, ts=E + 11_100))).reasons["shadow"]
                outs.append((sh["would"], sh["rule"]))
                s.close()
        self.assertEqual(outs[0], ("quiet", "serious"))
        self.assertEqual(outs[1], outs[0])
