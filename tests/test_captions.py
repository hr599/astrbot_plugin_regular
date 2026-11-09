"""Image captions: describe photos on arrival, keep only text, show it to the models."""

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path

from adapter.llm import make_call
from adapter.media import allowed, photo_links
from core import captions as cap
from core.budget import Budget, BudgetExceeded, Usage, load_prices
from core.decide import Msg
from core.decide_model import build_prompt
from core.gate import GateConfig
from core.labeling import Labeler
from core.llm import IMAGE_TOKEN_BOUND, ModelClient
from core.moments import stored_moments
from core.pipeline import Pipeline
from services.store import Store
from tests.test_m0 import GROUP, SCOPE, ev, group_msg

E = 1_793_451_600
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels-1" * 50
PNG2 = b"\x89PNG\r\n\x1a\n" + b"pixels-2" * 50


def photo(url="https://multimedia.nt.qq.com.cn/download?rkey=abc", sub=0):
    return {"type": "image", "data": {"url": url, "subType": sub, "file": "x.jpg"}}


class World:
    """A real store with a photo message, and a fake caption model."""

    def __init__(self, answer='{"caption": "一碗烤冷面，上面撒了葱花", "text": ""}', monthly=30):
        self.d = Path(tempfile.mkdtemp())
        self.store = Store(self.d / "regular.db")
        self.p = Pipeline(self.store, GateConfig(group_ids={GROUP}), captions=True)
        self.calls = []
        self.answer = answer

        async def call(pid, system, prompt, params):
            self.calls.append((system, prompt, params))
            a = self.answer(params) if callable(self.answer) else self.answer
            if isinstance(a, Exception):
                raise a
            return a, Usage(input_other=900, output=20), "stop"

        self.budget = Budget(self.store, load_prices(), monthly={"deepseek": monthly, "openai": 30}, usd_to_cny=7.0)
        self.client = ModelClient(self.budget, providers={"caption": "ds"}, model_of=lambda pid: "deepseek-flash",
                                  call=call, clock=lambda: E)

    def post(self, mid, segs, uid="20002", name="小王", ts=None):
        return self.p.handle(ev(group_msg(segs, uid=uid, mid=mid, ts=ts or E + mid, name=name)))


