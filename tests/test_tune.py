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
