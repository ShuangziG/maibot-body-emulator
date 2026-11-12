"""与「麦生活」（mai-life）插件的联动客户端。

设计原则：
1. 只用麦生活的公开 API（@API public=True）与宿主钩子，不 import 它的模块、
   不读它的数据库 —— 它的内部结构随时会变。
2. 所有调用都可能失败：麦生活不在跑、版本不兼容、超时，都只能降级，
   绝不能把异常抛回身体仿真。总开关关闭时一个调用都不发。
3. 跨进程 RPC 有往返开销，状态拉取用固定间隔缓存，不在每个仿真步里调。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from .body import food

# 麦生活 sleep_phase 的取值到睡眠深度的映射（life/life_state.py 的枚举）
SLEEP_DEPTH = {
    "awake": 0.0,
    "woken": 0.0,
    "falling_asleep": 0.35,
    "light_sleep": 0.65,
    "deep_sleep": 1.0,
}

# 日程 kind 到活动强度的映射。energy_load 是麦生活自己的字段（正=恢复，负=消耗），
# 这里只用 kind 做量级判断，不用 energy_load —— 它的单位随场景重写而变。
ACTIVITY_LEVEL = {
    "sleep": 0.0,
    "nap": 0.0,
    "rest": 0.05,
    "meal": 0.10,
    "leisure": 0.15,
    "study": 0.25,
    "work": 0.35,
    "travel": 0.45,
}

MEAL_KINDS = {"meal": ""}

# 日程摘要里的餐段关键词。麦生活的 summary 是自由文本（"洗漱并吃早餐"/
# "准备午饭"/"准备晚饭"），必须按关键词认出是哪一顿。
# 旧实现直接用身体钟点反推，结果凌晨 00:06 端出一份午饭套餐。
MEAL_SLOT_KEYWORDS = (
    ("breakfast", ("早餐", "早饭", "早点", "早餐", "breakfast")),
    ("lunch", ("午餐", "午饭", "中饭", "中餐", "lunch")),
    ("dinner", ("晚餐", "晚饭", "晚膳", "夜宵", "dinner", "supper")),
)


class LifeLink:
    """麦生活联动客户端：状态缓存 + 日程同步 + 降级。"""

    def __init__(self, ctx, config) -> None:
        self._ctx = ctx
        self._cfg = config
        self._last_pull = 0.0
        self._last_meal_key = ""
        self._cache: dict[str, Any] = {}
        self._fail_count = 0
        self._last_error = ""
        self._last_ok = 0.0

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def _plugin_id(self) -> str:
        return str(getattr(self._cfg, "host_plugin_id", "") or "maibot-community.mai-life")

    async def _call(self, api_name: str, **args: Any) -> Any:
        """调麦生活的公开 API；任何失败都返回 None，不抛。"""

        try:
            result = await self._ctx.api.call(
                f"{self._plugin_id()}.{api_name}", **args
            )
            self._fail_count = 0
            self._last_ok = time.time()
            return result
        except Exception as exc:  # noqa: BLE001 - 联动失败必须降级
            self._fail_count += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None

    # ------------------------------------------------------------------
    async def pull(self) -> dict[str, Any]:
        """拉一次麦生活状态，带间隔缓存。返回规范化后的字典。"""

        if not self.enabled:
            return {}

        now = time.time()
        interval = max(10.0, float(getattr(self._cfg, "interval_seconds", 60.0)))
        if self._cache and (now - self._last_pull) < interval:
            return self._cache
        self._last_pull = now

        state = await self._call("get_life_state")
        if not isinstance(state, dict) or not state:
            # 拿不到就返回空，调用方据此保持上次的注入值不变，
            # 而不是把睡眠/活动突然归零（会让心率在半夜突然跳回白天水平）。
            return {}

        phase = str(state.get("sleep_phase") or "")
        # 拿不到识别不了的阶段就报 -1（"没有信息"），而不是 0（"明确清醒"）。
        # 早前默认 0 会让引擎把"麦生活没表态"当成"她醒着"，半夜直接把人叫醒。
        sleep = SLEEP_DEPTH.get(phase, -1.0)

        scene = await self._call("get_current_scene")
        current = (scene or {}).get("current") or {}
        kind = str(current.get("kind") or "")
        # 拿不到日程时 activity 记 0（静息中性），不要用 0.15 这种"猜"的默认值：
        # 那个值会经交感项每轮给心率加 1.5 下，等于麦生活一掉线身体就开始"微活动"。
        activity = ACTIVITY_LEVEL.get(kind, 0.15) if kind else 0.0

        norm = {
            "sleep": sleep,
            "activity": activity,
            "phase": phase,
            "kind": kind,
            "summary": str(current.get("summary") or ""),
            "location": str(current.get("location") or ""),
            "energy": float(state.get("energy") or 0.0),
            "hunger": float(state.get("hunger") or 0.0),
            "mood": float(state.get("mood_valence") or 0.0),
            "activity_text": str(state.get("current_activity") or ""),
            "ts": now,
        }
        self._cache = norm
        return norm

    # ------------------------------------------------------------------
    def meal_slot_for(self, kind: str, hour: float, summary: str = "") -> str:
        """把日程的 meal 时段映射成餐段名。

        2026-11-09 修：旧实现只用 hour 反推餐段，日程里的 summary 被
        完全丢弃。于是"日程说早饭"可能端出晚饭套餐，凌晨也会端出午饭
        （截图 00:06 的「面包+鸡胸肉+汤」就是 lunch 食谱池的条目）。

        现在优先按 summary 的关键词认餐段；认不出来才退回按 hour 猜，
        并且猜的时候放宽到"早/午/晚"三档的合理区间。
        """

        text = str(summary or "").strip().lower()
        if text:
            for slot, words in MEAL_SLOT_KEYWORDS:
                for w in words:
                    if w in text:
                        return slot

        # 关键词认不出（summary 是"吃点东西""补充能量"这类）才按钟点兜底。
        if hour < 10.0:
            return "breakfast"
        if hour < 15.0:
            return "lunch"
        return "dinner"

    async def meal_trigger(self, hour: float) -> tuple[str, str, str, list]:
        """检查麦生活日程是否刚进入一个新的 meal 时段。

        返回 (餐段名, 去重键, 日程摘要, 具体食物 key 列表)。餐段名是
        breakfast/lunch/dinner 表示"该吃这一顿"；空串表示不用。去重键由调用方
        持久化 —— 只放在内存里时，重启后当天已经吃过的饭窗会被再触发一次
        （见 _restore 的注释）。摘要一并返回，是为了让日志写"日程说了什么"。
        2026-11-11 追加第 4 项：日程/场景文本里点名的具体食物（米粉、油条、
        黄焖鸡米饭……）解析成 food key，直接送到身体。旧实现只拿到
        kind=meal 就按餐段随机配餐，"点了外卖"具体吃了什么完全没落地。
        """

        if not self.enabled or not bool(getattr(self._cfg, "sync_meal", False)):
            return "", "", "", []

        scene = await self._call("get_current_scene")
        current = (scene or {}).get("current") or {}
        if str(current.get("kind") or "") != "meal":
            return "", "", "", []

        summary = str(current.get("summary") or "")

        # 去重键必须带日期：麦生活的日程按天排，每天的 08:00 早餐
        # start_minute/end_minute/summary 完全相同。旧键不含日期，
        # 第一天的早餐触发过一次之后，后面每一天的早餐都会被当成"同一顿"跳过。
        #
        # 日期优先用日程自带的 day 字段，它跟身体/服务器时区无关；
        # 取不到才退回墙钟日期（跨时区时可能差一天，只影响去重边界）。
        day = str(current.get("day") or "").strip()
        if not day:
            day = time.strftime("%Y-%m-%d")
        key = (
            f"{day}|{current.get('start_minute')}-{current.get('end_minute')}"
            f"-{current.get('summary')}"
        )
        if key == self._last_meal_key:
            return "", "", "", []
        self._last_meal_key = key
        slot = self.meal_slot_for("meal", hour, summary)

        # 从日程摘要里抽具体食物。摘要写"点了一份生榨米粉配油条外卖"，
        # 就解析出 米粉/油条；解析不出任何食物则返回空列表，由引擎按
        # 餐段菜谱池随机配一份（保持旧的兜底行为）。
        items = []
        try:
            items = food.parse_menu(summary, limit=6)
        except Exception:
            items = []
        return slot, key, summary, items

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    async def clock_offset(self, body_hour: float) -> float:
        """算出让身体时钟对齐麦生活环境时钟的相位差（小时）。

        返回 0.0 表示不需要对齐或取不到环境时间。差值取到 (-12, 12]，
        避免「差 23 小时」这种等价但剧烈跳变的情况。
        """

        if not self.enabled or not bool(getattr(self._cfg, "sync_clock", False)):
            return 0.0

        snap = await self._call("get_environment_snapshot")
        iso = str((snap or {}).get("iso_time") or "")
        if not iso:
            return 0.0
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            # 麦生活按本地时区给出 naive 时间；按本机时区补上，避免误判。
            dt = dt.astimezone()
        env_hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        delta = (env_hour - body_hour + 12.0) % 24.0 - 12.0
        # 小于 6 分钟就不动，防止每轮都微调导致 hour 抖动。
        if abs(delta) < 0.1:
            return 0.0
        return delta

    async def share(self, topic: str, motive: str, weight: float = 0.6) -> bool:
        """把一件事作为主动分享契机交给麦生活。返回是否成功。"""

        if not self.enabled or not bool(getattr(self._cfg, "share_state", False)):
            return False
        if not topic:
            return False
        result = await self._call(
            "create_proactive_opportunity",
            topic=topic[:160],
            motive=motive[:240],
            weight=weight,
        )
        return bool(isinstance(result, dict) and result.get("success"))

    def describe_for_prompt(self) -> str:
        """把身体状态压成一句可直接注入上下文的话；空串表示不注入。"""

        return self._cache.get("prompt_note", "") if self._cache else ""

    def status(self) -> dict[str, Any]:
        """联动状态，给 WebUI 用。"""

        return {
            "enabled": self.enabled,
            "sync_activity": bool(getattr(self._cfg, "sync_activity", False)),
            "sync_meal": bool(getattr(self._cfg, "sync_meal", False)),
            "inject_state": bool(getattr(self._cfg, "inject_state", False)),
            "sync_clock": bool(getattr(self._cfg, "sync_clock", False)),
            "share_state": bool(getattr(self._cfg, "share_state", False)),
            "interval_seconds": float(getattr(self._cfg, "interval_seconds", 60.0)),
            "fail_count": self._fail_count,
            "last_error": self._last_error,
            "last_ok": self._last_ok,
            "sleep": float(self._cache.get("sleep") or 0.0),
            "phase": str(self._cache.get("phase") or ""),
            "kind": str(self._cache.get("kind") or ""),
            "summary": str(self._cache.get("summary") or ""),
            "activity": float(self._cache.get("activity") or 0.0),
        }
