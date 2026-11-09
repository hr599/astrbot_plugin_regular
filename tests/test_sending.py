"""Task L: sending behind an off switch, /reg send, /reg say. No AstrBot needed."""

import asyncio
import json
import time
import unittest

from core.admin import AdminCommands
from core.gate import GateConfig
from core.outbox import SEND_CAP_PER_HOUR, Outbox, SendAdmin, deliver
from core.pipeline import Pipeline
from services.store import Store
from tests.test_m0 import BOT, FRIEND, GROUP, ME, SCOPE, dm, ev, group_msg

OTHER_BOT = "3612669335"


class SendTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.cfg = GateConfig(group_ids={GROUP}, bot_uids={OTHER_BOT})
        self.outbox = Outbox("off", rules={})
        self.saved_mode = None
        sender = SendAdmin(self.outbox, watched=lambda: self.cfg.group_ids, paused=lambda: self.cfg.paused_groups,
                           save_mode=lambda m: setattr(self, "saved_mode", m),
                           scope_of=lambda platform, gid: f"{platform}:GroupMessage:{gid}")
        self.p = Pipeline(self.s, self.cfg, admin_handler=AdminCommands(lambda: {}, lambda: "", lambda o: None,
                                                                        sender=sender))
        self.t = int(time.time()) - 600
        self.sent = []
        self.next_id = 5000

    def reg(self, text, admin=True, uid=ME):
        return self.p.handle(ev(dm(text, uid=uid), admin=admin)).admin_reply

    async def _send(self, gid, text):
        self.sent.append((gid, text))
        self.next_id += 1
        return self.next_id

    def run_queue(self, *, on_sleep=None, fail=False):
        results = []

        async def sleep(_):
            if on_sleep:
                on_sleep()

        async def send(gid, text):
            if fail:
                raise ConnectionError("llbot down")
            return await self._send(gid, text)

        async def go():
            while self.outbox.queue:
                job = self.outbox.queue.popleft()
                results.append(await deliver(
                    job, self.outbox, send=send,
                    record=lambda j, part, mid, ts, dry: self.p.record_own(j, part, mid, ts, dry, "aiocqhttp"),
                    paused=lambda: self.cfg.paused_groups, clock=lambda: self.t + 100, sleep=sleep))
        asyncio.run(go())
        return results

    # ---------------------------------------------------------------- switch
    def test_off_by_default_and_admin_only(self):
        self.assertIn("没发", self.reg(f"/reg say {GROUP} 大家好"))
        self.assertEqual(list(self.outbox.queue), [])
        self.assertIsNone(self.reg("/reg send on", admin=False, uid=FRIEND))  # gate drops non-admin DMs
        self.p.handle(ev(group_msg("/reg send on", uid=FRIEND)))  # in the group: a command, never run
        self.assertEqual(self.outbox.mode, "off")
        self.assertIn("发送模式：off", self.reg("/reg send"))

    def test_dry_run_records_but_sends_nothing(self):
        self.assertIn("试运行", self.reg("/reg send dry"))
        self.assertEqual(self.saved_mode, "dry")
        self.assertIn("排好了", self.reg(f"/reg say {GROUP} 大家好～ 我是阿灯"))
        self.assertEqual(self.run_queue(), [["dry", "dry"]])  # split at ～
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        tr = self.s.db.execute("SELECT decision, reasons FROM traces WHERE ended_at='send'").fetchall()
        self.assertEqual([r[0] for r in tr], ["dry_run", "dry_run"])
        self.assertNotIn("阿灯", tr[0][1])  # length only, not the words

    def test_send_traces_stay_readable_in_debug_bundles(self):
        # the live test's bundle masked origin and part_len: they're ours, not group content
        from services.debug import redact
        self.reg("/reg send dry")
        self.reg(f"/reg say {GROUP} 大家好")
        self.run_queue()
        r = json.loads(self.s.db.execute("SELECT reasons FROM traces WHERE ended_at='send'").fetchone()[0])
        self.assertEqual(redact(r), {"origin": "admin_say", "dry": True, "part_len": 3, "reply_to": None})

    def test_on_sends_stores_and_feeds_the_limits(self):
        self.reg("/reg send on")
        # another bot @s 阿灯; it would answer
        out = self.p.handle(ev(group_msg([{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 在吗"}}],
                                         uid=OTHER_BOT, mid=1, ts=self.t)))
        self.assertEqual(out.reasons["shadow"]["would"], "reply")
        self.reg(f"/reg say {GROUP} 在的")
        self.assertEqual(self.run_queue(), [["sent"]])
        self.assertEqual(self.sent, [(GROUP, "在的")])
        row = self.s.db.execute("SELECT msg_id, is_bot, person_id, text FROM messages WHERE is_bot=1").fetchone()
        self.assertEqual((row["msg_id"], row["person_id"], row["text"]), (f"{SCOPE}:5001", None, "在的"))
        # now live: the bot-loop rule holds the next bot @ (no person spoke since)
        out = self.p.handle(ev(group_msg([{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 再聊"}}],
                                         uid=OTHER_BOT, mid=2, ts=self.t + 200)))
        self.assertEqual(out.reasons["shadow"]["rule"], "bot_loop")
        # and a quote of what 阿灯 said is recognised from the store
        q = self.p.handle(ev(group_msg([{"type": "reply", "data": {"id": "5001"}}, {"type": "text", "data": {"text": "哈哈"}}],
                                       uid=FRIEND, mid=3, ts=self.t + 210)))
        self.assertTrue(q.reasons["reply_to_bot"])

    def test_parts_are_split_and_sent_in_order(self):
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 好耶！！今天吃什么？去吃火锅吧")
        self.run_queue()
        self.assertEqual([t for _, t in self.sent], ["好耶！！", "今天吃什么？", "去吃火锅吧"])

    # ---------------------------------------------------------------- safety
    def test_command_like_text_never_goes_out(self):
        self.reg("/reg send on")
        for text in ("/help", "＃签到", "好的！/签到", "  !roll"):
            with self.subTest(text=text):
                self.assertIn("没发", self.reg(f"/reg say {GROUP} {text}"))
        self.assertEqual(list(self.outbox.queue), [])

    def test_unwatched_or_paused_group(self):
        self.reg("/reg send on")
        self.assertIn("不在观察列表", self.reg("/reg say 999999 hi"))
        self.cfg.paused_groups = {GROUP}
        self.assertIn("暂停", self.reg(f"/reg say {GROUP} hi"))

    def test_switch_off_or_pause_stops_between_parts(self):
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 第一句！第二句！第三句")
        res = self.run_queue(on_sleep=lambda: setattr(self.outbox, "mode", "off") if self.sent else None)
        self.assertEqual(res, [["sent", "stopped: sending was switched off"]])
        self.outbox.set_mode("on")
        self.sent.clear()
        self.reg(f"/reg say {GROUP} 第一句！第二句")
        res = self.run_queue(on_sleep=lambda: self.cfg.paused_groups.add(GROUP) if self.sent else None)
        self.assertEqual(res, [["sent", "stopped: group paused"]])

    # ---------------------------------------------------------------- PR #16 review
    def test_dry_job_never_becomes_a_real_send(self):
        self.reg("/reg send dry")
        self.assertIn("试运行", self.reg(f"/reg say {GROUP} 测试文字"))
        job = self.outbox.queue[0]
        self.outbox.queue.clear()  # already started, like main._start_queued does
        self.reg("/reg send on")  # switched before its delay ran out
        self.outbox.queue.append(job)
        self.assertEqual(self.run_queue(), [["stopped: sending mode changed since it was queued"]])
        self.assertEqual(self.sent, [])

    def test_off_then_on_doesnt_revive_a_started_job(self):
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 旧的那句")
        job = self.outbox.queue.popleft()  # in flight
        self.reg("/reg send off")
        self.reg("/reg send on")
        self.outbox.queue.append(job)
        self.assertEqual(self.run_queue(), [["stopped: sending mode changed since it was queued"]])
        self.reg(f"/reg say {GROUP} 新的这句")  # a new say after re-enabling still goes out
        self.assertEqual(self.run_queue(), [["sent"]])
        self.assertEqual(self.sent, [(GROUP, "新的这句")])

    def test_cap_counts_jobs_already_queued(self):
        # issue #18: two jobs prepared at once can't both slip under the cap
        scope = f"aiocqhttp:GroupMessage:{GROUP}"
        self.reg("/reg send on")
        self.outbox.seed(scope, [int(time.time()) - 60] * (SEND_CAP_PER_HOUR - 1))
        self.assertIn("排好了", self.reg(f"/reg say {GROUP} 第一个"))
        self.assertIn("group cap", self.reg(f"/reg say {GROUP} 第二个"))
        self.run_queue()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.outbox._reserved.get(scope, 0), 0)

    def test_stopped_job_frees_its_reservation(self):
        scope = f"aiocqhttp:GroupMessage:{GROUP}"
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 一！二！三")
        self.assertEqual(self.outbox._reserved[scope], 3)
        self.run_queue(fail=True)
        self.assertEqual(self.outbox._reserved[scope], 0)

    def test_send_failure_stops_and_records_nothing(self):
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 在吗")
        self.assertEqual(self.run_queue(fail=True), [["stopped: send failed (ConnectionError)"]])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_hourly_group_cap(self):
        self.reg("/reg send on")
        self.outbox.seed(f"aiocqhttp:GroupMessage:{GROUP}", [int(time.time()) - 60] * SEND_CAP_PER_HOUR)
        self.assertIn("group cap", self.reg(f"/reg say {GROUP} 还能说吗"))
        self.outbox.seed(f"aiocqhttp:GroupMessage:{GROUP}", [int(time.time()) - 3700] * SEND_CAP_PER_HOUR)
        self.assertIn("排好了", self.reg(f"/reg say {GROUP} 一小时过去了"))

    def test_off_clears_the_queue(self):
        self.reg("/reg send on")
        self.reg(f"/reg say {GROUP} 排队中")
        self.reg("/reg send off")
        self.assertEqual(list(self.outbox.queue), [])


class AdapterTests(unittest.TestCase):
    def test_send_group_text_uses_call_action_and_returns_the_id(self):
        import importlib
        import sys
        import types
        # adapter/outbound imports astrbot only for the DM path; stub it for this test
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        event = types.ModuleType("astrbot.api.event")
        event.MessageChain = object
        sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})
        try:
            outbound = importlib.import_module("adapter.outbound")
            calls = []

            class Client:
                async def call_action(self, action, **kw):
                    calls.append((action, kw))
                    return {"message_id": 42}

            class Inst:
                def get_client(self):
                    return Client()

            class Ctx:
                def get_platform_inst(self, pid):
                    return Inst() if pid == "llbot" else None

            mid = asyncio.run(outbound.send_group_text(Ctx(), "llbot", "555", "在的"))
            self.assertEqual(mid, 42)
            self.assertEqual(calls, [("send_group_msg", {"group_id": 555,
                                                          "message": [{"type": "text", "data": {"text": "在的"}}]})])
            with self.assertRaises(RuntimeError):
                asyncio.run(outbound.send_group_text(Ctx(), "nope", "555", "x"))
        finally:
            for k in ("astrbot", "astrbot.api", "astrbot.api.event", "adapter.outbound"):
                sys.modules.pop(k, None)


if __name__ == "__main__":
    unittest.main()


class CapTests(unittest.TestCase):
    """Nov 9, first live afternoon: everyone wanted to chat; the owner asked for a
    way to raise the hourly cap and to reset it."""

    def setUp(self):
        self.outbox = Outbox("on", cap=3)
        self.saved = {}
        self.resets = []
        self.admin = SendAdmin(self.outbox, watched=lambda: {GROUP}, paused=lambda: set(),
                               save_mode=lambda m: None, scope_of=lambda platform, gid: f"{platform}:GroupMessage:{gid}",
                               save_cap=lambda n: self.saved.update(cap=n), on_reset=self.resets.append)

    def fill(self):
        for i in range(3):
            self.outbox.note_sent(SCOPE, 1000 + i)

    def test_the_cap_is_configurable_and_bounded(self):
        self.fill()
        self.assertIn("group cap: 3", self.outbox.prepare("嗯", scope_id=SCOPE, group_id=GROUP, origin="reply",
                                                          now=1010).refused)
        self.assertIn("最多 5 条", self.admin.handle("send", ["cap", "5"], "aiocqhttp", ""))
        self.assertEqual((self.outbox.cap, self.saved["cap"]), (5, 5))
        self.assertIsNotNone(self.outbox.prepare("嗯", scope_id=SCOPE, group_id=GROUP, origin="reply", now=1010).job)
        self.assertIn("最多 60", self.admin.handle("send", ["cap", "500"], "aiocqhttp", ""))
        self.assertEqual(self.outbox.cap, 60)
        self.assertEqual(Outbox("on", cap="abc").cap, SEND_CAP_PER_HOUR)
        self.assertIn("每个群每小时最多 60 条", self.admin.handle("send", ["cap"], "aiocqhttp", ""))
        self.assertIn("用法", self.admin.handle("send", ["cap", "x"], "aiocqhttp", ""))

    def test_reset_clears_this_hours_count(self):
        self.fill()
        self.assertIsNone(self.outbox.prepare("嗯", scope_id=SCOPE, group_id=GROUP, origin="reply", now=1010).job)
        reply = self.admin.handle("send", ["reset"], "aiocqhttp", "")
        self.assertIn("已清零", reply)
        self.assertEqual(self.resets, [[GROUP]])
        self.assertIsNotNone(self.outbox.prepare("嗯", scope_id=SCOPE, group_id=GROUP, origin="reply", now=1010).job)
        self.assertIn("不在观察列表", self.admin.handle("send", ["reset", "999"], "aiocqhttp", ""))

    def test_mode_commands_still_work(self):
        self.assertIn("off", self.admin.handle("send", [], "aiocqhttp", ""))
        self.assertIn("已关闭", self.admin.handle("send", ["off"], "aiocqhttp", ""))
