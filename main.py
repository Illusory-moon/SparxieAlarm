# -*- coding: utf-8 -*-
"""火警 —— 她发现某个功能出毛病时，用自己的语气去找「运营」报修。

只做三件事：**登记 / 递话头 / 销账**。
绝不宣布「修好了」—— 那是运营在群里亲口说的事（2026-10-06 主人拍板）。

与 mindscape 零硬耦合：
  · 故障来自框架的 agent run 工具结果（只看 role == "tool"）
  · 话头走 on_llm_request + temp TextPart（不落历史）
  · 报修动作由她自己完成（她自己的工具，或直接说）
"""
import json
import os
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None


VERSION = 1
HINT_LIMIT = 2               # 同一故障最多递 2 次话头
HINT_COOLDOWN = 900         # 两次递话头至少隔 15 分钟（一轮里 LLM 请求会来好几次）
NUDGE_AFTER = 24 * 3600     # 首次递出后 24h 还没报 → 允许再催一次
_MAX_SNIPPET = 60

# 判据：**先紧后松**（2026-10-06 主人拍板）。
# 第一版只认我们自己工具的约定失败话术 —— 宁可漏报，不可误报。
FAIL_MARKS = (
    "没发出去",
    "只发出去",
    "发好了：0 条",
    "拿不到 MessageChain",
)
# 框架通用失败词：默认**不启用**（配置 generic_marks = true 才认）
GENERIC_MARKS = ("error:", "failed to send", "Traceback (most recent call last)")

# 给她看的功能名（中性词；认不出就用工具名）
TOOL_LABELS = {
    "say_lines": "连发短句的功能",
    "send_message_to_user": "发消息的功能",
}


def target_ids(raw):
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set(re.split(r"[\s,，;；]+", str(raw or "").strip())) - {""}


def tool_label(tool):
    return TOOL_LABELS.get(str(tool or ""), str(tool or "那个功能"))


def is_failure(text, generic=False):
    """一段**工具返回**是不是故障。只对 tool 角色用，绝不对助手正文用。"""
    body = str(text or "")
    if not body.strip():
        return None
    for mark in FAIL_MARKS + (GENERIC_MARKS if generic else ()):
        if mark in body:
            return mark
    return None


def bug_key(tool, mark):
    return "%s|%s" % (str(tool or "?"), str(mark or "?"))


def render_hint(item, nudge=False):
    """给她的那句中性话头 —— **不含任何人名 / 号 / 语气**（单测直接断言）。"""
    when = time.strftime("%m-%d %H:%M", time.localtime(float(item.get("first_seen") or 0)))
    label = tool_label(item.get("tool"))
    lead = "又出毛病了" if nudge else "出过一次毛病"
    return ("（插一句：%s 在 %s %s，现在还没好。"
            "你要是想，可以去找「运营」说一声 —— 用你自己的说法就行。）"
            % (label, when, lead))


def load_state(path):
    if not os.path.exists(path):
        return {"version": VERSION, "pending": []}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("pending"), list):
        raise ValueError("invalid alarm state file")
    return data


