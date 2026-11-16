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

# ---- 麦织（MaiWeave）适配（2026-11-15）----
# 两个日程插件的公开 API 不同名、字段也不同：
#   麦生活  get_life_state + get_current_scene（sleep_phase / current.kind|summary）
#   麦织    get_life_status 单个只读 API（sleeping|quiet / current.kind|title）
# 这里只做单向读取，不改麦织任何东西；字段映射不全就留中性值，绝不猜。
LIFE_PLUGIN_ID = "maibot-community.mai-life"
WEAVE_PLUGIN_ID = "ilworkcn.maiweave"

# 麦织的 plan kind（core/models.py 的 Kind）到活动强度。麦生活没有
# cook / social / diary 三种，这里补上，其余与 ACTIVITY_LEVEL 同量级。
WEAVE_ACTIVITY = {
    "sleep": 0.0,
    "rest": 0.05,
    "diary": 0.05,
    "meal": 0.10,
    "cook": 0.15,
    "leisure": 0.15,
    "social": 0.20,
    "study": 0.25,
    "work": 0.35,
    "travel": 0.45,
}

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
        # 室外天气（2026-11-14）
        self._weather_cache: dict = {}
        self._last_indoor = True
        self._location_hint = ""

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def _provider(self) -> str:
        """联动对象：life（麦生活）/ weave（麦织）。"""

        p = str(getattr(self._cfg, "provider", "life") or "life").strip().lower()
        return "weave" if (p.startswith("weave") or p in ("麦织", "maiweave")) else "life"

    def _plugin_id(self) -> str:
        if self._provider() == "weave":
            explicit = str(getattr(self._cfg, "host_plugin_id", "") or "")
            # 填的还是麦生活的默认 ID 就等于没填：切到麦织后不该再往麦生活打。
            if not explicit or explicit == LIFE_PLUGIN_ID:
                return WEAVE_PLUGIN_ID
            return explicit
        return str(getattr(self._cfg, "host_plugin_id", "") or LIFE_PLUGIN_ID)

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

        if self._provider() == "weave":
            return await self._pull_weave(now)

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
    async def _pull_weave(self, now: float) -> dict[str, Any]:
        """拉一次麦织状态，规范化成与麦生活同一套字段。

        映射原则：拿得到就映射，拿不到就留中性（0 / 空串），
        绝不替麦织宣布"她醒着"或"她在活动"——那会让身体偏离事实。
        """

        status = await self._call("get_life_status")
        if not isinstance(status, dict) or not status:
            return {}
        if not status.get("ready") or not status.get("enabled"):
            # 麦织没就绪或生活引擎关闭：返回空，调用方保持上次注入值。
            return {}

        current = status.get("current") or {}
        kind = str(current.get("kind") or "")
        summary = str(current.get("title") or "")

        # 麦织没有 sleep_phase，只有三个布尔/时间字段。
        # quiet（安静睡眠）比 sleeping（睡着）更强：quiet 时按深睡算。
        quiet = status.get("quiet")
        sleeping = status.get("effective_sleeping")
        if sleeping is None:
            sleeping = status.get("sleeping")
        if quiet:
            phase, sleep = "deep_sleep", 1.0
        elif sleeping:
            phase, sleep = "light_sleep", 0.65
        else:
            phase, sleep = "awake", 0.0

        activity = WEAVE_ACTIVITY.get(kind, 0.0) if kind else 0.0

        norm = {
            "sleep": sleep,
            "activity": activity,
            "phase": phase,
            "kind": kind,
            "summary": summary,
            "location": str(status.get("location") or current.get("location") or ""),
            # 麦织的 energy 是 0~100、mood 是 -5~5，与麦生活的量纲不同。
            # 量纲不明的字段一律不透传，避免把身体的心率/心境带偏。
            "energy": 0.0,
            "hunger": 0.0,
            "mood": 0.0,
            "activity_text": summary,
            "ts": now,
            "provider": "weave",
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

        if self._provider() == "weave":
            return await self._meal_trigger_weave(hour)

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
    async def current_scene(self) -> dict[str, Any]:
        """取"当前场景"，供运动识别与天气地点使用。

        两个日程插件的 API 不同名：
          - 麦生活：get_current_scene，返回 {"current": {kind, summary, location}}
          - 麦织：  没有 get_current_scene，只有 get_life_status，
                    当前安排放在 status["current"]，文本字段叫 title 不叫 summary

        旧实现两处调用点都硬编码 get_current_scene，切到麦织后必然抛异常
        并被静默吞掉 —— 结果是"日程里写了跑步，身体不会去跑"，且无任何日志。
        这里按 provider 分派，麦生活分支保持原样，只补麦织分支。

        返回统一形状 {"current": {"kind", "summary", "location"}}，
        拿不到时返回空 dict，调用方按"没有场景信息"降级。
        """

        if self._provider() != "weave":
            # 麦生活：原样调用，行为与改动前完全一致
            return await self._call("get_current_scene") or {}

        status = await self._call("get_life_status")
        if not isinstance(status, dict) or not status:
            return {}
        if not status.get("ready") or not status.get("enabled"):
            return {}
        current = status.get("current") or {}
        if not current:
            return {}
        # 麦织的文本字段是 title；location 可能在 current 里，也可能在顶层
        return {
            "current": {
                "kind": str(current.get("kind") or ""),
                "summary": str(current.get("title") or ""),
                "location": str(current.get("location") or status.get("location") or ""),
            }
        }

    # ------------------------------------------------------------------
    async def _meal_trigger_weave(self, hour: float) -> tuple[str, str, str, list]:
        """麦织版的三餐触发：结构对齐 meal_trigger，去重键用 plan id + start。

        麦织的 plan 自带 ISO 起止时间与唯一 id，比麦生活按"日 + 分钟"拼键更可靠。
        """

        status = await self._call("get_life_status")
        if not isinstance(status, dict) or not status.get("ready"):
            return "", "", "", []
        current = status.get("current") or {}
        if str(current.get("kind") or "") != "meal":
            return "", "", "", []

        summary = str(current.get("title") or "")
        key = f"weave|{current.get('id') or ''}|{current.get('start') or ''}"
        if key == self._last_meal_key:
            return "", "", "", []
        self._last_meal_key = key

        slot = self.meal_slot_for("meal", hour, summary)
        items: list = []
        try:
            items = food.parse_menu(summary, limit=6)
        except Exception:  # noqa: BLE001 - 解析失败就交给引擎按餐段配
            items = []
        return slot, key, summary, items

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 室外天气（2026-11-14 新增）
    # ------------------------------------------------------------------
    # 麦生活不暴露湿度：它的请求参数只有 temperature_2m,weather_code
    # （environment.py 的 current 字段），所以缓存里也没有湿度。而体感温度
    # 离开湿度算不准 —— 35℃ 干燥和 35℃ 潮湿是两种体验。
    # 这里直连 Open-Meteo 拿齐温湿度；经纬度暂时用北京（麦生活没公开坐标），
    # 城市可以在配置里指定。
    # 任何失败都降级为 None，绝不影响身体推进。
    # 日程摘要里的运动关键词。麦生活的 kind 没有 exercise，
    # 只能从 summary 自由文本认（"去操场跑步"/"健身房练腿"）。
    _EXERCISE_WORDS = (
        "跑步", "慢跑", "晨跑", "夜跑", "跑了", "长跑",
        "健身", "练腿", "撸铁", "举铁", "力量训练",
        "游泳", "骑车", "单车", "骑行",
        "打球", "篮球", "羽毛球", "乒乓", "排球", "足球", "网球",
        "瑜伽", "跳操", "跳绳", "拉伸", "爬山", "徒步", "散步",
        "锻炼", "运动", "热身", "有氧", "HIIT",
    )
    # 强度分档：这些是高强度，其余按中等算
    _EXERCISE_HIGH = (
        "跑步", "慢跑", "晨跑", "夜跑", "长跑", "跳绳", "HIIT",
        "游泳", "打球", "篮球", "羽毛球", "足球", "网球", "爬山",
    )

    _INDOOR_WORDS = (
        "家", "卧室", "客厅", "房间", "宿舍", "寝室", "图书馆", "教室",
        "办公室", "公司", "食堂", "餐厅", "商场", "超市", "咖啡", "书店",
        "实验室", "机房", "室内", "屋里", "床上", "沙发", "地铁", "公交",
        "医院", "厨房", "书房", "走廊", "浴室",
    )
    _OUTDOOR_WORDS = (
        "公园", "操场", "跑道", "路上", "街上", "街道", "户外", "室外",
        "骑车", "散步", "爬山", "海边", "湖边", "广场", "球场", "露天",
        "天台", "阳台", "门口", "小区", "校园", "步行", "跑步",
    )

    def guess_indoor(self, location: str = "") -> bool:
        """按当前场景的地点猜室内外。猜不出就沿用上次（默认室内）。"""

        loc = str(location or self._location_hint or "")
        if not loc:
            return bool(self._last_indoor)
        for w in self._INDOOR_WORDS:
            if w in loc:
                self._last_indoor = True
                return True
        for w in self._OUTDOOR_WORDS:
            if w in loc:
                self._last_indoor = False
                return False
        return bool(self._last_indoor)

    def detect_exercise(self, summary: str) -> str:
        """从日程摘要认出运动种类。返回 '' 表示不是运动。

        麦生活的 kind 只有 8 种，没有 exercise，所以只能看 summary 文本。
        返回 "high" / "mid" 表示强度档，供引擎决定心率上抬幅度。
        """

        text = str(summary or "")
        if not text:
            return ""
        hit_any = False
        is_high = False
        for w in self._EXERCISE_WORDS:
            if w in text:
                hit_any = True
                if w in self._EXERCISE_HIGH:
                    is_high = True
        if not hit_any:
            return ""
        return "high" if is_high else "mid"

    async def fetch_outdoor(self, timezone: str = "Asia/Shanghai",
                            location: str = "") -> dict | None:
        """拿一次室外天气，附带室内外判断。失败返回 None。

        城市名写在配置里，插件自己解析成坐标 —— 不去同步麦生活，
        因为它的天气接口不公开、也不含湿度。
        """

        if not bool(getattr(self._cfg, "weather_enabled", True)):
            return None
        try:
            from .body import weather as wx
            city = str(getattr(self._cfg, "weather_city", "") or "").strip() or "北京"
            loc = wx.geocode_city(city)
            if not loc:
                # 城市解析不出来时退回北京，而不是干脆没有天气
                loc = wx.geocode_city("北京")
            if not loc:
                return None
            lat, lon = loc
            data = wx.fetch_weather(lat, lon, timezone=timezone,
                                    cache=self._weather_cache,
                                    refresh_seconds=float(getattr(
                                        self._cfg, "weather_refresh_seconds", 1800.0)))
            if not data:
                return None
            self._weather_cache = data
            out = dict(data)
            indoor = self.guess_indoor(location)
            out["indoor"] = indoor
            # 风寒只在室外生效：室内风速被墙体挡住，"风"这一项没有意义
            wind = float(data.get("wind_ms", 0.0)) if not indoor else 0.0
            out["wind_ms"] = wind
            out["feels_c"] = wx.feels_like(data["temp_c"], data["humidity"],
                                           wind if data["temp_c"] <= 10.0 else 0.0)
            out["load"] = wx.outdoor_load(data["temp_c"], data["humidity"], wind)
            return out
        except Exception:
            return None

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
            "provider": self._provider(),
            "plugin_id": self._plugin_id(),
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
