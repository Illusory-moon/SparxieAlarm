import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_plugin(data_dir):
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    root = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    message = types.ModuleType("astrbot.core.agent.message")

    class Star:
        def __init__(self, context):
            self.context = context

    class Logger:
        def info(self, *args):
            pass

        def warning(self, *args):
            pass

    class TextPart:
        def __init__(self, text):
            self.text = text
            self.temporary = False

        def mark_as_temp(self):
            self.temporary = True
            return self

    def hook(*args, **kwargs):
        return lambda fn: fn

    api.logger = Logger()
    api.star = star_api
    star_api.Star = Star
    star_api.Context = object
    star_api.StarTools = types.SimpleNamespace(get_data_dir=lambda name: data_dir)
    event_api.AstrMessageEvent = object
    event_api.filter = types.SimpleNamespace(
        on_llm_request=hook, on_agent_done=hook)
    message.TextPart = TextPart
    modules = {
        "astrbot": root,
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.message": message,
    }
    spec = importlib.util.spec_from_file_location(
        "alarm_under_test", Path(__file__).with_name("main.py"))
    plugin = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
    return plugin


class Event:
    def __init__(self, bot):
        self.bot = bot

    def get_self_id(self):
        return self.bot


def tool_msg(content, call_id="c1", name="say_lines"):
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


class AlarmTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_plugin(self.temp.name)

    def plugin(self, **conf):
        base = {"enabled": True, "enabled_self_ids": "fire"}
        base.update(conf)
        return self.module.Main(None, base)

    def run_ctx(self, messages):
        return types.SimpleNamespace(messages=messages)

    def test_off_by_default_and_scope(self):
        p = self.module.Main(None, {})
        self.assertFalse(p.scope(Event("fire")))
        p = self.plugin()
        self.assertTrue(p.scope(Event("fire")))
        self.assertFalse(p.scope(Event("water")))

    def test_judgement_only_scans_tool_role(self):
        """她正文里说「没发出去」不算故障；工具返回才算（判据的核心约束）。"""
        p = self.plugin()
        msgs = [{"role": "assistant", "content": "报告：刚才没发出去，几条全咽回去了"}]
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(msgs)))
        self.assertFalse(os.path.exists(p.path))
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
            tool_msg("没发出去（这个功能现在有毛病）—— 把想说的话直接写在正文里就行。"))))
        data = self.module.load_state(p.path)
        self.assertEqual(len(data["pending"]), 1)
        self.assertEqual(data["pending"][0]["tool"], "say_lines")
        self.assertIn("没发出去", data["pending"][0]["snippet"])

    def test_generic_marks_off_by_default(self):
        p = self.plugin()
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
            tool_msg("error: failed to send message", name="other_tool"))))
        self.assertFalse(os.path.exists(p.path))
        q = self.plugin(generic_marks=True)
        asyncio.run(q.scan_run(Event("fire"), self.run_ctx(
            tool_msg("error: failed to send message", name="other_tool"))))
        self.assertEqual(len(self.module.load_state(q.path)["pending"]), 1)

    def test_dedupe_same_bug_twice(self):
        p = self.plugin()
        for _ in range(2):
            asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
                tool_msg("只发出去 1 条（剩下的没发成）", call_id="c%d" % _))))
        self.assertEqual(len(self.module.load_state(p.path)["pending"]), 1)

    def test_hint_template_is_neutral(self):
        """话头里不许出现人名 / 号码 —— 找谁是她自己的事。"""
        p = self.plugin()
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
            tool_msg("没发出去（这个功能现在有毛病）"))))
        item = self.module.load_state(p.path)["pending"][0]
        hint = self.module.render_hint(item)
        self.assertIn("运营", hint)
        self.assertIn("连发短句的功能", hint)
        self.assertIsNone(re.search(r"[0-9]{4,}", hint))
        self.assertEqual(hint, self.module.render_hint(item))

    def test_hint_delivered_once_per_cooldown_then_reported(self):
        p = self.plugin()
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
            tool_msg("没发出去（这个功能现在有毛病）"))))
        req = types.SimpleNamespace(system_prompt="", prompt="hi", extra_user_content_parts=[])
        asyncio.run(p.maybe_hint(Event("fire"), req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("运营", req.extra_user_content_parts[0].text)
        self.assertTrue(req.extra_user_content_parts[0].temporary)
        req2 = types.SimpleNamespace(system_prompt="", prompt="hi", extra_user_content_parts=[])
        asyncio.run(p.maybe_hint(Event("fire"), req2))
        self.assertEqual(len(req2.extra_user_content_parts), 0)
        at = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "a1", "type": "function",
             "function": {"name": "at_user", "arguments": "{\"who\": \"某人\"}"}}]},
            {"role": "tool", "tool_call_id": "a1", "content": "点名排上了：…"}]
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(at)))
        self.assertIsNotNone(self.module.load_state(p.path)["pending"][0]["reported_at"])
        req3 = types.SimpleNamespace(system_prompt="", prompt="hi", extra_user_content_parts=[])
        asyncio.run(p.maybe_hint(Event("fire"), req3))
        self.assertEqual(len(req3.extra_user_content_parts), 0)

    def test_at_user_without_hint_does_not_close(self):
        p = self.plugin()
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(
            tool_msg("没发出去（这个功能现在有毛病）"))))
        at = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "a1", "type": "function",
             "function": {"name": "at_user", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a1", "content": "点名排上了：…"}]
        asyncio.run(p.scan_run(Event("fire"), self.run_ctx(at)))
        self.assertIsNone(self.module.load_state(p.path)["pending"][0]["reported_at"])


if __name__ == "__main__":
    unittest.main()