def save_state(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            t = _get(part, "text", None)
            if isinstance(t, str):
                out.append(t)
        return chr(10).join(out)
    return str(content or "")


def _tool_calls_of(msg):
    """取这条 assistant 消息里的工具调用 [(id, name, arguments), ...]。"""
    out = []
    for call in (_get(msg, "tool_calls", None) or []):
        fn = _get(call, "function", None)
        name = _get(fn, "name", None) if fn is not None else _get(call, "name", None)
        cid = _get(call, "id", None) or _get(call, "tool_call_id", None)
        args = _get(fn, "arguments", None) if fn is not None else None
        if name:
            out.append((str(cid or ""), str(name), args))
    return out


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        try:
            base = str(StarTools.get_data_dir("SparxieAlarm"))
        except Exception:
            base = os.path.join(os.getcwd(), "data", "SparxieAlarm")
        self.path = os.path.join(base, "alarm_state.json")
        self.enabled = bool(self.config.get("enabled", False))
        self.targets = target_ids(self.config.get("enabled_self_ids", ""))
        self.generic = bool(self.config.get("generic_marks", False))
        logger.info("[alarm] loaded | enabled=%s targets=%d | 通用失败词=%s",
                    self.enabled, len(self.targets), self.generic)

    # ---------- 基础 ----------
    def scope(self, event):
        if not self.enabled:
            return False
        try:
            bot = str(event.get_self_id() or "").strip()
        except Exception:
            return False
        return bool(bot) and bot in self.targets

    def _load(self):
        try:
            return load_state(self.path)
        except Exception as exc:
            logger.warning("[alarm] 状态文件读不动: %s", type(exc).__name__)
            return {"version": VERSION, "pending": []}

    def _save(self, data=None):
        try:
            save_state(self.path, data if data is not None else self._data)
        except Exception as exc:
            logger.warning("[alarm] 状态写不动: %s", type(exc).__name__)

    def _register(self, tool, mark, snippet):
        data = self._load()
        key = bug_key(tool, mark)
        now = time.time()
        for item in data["pending"]:
            if bug_key(item.get("tool"), item.get("mark")) == key:
                item["last_seen"] = now
                self._save(data)
                return False
        data["pending"].append({
            "tool": str(tool or "?"),
            "mark": str(mark or "?"),
            "snippet": str(snippet or "")[:_MAX_SNIPPET],
            "first_seen": now,
            "last_seen": now,
            "hinted": 0,
            "hinted_at": 0,
            "reported_at": None,
        })
        self._save(data)
        logger.info("[alarm] 登记 tool=%s snippet=%s", tool, str(snippet or "")[:_MAX_SNIPPET])
        return True

    def _hintable(self):
        data = self._load()
        now = time.time()
        for item in data["pending"]:
            if item.get("reported_at"):
                continue
            if int(item.get("hinted") or 0) >= HINT_LIMIT:
                continue
            if now - float(item.get("hinted_at") or 0) < HINT_COOLDOWN:
                continue
            return data, item
        return data, None

    # ---------- 钩子 ----------
    @filter.on_llm_request()
    async def maybe_hint(self, event: AstrMessageEvent, request):
        """她每次开口前，如果手上有没报过的故障，就递个话头（不落历史）。"""
        if not self.scope(event):
            return
        try:
            data, item = self._hintable()
            if item is None:
                return
            nudge = int(item.get("hinted") or 0) > 0
            text = render_hint(item, nudge=nudge)
            parts = getattr(request, "extra_user_content_parts", None)
            if TextPart is not None and parts is not None:
                part = TextPart(text=text)
                parts.append(part.mark_as_temp() if hasattr(part, "mark_as_temp") else part)
            else:
                request.prompt = (request.prompt or "") + chr(10) * 2 + text
            item["hinted"] = int(item.get("hinted") or 0) + 1
            item["hinted_at"] = time.time()
            self._save(data)
            logger.info("[alarm] 递话头 #%d tool=%s", item["hinted"], item.get("tool"))
        except Exception as exc:
            logger.warning("[alarm] 递话头失败: %s", type(exc).__name__)

    @filter.on_agent_done()
    async def scan_run(self, event: AstrMessageEvent, run_context, response=None):
        """这一轮跑完：登记工具故障；她要是 @ 了人，就把递过话头的故障销账。"""
        if not self.scope(event):
            return
        try:
            messages = list(_get(run_context, "messages", None) or [])
        except Exception:
            return
        calls = {}
        pointed = ""
        for msg in messages:
            role = str(_get(msg, "role", "") or "")
            for cid, name, args in _tool_calls_of(msg):
                calls[cid] = name
                if name == "at_user":
                    pointed = str(args or "")[:80]
            if role != "tool":
                continue
            body = _text_of(_get(msg, "content", ""))
            mark = is_failure(body, self.generic)
            if not mark:
                continue
            tool = calls.get(str(_get(msg, "tool_call_id", "") or ""), "?")
            self._register(tool, mark, body)
        if pointed:
            logger.info("[alarm] 她点名了 args=%s", pointed[:60])
            self._close_reported()

    def _close_reported(self):
        """只销「已递过话头」的账 —— 她 @ 别人的日常操作不会误销。"""
        data = self._load()
        now = time.time()
        hit = 0
        for item in data["pending"]:
            if item.get("reported_at") or int(item.get("hinted") or 0) < 1:
                continue
            item["reported_at"] = now
            hit += 1
            logger.info("[alarm] 她报修了 → 销账 tool=%s", item.get("tool"))
        if hit:
            self._save(data)