class CaptionTests(unittest.TestCase):
    def test_parse_mime_and_number_masking(self):
        self.assertEqual(cap.parse('{"caption": "一只猫", "text": ""}'), ("一只猫", ""))
        self.assertEqual(cap.parse('```json\n{"caption": "截图", "text": "加我 QQ 1234567890"}\n```'),
                         ("截图", "加我 QQ [号码]"))
        for bad in ("一只猫", '{"caption": "x"}', '{"caption": "", "text": ""}', '{"caption": 1, "text": ""}',
                    '{"caption": "x", "text": "", "who": "小王"}'):
            with self.subTest(bad=bad):
                self.assertIsNone(cap.parse(bad))
        self.assertEqual(cap.mime_of(PNG), "image/png")
        self.assertEqual(cap.mime_of(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertIsNone(cap.mime_of(b"<html>not an image"))
        self.assertIn("不要认人", cap.SYSTEM)

    def test_describe_stores_text_only_and_reuses_by_content(self):
        w = World()
        w.post(1, [photo()])
        w.post(2, [photo()])
        r = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        self.assertEqual(r.described, 1)
        system, prompt, params = w.calls[0]
        self.assertEqual(params["images"][0][0], "image/png")  # sent as an image
        r2 = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:2", [(0, PNG)], now=E))
        self.assertEqual((r2.reused, len(w.calls)), (1, 1))  # same image: no second call
        caps = w.store.captions_for([f"{SCOPE}:1", f"{SCOPE}:2"])
        self.assertEqual(caps[f"{SCOPE}:1"], [("一碗烤冷面，上面撒了葱花", "")])
        # nothing but text and a hash is kept
        rows = w.store.db.execute("SELECT * FROM media").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["media_key"].startswith("sha256:"))
        blob = json.dumps([dict(r) for r in rows], ensure_ascii=False)
        self.assertNotIn("pixels", blob)
        self.assertEqual(w.store.db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0], 0)

    def test_skips_failures_and_budget(self):
        w = World()
        w.post(1, [photo(), photo(), photo()])
        r = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1",
                                     [(0, b"not an image"), (1, PNG[:8] + b"x" * (cap.MAX_BYTES + 1))], now=E))
        self.assertEqual((r.skipped, len(w.calls)), (2, 0))
        w.answer = RuntimeError("boom")
        r = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        self.assertEqual(r.errors, {"RuntimeError": 1})
        w.answer = "这是一只猫"
        r = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        self.assertEqual(r.errors, {"unparsed": 1})
        self.assertEqual(w.store.captions_for([f"{SCOPE}:1"]), {})
        poor = World(monthly=0.0001)
        poor.post(1, [photo()])
        r = asyncio.run(cap.describe(poor.client, poor.store, f"{SCOPE}:1", [(0, PNG), (1, PNG2)], now=E))
        self.assertEqual((r.over_budget, len(poor.calls)), (1, 0))

    def test_forget_or_recall_during_the_call_writes_nothing(self):
        w = World()
        w.post(1, [photo()])

        def forget(params):
            w.store.cache_gen += 1
            return '{"caption": "一只猫", "text": ""}'
        w.answer = forget
        r = asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        self.assertEqual(r.forgotten, 1)
        self.assertEqual(w.store.db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

        w2 = World()
        w2.post(1, [photo()])

        def recall(params):
            w2.store.recall(f"{SCOPE}:1")
            return '{"caption": "一只猫", "text": ""}'
        w2.answer = recall
        asyncio.run(cap.describe(w2.client, w2.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        self.assertEqual(w2.store.db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_captions_live_as_long_as_their_messages(self):
        w = World()
        w.post(1, [photo()], uid="20002")
        w.post(2, [photo()], uid="30003", name="小李")
        w.post(3, [photo()], uid="30003", name="小李")
        for mid, img in ((1, PNG), (2, PNG2), (3, PNG)):
            asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:{mid}", [(0, img)], now=E))
        count = lambda: w.store.db.execute("SELECT COUNT(*) FROM media").fetchone()[0]  # noqa: E731
        self.assertEqual(count(), 2)
        w.store.recall(f"{SCOPE}:2")  # PNG2 only appeared here
        self.assertEqual(count(), 1)
        w.store.forget_person("aiocqhttp", "20002")  # PNG is still in 小李's message 3
        self.assertEqual(count(), 1)
        w.store.purge_before(E + 10)
        self.assertEqual(count(), 0)

    def test_budget_counts_a_worst_case_per_image(self):
        w = World()
        text_only = w.budget.check("deepseek-flash", "x", 100, E)
        with_image = w.budget.check("deepseek-flash", "x", 100, E, extra_input_tokens=IMAGE_TOKEN_BOUND)
        self.assertGreater(with_image, text_only)

    def test_cache_key_has_the_hash_never_the_bytes(self):
        w = World()
        asyncio.run(w.client.generate("caption", prompt="p", images=[("image/png", PNG)], cache=True))
        row = w.store.db.execute("SELECT * FROM llm_cache").fetchone()
        self.assertNotIn("pixels", json.dumps(dict(row), ensure_ascii=False))
        asyncio.run(w.client.generate("caption", prompt="p", images=[("image/png", PNG2)], cache=True))
        self.assertEqual(len(w.calls), 2)  # another image is another request

    def test_adapter_sends_images_as_vision_parts(self):
        seen = {}

        async def create(**kw):
            seen.update(kw)
            msg = types.SimpleNamespace(content='{"caption": "猫", "text": ""}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                         usage=None)
        prov = types.SimpleNamespace(client=types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create))), get_model=lambda: "deepseek-flash")
        ctx = types.SimpleNamespace(get_provider_by_id=lambda pid: prov)
        asyncio.run(make_call(ctx)("ds", "sys", "描述这张图。",
                                   {"max_tokens": 50, "thinking": "off", "images": [("image/png", "QUJD")]}))
        user = seen["messages"][-1]["content"]
        self.assertEqual(user[0], {"type": "text", "text": "描述这张图。"})
        self.assertEqual(user[1]["image_url"]["url"], "data:image/png;base64,QUJD")


class FetchAndPipelineTests(unittest.TestCase):
    def test_only_photos_from_qq_hosts(self):
        raw = {"message": [photo(), photo(sub=1), {"type": "text", "data": {"text": "hi"}},
                           photo("https://evil.example/x.jpg"), photo("file:///etc/passwd"),
                           photo("https://gchat.qpic.cn/a.jpg")]}
        self.assertEqual([p for p, _ in photo_links(raw)], [0, 3])  # the sticker isn't counted
        self.assertTrue(allowed("https://multimedia.nt.qq.com.cn/x"))
        self.assertFalse(allowed("https://qq.com.evil.example/x"))
        self.assertFalse(allowed("http://127.0.0.1/x"))

    def test_pipeline_flags_photos_not_stickers_or_bots(self):
        w = World()
        out = w.post(1, [photo(), photo(sub=1), {"type": "text", "data": {"text": "看"}}])
        self.assertEqual(out.reasons["caption_photos"], 1)
        bots = Pipeline(w.store, GateConfig(group_ids={GROUP}, bot_uids={"70007"}), captions=True)
        self.assertEqual(bots.handle(ev(group_msg([photo()], uid="70007", mid=2, ts=E + 2))).reasons["caption_photos"], 0)
        off = Pipeline(w.store, GateConfig(group_ids={GROUP}), captions=False)
        self.assertEqual(off.handle(ev(group_msg([photo()], mid=3, ts=E + 3))).reasons["caption_photos"], 0)

    def test_models_and_labeling_see_the_caption(self):
        w = World()
        w.post(1, [photo()], name="小王")
        w.post(2, "好香", uid="30003", name="小李")
        asyncio.run(cap.describe(w.client, w.store, f"{SCOPE}:1", [(0, PNG)], now=E))
        ms = stored_moments(w.store, SCOPE)
        p = build_prompt(ms[1], ms[:1], E + 2)
        self.assertIn("小王：[图片：一碗烤冷面，上面撒了葱花]", p)
        live = w.p._context(f"{SCOPE}:2")
        self.assertEqual(live[0].captions, ["一碗烤冷面，上面撒了葱花"])
        lab = Labeler(w.store)
        rows = [w.store.db.execute("SELECT m.*, '小李' AS sender_name FROM messages m WHERE msg_id=?",
                                   (f"{SCOPE}:2",)).fetchone()]
        self.assertIn("[图片：一碗烤冷面", "\n".join(lab._blocks(rows, False)))
        # a photo without a caption stays [图片]; two photos, one caption: the first gets it
        m = Msg("9", E, "p", "[图片][图片]", name="小王", captions=["猫"])
        self.assertIn("[图片：猫][图片]", build_prompt(m, [], E))


if __name__ == "__main__":
    unittest.main()
