"""麦麦身体 —— 让麦麦有一副持续运转的身体。

设计要点：
1. 单例 BodyEngine 由后台 asyncio 任务驱动，dt=10 仿真秒一步（实测 30 秒以上数值发散）。
2. WebUI 的 query 只读内存快照，不做仿真 —— 宿主对单次 WebUI API 有 10 秒预算
   与每插件 2 并发的硬限制，仿真必须活在后台任务里。
3. 存档是一份 BodyState 的 JSON 快照 + 落盘墙钟时间，启动时按墙钟差值追帧。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import API, Command, HomeCard, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .body import BodyEngine, BodyState, EventBus, brief, build_html, describe
from .body.food import (
    _FOOD_EXTRA_ADDED,
    ALL_FOODS,
    DRINK_POOL,
    FOODS,
    MEAL_POOLS,
    SNACK_POOL,
    combo_summary,
    decompose_menu,
    is_plain_food,
    parse_menu,
)
from .body.translate import FEELING_LEVELS, drunk_level, eat_phase, phase
from .config import LifeBodyConfig
from .link import LifeLink

PLUGIN_VERSION = "1.1.0"
H2S = 3600.0
SAVE_FILE = "body_state.json"

# 生理波形缓冲区的「超前起点」（秒）。
# 前端 webui/organs.html 用 WAVE_WINDOW_SEC=8 的窗口显示波形，屏幕右缘是
# 「现在」。所以后端生成的样本必须从「现在-8s」开始，前端才能按绝对时间
# 去索引数据池 —— 这样每次重新拉取时窗口接得上，不会跳帧。
# 缓冲区总长由前端传入的 seconds（20s）决定，覆盖 [现在-8s, 现在+12s]，
# 前端在 10s 时预取下一批，正好留 2s 余量。
_WAVE_LEAD_SEC = 8.0

# 体征展示定义：(字段, 中文名, 单位, 小数位, 正常区间, 偏低词, 偏高词)
VITALS = [
    ("hr", "心率", "bpm", 0, (60.0, 85.0), "偏慢", "偏快"),
    ("sbp", "收缩压", "mmHg", 0, (100.0, 140.0), "偏低", "偏高"),
    ("dbp", "舒张压", "mmHg", 0, (60.0, 90.0), "偏低", "偏高"),
    ("sao2", "血氧", "%", 1, (95.0, 100.0), "偏低", "正常"),
    ("glucose", "血糖", "mmol/L", 1, (3.9, 6.1), "偏低", "偏高"),
    ("temp", "体温", "℃", 2, (36.3, 37.2), "偏低", "偏高"),
    ("rr", "呼吸", "次/分", 0, (12.0, 20.0), "偏慢", "偏快"),
    ("map", "平均动脉压", "mmHg", 0, (70.0, 105.0), "偏低", "偏高"),
]

FEELINGS = [
    ("energy", "精力"), ("hunger", "饥饿"), ("thirst", "口渴"),
    ("stress", "压力"), ("pain", "疼痛"), ("depression", "心境"),
]

# 内脏细则分组：(组名, 副标题, [(字段, 中文名, 单位, 小数位)])
ORGAN_GROUPS = [
    ("cardiovascular", "心血管", "泵血与压力", [
        ("hr", "心率", "bpm", 0), ("sv", "每搏输出量", "mL", 1), ("co", "心输出量", "L/min", 2),
        ("svr", "体循环阻力", "dyn·s·cm⁻⁵", 0), ("map", "平均动脉压", "mmHg", 1),
        ("sbp", "收缩压", "mmHg", 1), ("dbp", "舒张压", "mmHg", 1), ("pp", "脉压", "mmHg", 1),
        ("blood_vol", "循环血量", "mL", 0),
    ]),
    ("respiratory", "呼吸", "气体交换", [
        ("rr", "呼吸频率", "次/分", 1), ("tidal", "潮气量", "mL", 0),
        ("sao2", "血氧饱和度", "%", 2), ("paco2", "动脉二氧化碳", "mmHg", 1),
    ]),
    ("metabolic", "代谢", "糖脂与能量", [
        ("glucose", "血糖", "mmol/L", 2), ("insulin", "胰岛素", "相对单位", 2),
        ("glycogen", "肝糖原", "g", 1), ("atp", "细胞能量", "0-1", 3),
        ("lactate", "血乳酸", "mmol/L", 2), ("fat_reserve", "待氧化脂肪", "g", 1),
        ("na_intake_24h", "膳食钠吸收", "mmol/天", 1),
    ]),
    ("renal", "肾脏", "滤过与体液", [
        ("gfr", "肾小球滤过率", "mL/min", 1), ("urine_total", "累计尿量", "mL", 0),
        ("bladder", "膀胱充盈", "mL", 0), ("na", "血钠", "mmol/L", 2),
        ("k", "血钾", "mmol/L", 2), ("osmol", "血浆渗透压", "mOsm/kg", 1),
        ("ph", "血液 pH", "", 3), ("body_water", "总体水", "L", 2), ("na_mass", "可交换钠", "mmol", 0),
    ]),
    ("digestive", "消化道", "胃与肠吸收", [
        ("gastric_volume", "胃内容物", "mL", 1), ("chyme_carb", "待排空碳水", "g", 2),
        ("chyme_protein", "待排空蛋白", "g", 2), ("chyme_fat", "待排空脂肪", "g", 2),
        ("chyme_water", "胃内自由水", "mL", 0), ("chyme_na", "待吸收钠", "mmol", 1),
        ("gut_glucose", "肠内待入血葡萄糖", "mmol", 2), ("gut_amino", "肠内待入血氨基酸", "g", 2),
        ("duodenum_volume", "十二指肠内容物", "mL", 1),
        ("jejunum_volume", "空肠内容物", "mL", 1),
        ("ileum_residue", "回肠残余", "g", 2),
        ("bile", "胆汁", "0-1", 2),
    ]),
    ("endocrine", "内分泌", "激素轴", [
        ("renin", "肾素", "", 2), ("ang2", "血管紧张素 II", "", 2),
        ("aldosterone", "醛固酮", "", 2), ("cortisol", "皮质醇", "", 3),
        ("acth", "ACTH", "", 3), ("adrenaline", "肾上腺素", "", 3),
        ("tsh", "TSH", "", 3), ("t3", "T3", "", 3), ("adh", "抗利尿激素", "", 3),
    ]),
    # 2026-11-11 新增：这 7 个激素一直算在 engine 里（_digest 段），但
    # ORGAN_GROUPS 是硬编码表，没列出来 WebUI 就看不到 —— 用户反馈
    # 「看不到小肠腺体的那 7 个激素」即此。字段全部存在于 BodyState，
    # 这里只是把它们暴露到界面。
    ("gut_hormones", "肠道腺体", "胃与小肠分泌", [
        ("gastrin", "胃泌素", "G 细胞·蛋白", 3),
        ("secretin", "促胰液素", "S 细胞·酸", 3),
        ("cck", "缩胆囊素", "I 细胞·脂肪", 3),
        ("gip", "抑胃肽 GIP", "K 细胞·碳水", 3),
        ("pyy", "酪酪肽 PYY", "回肠·饱腹刹车", 3),
        ("glp1", "胰高血糖素样肽-1", "回肠·饱腹", 3),
        ("ghrelin", "胃饥饿素", "胃底·促食欲", 3),
    ]),
    ("receptors", "受体占据", "0-1", [
        ("r_alpha1", "血管 α1", "", 3), ("r_beta1", "心脏 β1", "", 3),
        ("r_m2", "迷走 M2", "", 3), ("r_insulin", "胰岛素受体", "", 3),
    ]),
    ("thermo", "体温调节", "", [
        ("temp", "核心体温", "℃", 2), ("sweat", "出汗", "0-1", 3),
        ("shiver", "寒战", "0-1", 3), ("thermal", "冷热体感", "50=舒适", 1),
    ]),
    ("drugs", "嗜好品", "乙醇与咖啡因", [
        ("bac", "血乙醇浓度", "g/L", 3), ("caffeine", "血浆咖啡因", "mg", 1),
    ]),
    ("behavior", "行为与节律", "", [
        ("hour", "仿真时刻", "时", 2), ("circ", "昼夜相位", "0-1", 3),
        ("meals", "累计进食", "次", 0), ("drinks", "累计饮水", "次", 0),
        ("urinations", "累计排尿", "次", 0), ("health", "健康度", "0-100", 1),
    ]),
]


def _r(value: Any, digits: int) -> float:
    """安全取整，非数值一律回落 0。"""

    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _clock(hour: float) -> str:
    """把 0-24 的小时数格式化成 HH:MM。"""

    total = int(round(max(0.0, min(23.999, float(hour))) * 60.0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _tone(key: str, value: float) -> str:
    """按正常区间给体征配色档位。"""

    for field, _label, _unit, _digits, span, _low, _high in VITALS:
        if field != key:
            continue
        lo, hi = span
        if value < lo:
            return "warn" if value >= lo * 0.85 else "crit"
        if value > hi:
            return "warn" if value <= hi * 1.15 else "crit"
        return "good"
    return "good"


FEELING_HIGH_BAD = {"hunger": (40.0, 70.0), "thirst": (40.0, 70.0), "stress": (40.0, 70.0),
                     "pain": (10.0, 40.0), "depression": (25.0, 55.0)}


def _feeling_tone(key: str, value: float) -> str:
    """体感配色：精力越高越好，其余（饥饿/口渴/压力/疼痛/心境）越高越糟。"""

    if key == "energy":
        if value >= 60.0:
            return "good"
        return "warn" if value >= 35.0 else "crit"
    warn_at, crit_at = FEELING_HIGH_BAD.get(key, (70.0, 90.0))
    if value >= crit_at:
        return "crit"
    if value >= warn_at:
        return "warn"
    return "good"


def _vital_text(key: str, value: float) -> str:
    """体征的中文判读，优先用引擎自带的分档词。"""

    table = FEELING_LEVELS.get(key)
    if table:
        for upper, word in table:
            if value < upper:
                return word
        return table[-1][1]
    return ""


class LifeBodyPlugin(MaiBotPlugin):
    """麦麦身体插件。"""

    config_model = LifeBodyConfig

    def __init__(self) -> None:
        """初始化运行期容器。"""

        super().__init__()
        self._engine: BodyEngine | None = None
        self._bus: EventBus | None = None
        self._task: asyncio.Task | None = None
        self._samples: deque[dict[str, Any]] = deque(maxlen=2200)
        self._events: deque[dict[str, Any]] = deque(maxlen=800)
        self._last_step_wall = 0.0
        self._last_save_wall = 0.0
        self._started_at = 0.0
        self._debt = 0.0
        self._seed = 20261106
        self._ready = False
        self._step_error = ""
        self._day_base: tuple[int, int, int, int, float] | None = None
        self._last_silence_note = -1.0e9
        self._shared_visit = 0
        self._share_armed = True
        self._link: LifeLink | None = None
        self._link_task: asyncio.Task | None = None
        self._weather_task: asyncio.Task | None = None
        # 联动进餐的去重键。必须存在插件自己身上、不能只存在 LifeLink 里：
        # on_load 的顺序是 _restore() 早于 LifeLink 构造，那时 self._link 还是
        # None，往 self._link 上恢复键值会静默失败，于是每次重启当天已经吃过
        # 的饭窗都会被重新触发一遍（2026-11-09 复现：08:00/08:31/09:02 连吃三份）。
        self._link_meal_key: str = ""

    @staticmethod
    def _history_limit(value: Any) -> int:
        """把历史容量归一化为可用于 deque 的正整数。"""
        try:
            limit = int(value)
        except (TypeError, ValueError, OverflowError):
            return 1
        return max(1, limit)

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """启动仿真。"""

        cfg = self.config
        self._seed = int(cfg.sim.seed)
        self._engine = BodyEngine(enable_behaviour=True, **self._rescue_kwargs())
        self._engine.rng.seed(self._seed) if hasattr(self._engine, "rng") else None
        self._bus = self._engine.bus if getattr(self._engine, "bus", None) is not None else EventBus()
        self._engine.bus = self._bus
        self._samples = deque(maxlen=self._history_limit(cfg.sim.history_limit))
        self._events = deque(maxlen=800)
        self._bus.subscribe(self._on_event)
        self._started_at = time.time()

        # LifeLink 必须早于 _restore() 构造：_restore 要走联动侧的恢复钩子
        # （联动进餐去重键等）。旧顺序是先 _restore 再建 link，导致
        # "self._link is not None" 恒假，所有联动侧状态每次都恢复失败。
        self._link = LifeLink(self.ctx, cfg.link)

        # 启动时必须立刻按配置决定"三餐交不交给麦生活日程"（引擎的 _meals_external）。
        # 旧实现只在 on_config_update 里调 _sync_link_mode，on_load 从不调用，
        # 于是引擎一直停在默认 _meals_external=False：内置 7.5/12.5/18.5 饭点照跑，
        # 与日程的 08:00/12:00/18:00 各吃一遍（服务器实测每顿都成对出现）。
        # 必须早于 _catch_up()：补算离线那段时间也要按新口径走，否则补算时会凭空多吃几顿。
        self._sync_link_mode()

        await self._restore()
        self._catch_up()
        # 补算完再按系统钟对齐：补算只负责让生理量按真实离线时长演进，
        # 钟点最终必须钉在真实时间上（否则离线多久，钟就偏多久）。
        self._sync_wall_clock()
        if self._day_base is None:
            # 新身体从第 1 天零点起；老存档缺基准时同样归零，最多漏记当天已发生的几次行为
            self._day_base = (self._body_sim_day(), 0, 0, 0, 0.0)
        self._seed_samples()

        self._last_step_wall = time.time()
        self._last_save_wall = time.time()
        self._task = asyncio.create_task(self._ticker())
        if cfg.link.enabled:
            self._link_task = asyncio.create_task(self._linker())
        # 天气独立于联动：联动关了天气也要照常更新（外面冷不冷跟麦生活无关）
        if bool(getattr(cfg.link, "weather_enabled", True)):
            self._weather_task = asyncio.create_task(self._weather_loop())
        self._ready = True
        # 食物条数写进启动日志：食物库扩展是「静默装配」的，没有这行
        # 就只能靠面板去数，出问题时（扩展没装上/版本错）看不出来。
        self._get_logger().info(
            "麦麦身体已启动：仿真第 %.1f 天 %s，心率 %.0f，食物 %d 种%s",
            self._body_sim_day(), _clock(self._s.hour), self._s.hr,
            len(FOODS), "" if _FOOD_EXTRA_ADDED > 0 else "（扩展未装）",
        )

    async def on_unload(self) -> None:
        """停止仿真并落盘。"""

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._task = None
        if self._link_task is not None:
            self._link_task.cancel()
            try:
                await self._link_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._link_task = None
        if self._weather_task is not None:
            self._weather_task.cancel()
            try:
                await self._weather_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._weather_task = None
        await self._save()
        self._ready = False

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载：只调整节奏与容量，不重置身体。"""

        del scope, config_data, version
        history_limit = self._history_limit(self.config.sim.history_limit)
        if self._samples.maxlen != history_limit:
            self._samples = deque(self._samples, maxlen=history_limit)
        if self._engine is not None:
            for key, value in self._rescue_kwargs().items():
                setattr(self._engine, key, value)

        # 联动总开关热切换：开则起任务，关则停任务并把注入值归零。
        self._sync_link_mode()

    def _sync_link_mode(self) -> None:
        """按当前配置决定联动任务是否运行、引擎是否接受外部作息。"""

        if self._engine is None:
            return
        want = bool(self.config.link.enabled)
        self._engine.link_enabled = want
        self._engine._meals_external = bool(
            want and self.config.link.sync_meal
        )

        # 月经周期配置注入（默认关闭）
        cycle_cfg = getattr(self.config, "cycle", None)
        if cycle_cfg is not None:
            self._engine.cycle_enabled = bool(getattr(cycle_cfg, "enabled", False))
            self._engine.cycle_length_days = max(
                18.0, min(45.0, float(getattr(cycle_cfg, "cycle_length", 28.0)))
            )
            self._engine.cycle_pain_enabled = bool(
                getattr(cycle_cfg, "pain_enabled", True)
            )
            self._engine.MENSTRUAL_LENGTH = max(
                1.0, min(10.0, float(getattr(cycle_cfg, "period_length", 5.0)))
            )
            # 现实日历锚点：填了「上次月经第一天」就按日历算周期第几天，
            # 留空/写错/写未来都退回「从启用那刻累加」的旧行为。
            self._engine.set_cycle_anchor(
                self._parse_cycle_anchor(getattr(cycle_cfg, "last_period_date", ""))
            )

        # 运动配置注入
        ex_cfg = getattr(self.config, "exercise", None)
        if ex_cfg is not None:
            self._engine.exercise_enabled = bool(getattr(ex_cfg, "enabled", True))
            self._engine.exercise_max_minutes = max(
                5.0, float(getattr(ex_cfg, "max_minutes", 45.0)))
            self._engine.exercise_interval_minutes = max(
                3.0, float(getattr(ex_cfg, "interval_minutes", 15.0)))
            self._engine.exercise_min_energy = max(
                10.0, min(95.0, float(getattr(ex_cfg, "min_energy", 55.0))))
            self._engine.exercise_min_sleep_hours = max(
                0.0, float(getattr(ex_cfg, "min_sleep_hours", 6.0)))

        # 睡眠配置注入。联动开启时，唤醒与拦截都以麦生活的日程为准，
        # 这两个开关自动失效（配置里也写明了会失效）。
        sleep_cfg = getattr(self.config, "sleep", None)
        if sleep_cfg is not None:
            self._engine.sleep_start_hour = float(getattr(sleep_cfg, "start_hour", 23.0))
            self._engine.sleep_end_hour = float(getattr(sleep_cfg, "end_hour", 7.0))
            self._engine.wake_hold_minutes = max(
                0.0, float(getattr(sleep_cfg, "wake_hold_minutes", 10.0))
            )
            self._engine.wake_on_message = bool(
                getattr(sleep_cfg, "wake_on_message", True)
            ) and not want
            # 强制睡眠：连续清醒过久会睡过去。设 0 表示关闭。
            _fsa = float(getattr(sleep_cfg, "force_sleep_after_hours", 20.0))
            self._engine.force_sleep_after_hours = _fsa if _fsa > 0.0 else 9999.0
            self._engine.force_sleep_hours = max(
                1.0, float(getattr(sleep_cfg, "force_sleep_hours", 6.0)))
            self._engine.force_sleep_wakeable = bool(
                getattr(sleep_cfg, "force_sleep_wakeable", True))
        if not want:
            # 关掉联动时立刻回到中性（-1 = 无外部意见），避免残留值继续压低心率。
            # 注意这里不能写 0.0：0.0 是"外部明确说清醒"，只在联动开着时才有意义。
            self._engine.link_sleep = -1.0
            self._engine.link_activity = 0.0
        if want and self._link_task is None and self._ready:
            self._link_task = asyncio.create_task(self._linker())
        elif not want and self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None

    def _rescue_kwargs(self) -> dict[str, Any]:
        """把急救与排泄配置翻译成 BodyEngine 的构造/热更参数。"""

        cfg = self.config.rescue
        # 老配置文件里没有 [excretion] 段时，config.excretion 仍由默认值填充，
        # 所以这里不需要额外兜底（PluginConfigBase 会实例化默认子配置）。
        exc = getattr(self.config, "excretion", None)
        return {
            "rescue_enabled": bool(cfg.enabled),
            "rescue_threshold": max(0.0, min(100.0, float(cfg.threshold))),
            "rescue_critical_seconds": max(0.0, float(cfg.critical_seconds)),
            "rescue_stay_hours": max(0.0, float(cfg.stay_hours)),
            "rescue_restore_health": max(0.0, min(100.0, float(cfg.restore_health))),
            "enable_void": bool(getattr(exc, "show_urination", True)) if exc else True,
            "enable_stool": bool(getattr(exc, "show_stool", True)) if exc else True,
        }

    def _sleep_view(self) -> dict[str, Any]:
        """睡眠面板：四态分期、整夜结构、质量评分、睡眠债。"""

        s = self._s
        stage_zh = {"awake": "清醒", "light": "浅睡", "deep": "深睡", "rem": "快速眼动"}
        total = s.dur_light + s.dur_deep + s.dur_rem
        hours = total / H2S

        def pct(x: float) -> float:
            return round(100.0 * x / total, 1) if total > 0 else 0.0

        return {
            "stage": s.sleep_stage,
            "stage_text": stage_zh.get(s.sleep_stage, s.sleep_stage),
            "depth": _r(s.sleep_depth, 2),
            "sleeping": bool(s.sleeping),
            "cycle_index": int(s.stage_index),
            "cycle_pos": _r(s.cycle_pos, 2),
            "debt_h": _r(s.sleep_debt, 2),
            "quality": _r(s.sleep_quality, 0),
            "total_h": _r(hours, 2),
            "light_h": _r(s.dur_light / H2S, 2),
            "deep_h": _r(s.dur_deep / H2S, 2),
            "rem_h": _r(s.dur_rem / H2S, 2),
            "light_pct": pct(s.dur_light),
            "deep_pct": pct(s.dur_deep),
            "rem_pct": pct(s.dur_rem),
            # 夜间唤醒统计（2026-11-14）
            "wake_count": int(s.wake_count),
            "wake_by_message": int(s.wake_by_message),
            "wake_by_thirst": int(s.wake_by_thirst),
            "wake_by_bladder": int(s.wake_by_bladder),
            "wake_pending": bool(s.wake_pending),
            "last_wake_reason": str(s.last_wake_reason or ""),
            "awake_h": _r(s.dur_awake / H2S, 2),
            # 强制睡眠（连续清醒过久的微睡眠）
            "forced_sleep": bool(getattr(s, "forced_sleep", False)),
            "microsleep_count": int(getattr(s, "microsleep_count", 0) or 0),
            "awake_hours": _r(getattr(s, "awake_hours", 0.0), 1),
        }

    def _parse_cycle_anchor(self, raw: Any):
        """把配置里的「上次月经第一天」解析成 date。

        空字符串、格式不对、日期在未来，一律返回 None（= 不锚定）。
        之所以不抛异常：配置写错不该让整个插件起不来，也不该让周期停在
        某个奇怪相位上 —— 退回旧的累加行为，并在日志里说清楚为什么。
        """
        text = str(raw or "").strip()
        if not text:
            self._cycle_anchor_note = "未填日期，周期从启用那一刻自己累加"
            return None
        # 容忍常见写法：2026/01/01、2026.1.5、2026年1月1日、20260101。
        # 只认一种分隔符对使用者太苛刻（面板里也没地方写格式提示之外的东西）。
        norm = text
        for sep in ("年", "月", "/", ".", " ", "\t"):
            norm = norm.replace(sep, "-")
        norm = norm.replace("日", "")
        norm = "-".join(p for p in norm.split("-") if p.strip())
        day = None
        for fmt, candidate in ((("%Y-%m-%d"), norm),
                               (("%Y%m%d"), norm.replace("-", ""))):
            try:
                day = datetime.strptime(candidate, fmt).date()
                break
            except ValueError:
                continue
        if day is None:
            self._cycle_anchor_note = f"日期「{text}」没认出来，已退回从启用那一刻算"
            self._get_logger().warning(
                "cycle.last_period_date 不是合法日期（%r），按未设置处理；"
                "周期退回「从启用那刻开始累加」", text)
            return None
        today = datetime.now().date()
        if day > today:
            self._cycle_anchor_note = f"日期 {day.isoformat()} 在今天之后，已退回从启用那一刻算"
            self._get_logger().warning(
                "cycle.last_period_date 在未来（%s > 今天 %s），按未设置处理",
                day.isoformat(), today.isoformat())
            return None
        self._cycle_anchor_note = f"相位按现实日历，从 {day.isoformat()} 起算"
        return day

    def _cycle_view(self) -> dict[str, Any]:
        """月经面板：双相位、激素水平、经期状态、疼痛。"""

        s = self._s
        anchor = getattr(self._engine, "cycle_anchor_date", None)
        phase_zh = {"follicular": "卵泡期", "ovulation": "排卵期", "luteal": "黄体期"}
        uterus_zh = {"menstrual": "行经期", "proliferative": "增殖期", "secretory": "分泌期"}
        return {
            "day": _r(s.menstrual_day, 1),
            "length": _r(s.cycle_length, 0),
            "phase": s.menstrual_phase,
            "phase_text": phase_zh.get(s.menstrual_phase, "-"),
            "uterus_phase": s.uterus_phase,
            "uterus_text": uterus_zh.get(s.uterus_phase, "-"),
            "menstruating": bool(s.menstruating),
            "flow": _r(s.menstrual_flow, 2),
            "estrogen": _r(s.estrogen, 2),
            "progesterone": _r(s.progesterone, 2),
            "pain": _r(s.pain, 1),
            "follicle_maturing": bool(s.follicle_maturing),
            "luteal_maturing": bool(s.luteal_maturing),
            # 第几天是按什么算的：calendar=现实日历锚点，sim=从启用那刻累加
            "day_source": "calendar" if anchor else "sim",
            "anchor_date": anchor.isoformat() if anchor else "",
            # 一句人话，说明相位依据；日期填错时这里也会说清楚
            "anchor_note": getattr(self, "_cycle_anchor_note", ""),
        }

    def _link_view(self) -> dict[str, Any]:
        """联动与喂食状态，给 WebUI 展示用。"""

        cfg = self.config
        link = self._link
        view: dict[str, Any] = {
            "enabled": bool(cfg.link.enabled),
            "sync_activity": bool(cfg.link.sync_activity),
            "sync_meal": bool(cfg.link.sync_meal),
            "inject_state": bool(cfg.link.inject_state),
            "sync_clock": bool(cfg.link.sync_clock),
            "share_state": bool(cfg.link.share_state),
            "feeding": bool(cfg.feeding.enabled),
            # -1 表示"麦生活没给睡眠信息"，WebUI 上显示为空更准确
            "sleep": (None if getattr(self._engine, "link_sleep", -1.0) < 0.0
                      else _r(getattr(self._engine, "link_sleep"), 2)) if self._engine else None,
            "activity": _r(getattr(self._engine, "link_activity", 0.0), 2) if self._engine else 0.0,
            "meals_external": bool(getattr(self._engine, "_meals_external", False)) if self._engine else False,
            "clock_offset": _r(getattr(self._engine, "clock_offset", 0.0), 2) if self._engine else 0.0,
            # 时钟来源：system=内置系统钟（默认），sim=纯仿真钟（会漂移）
            "clock_source": str(getattr(self._engine, "_wall_clock_source", "sim")),
            "follow_system_clock": bool(getattr(self.config.sim, "follow_system_clock", True)),
            "ok": False,
            "phase": "",
            "summary": "",
            "last_error": "",
        }
        if link is not None:
            try:
                status = link.status()
            except Exception:  # noqa: BLE001 - 展示层不得因联动异常而失败
                status = {}
            view.update({
                "ok": bool(status.get("last_ok")),
                "phase": str(status.get("phase") or ""),
                "summary": str(status.get("summary") or ""),
                "last_error": str(status.get("last_error") or ""),
            })
        return view

    def _hospital_view(self) -> dict[str, Any]:
        """濒死与急救的对外视图。"""

        s = self._s
        silent_hours = max(0.0, float(self.config.rescue.silent_hours))
        in_hospital = s.hospital_until > s.t
        silent, why = self._silence_window() if in_hospital else (False, "")
        return {
            "critical": bool(s.critical),
            "critical_seconds": _r(max(0.0, s.t - s.critical_since), 0) if s.critical else 0.0,
            "in_hospital": bool(in_hospital),
            "visits": int(s.hospital_visits),
            "reason": str(s.hospital_reason or ""),
            "hours_left": _r((s.hospital_until - s.t) / H2S, 2) if in_hospital else 0.0,
            "silent_hours": _r(silent_hours, 2),
            "silent": bool(silent),
            "silent_note": why,
            "rescue_enabled": bool(self.config.rescue.enabled),
        }

    def _silence_window(self) -> tuple[bool, str]:
        """住院静默窗口：(是否静默, 说明)。"""

        cfg = self.config.rescue
        hours = float(cfg.silent_hours)
        if hours <= 0.0:
            return False, ""
        s = self._s
        if s.hospital_since <= 0.0:
            return False, ""
        if s.hospital_until <= s.t:
            # 已经出院
            return False, ""
        silent_until = s.hospital_since + min(hours, max(0.0, float(cfg.stay_hours))) * H2S
        if s.t >= silent_until:
            return False, ""
        left = (silent_until - s.t) / H2S
        return True, f"住院静默中（还剩 {left:.1f} 小时）"

    # ===== 引擎驱动 =====

    @property
    def _s(self) -> BodyState:
        """当前身体状态。"""

        assert self._engine is not None
        return self._engine.s

    def _body_sim_day(self) -> int:
        """仿真进行到第几天，从 1 起。

        按「午夜翻页」算，不按 t//86400 算：钟点 = t/3600 + clock_offset，
        只有相位为 0 时两者才重合。跟随系统钟后相位必然非 0，
        用 t//86400 会让"今天"在中午清零。
        """

        assert self._engine is not None
        total_h = self._s.t / H2S + float(getattr(self._engine, "clock_offset", 0.0) or 0.0)
        return int(total_h // 24.0) + 1

    def _counters(self) -> tuple[int, int, int, int, float]:
        """计数器快照：(仿真天, 进食, 饮水, 排尿, 累计尿量)。"""

        s = self._s
        return (
            self._body_sim_day(), int(s.meals), int(s.drinks),
            int(s.urinations), float(s.urine_total),
        )

    def _sync_day_base(self) -> None:
        """每步检查是否跨天；跨天瞬间把当时的计数器记为当天基准。

        在步进循环里调用，检测延迟不超过一个 dt（默认 10 仿真秒），
        因此基准值就是零点那一刻的累计量，而不是第一次查询时的值。
        """

        snap = self._counters()
        if self._day_base is None:
            # 首次运行：把"当天零点前"的累计量反推为基准，避免把历史算进今天
            self._day_base = (snap[0], 0, 0, 0, 0.0) if snap[0] == 1 else snap
            return
        if self._day_base[0] != snap[0]:
            self._day_base = snap

    def _today(self) -> dict[str, float]:
        """今日累计量：以当天零点为基准的差值。"""

        self._sync_day_base()
        snap = self._counters()
        base = self._day_base or snap
        return {
            "meals": max(0, snap[1] - base[1]),
            "drinks": max(0, snap[2] - base[2]),
            "urinations": max(0, snap[3] - base[3]),
            "urine_total_l": round(max(0.0, snap[4] - base[4]) / 1000.0, 2),
        }

    def _data_dir(self) -> Path:
        """插件数据目录，不存在时创建。"""

        path = self.ctx.paths.data_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _weather_loop(self) -> None:
        """独立的天气轮询。与联动解耦：联动关了也照常更新室外环境。"""

        interval = max(300.0, float(getattr(self.config.link, "weather_refresh_seconds", 1800.0)))
        # 启动后先拉一次，避免头半小时没有天气数据
        try:
            await self._refresh_outdoor()
        except Exception:
            pass
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._ready:
                    continue
                await self._refresh_outdoor()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 天气失败不影响身体
                self._get_logger().debug("天气刷新失败: %s: %s", type(exc).__name__, exc)

    async def _refresh_outdoor(self) -> None:
        """拉一次室外天气并写进状态。任何失败静默降级。"""

        link = self._link
        if link is None:
            return
        cfg = self.config
        tz = "Asia/Shanghai"
        env_cfg = getattr(cfg, "environment", None)
        if env_cfg is not None:
            tz = str(getattr(env_cfg, "timezone", "") or tz)
        # 室内外判断要用当前场景地点
        loc = str(getattr(link, "_location_hint", "") or "")
        try:
            scene = await link._call("get_current_scene")
            cur = (scene or {}).get("current") or {}
            loc = str(cur.get("location") or "") or loc
            link._location_hint = loc
        except Exception:
            pass

        outdoor = await link.fetch_outdoor(timezone=tz, location=loc)
        if not outdoor:
            return
        s = self._s
        s.env_temp_c = float(outdoor.get("temp_c") or 22.0)
        s.env_humidity = float(outdoor.get("humidity") or 50.0)
        s.env_feels_c = float(outdoor.get("feels_c") or 22.0)
        s.env_load = float(outdoor.get("load") or 0.0)
        s.env_indoor = bool(outdoor.get("indoor", True))
        s.env_updated_at = float(outdoor.get("fetched_at") or 0.0)
        s.env_wind_ms = float(outdoor.get("wind_ms") or 0.0)

    async def _linker(self) -> None:
        """联动后台循环：定时拉麦生活状态，喂给引擎。

        与 _ticker 分开跑：联动是网络/跨进程调用，慢也不能拖住仿真；
        拉失败时保留上一次的值，不把睡眠状态突变成 0。
        """

        interval = max(10.0, float(self.config.link.interval_seconds))
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._ready or self._link is None:
                    continue
                if not self.config.link.enabled:
                    continue
                try:
                    await self._link_cycle()
                except Exception as exc:  # noqa: BLE001 - 联动失败不能拖垮插件
                    self._get_logger().warning(
                        "麦麦身体联动失败：%s: %s", type(exc).__name__, exc
                    )
        except asyncio.CancelledError:
            raise

    async def _link_cycle(self) -> None:
        """一次联动周期：拉状态、写引擎、按日程触发进餐。"""

        link = self._link
        engine = self._engine
        if link is None or engine is None:
            return

        data = await link.pull()
        if not data:
            # 麦生活掉线/超时：超过两个拉取周期还拿不到状态，就把注入值退回中性。
            # 早前一直保留上次的值，半夜断线会让她"停在深睡"里直到重启。
            interval = max(10.0, float(self.config.link.interval_seconds))
            stale_after = interval * 2.0
            last_ok = float(getattr(link, "_last_ok", 0.0) or 0.0)
            if last_ok and (time.time() - last_ok) > stale_after:
                if self._engine is not None and self._engine.link_sleep != -1.0:
                    self._engine.link_sleep = -1.0
                    self._engine.link_activity = 0.0
                    self._get_logger().warning(
                        "麦生活联动已失联 %.0f 秒，睡眠/活动注入值退回中性", time.time() - last_ok
                    )
            return

        cfg = self.config.link
        if cfg.sync_activity:
            # 显式取 -1 兜底：拿不到睡眠阶段时保持"无外部意见"，
            # 不能退化成 0.0（那等于替麦生活宣布"她醒着"）。
            raw_sleep = data.get("sleep")
            engine.link_sleep = -1.0 if raw_sleep is None else float(raw_sleep)
            engine.link_activity = float(data.get("activity") or 0.0)

        # ---- 日程运动（2026-11-14）----
        # 麦生活的 kind 没有 exercise，只能从 summary 认。
        # 认出来之后还要过一遍身体否决：外面暴晒/刮风/正在痛经就不跑。
        ex_cfg = getattr(self.config, "exercise", None)
        if ex_cfg is not None and bool(getattr(ex_cfg, "enabled", True)):
            if not engine.s.exercise_intent:
                try:
                    scene = await link._call("get_current_scene")
                    cur = (scene or {}).get("current") or {}
                    summary = str(cur.get("summary") or "")
                    kind = str(cur.get("kind") or "")
                    level = link.detect_exercise(summary)
                    if level and kind not in ("sleep", "nap"):
                        ok = engine.request_exercise(level, 30.0)
                        if ok:
                            self._get_logger().info(
                                "日程运动已接受：%s（%s）", summary, level)
                        elif engine.s.exercise_refused:
                            self._get_logger().info(
                                "日程运动被身体否决：%s → %s",
                                summary, engine.s.exercise_refused)
                except Exception:
                    pass

        if cfg.sync_meal:
            slot, key, summary, items = await link.meal_trigger(self._s.hour)
            if slot and summary and len(items) < 2:
                # 摘要里只认出一样（或一样都没有）时交给 utils 模型补全：
                # 「番茄炒蛋」关键词只能认出 Egg，模型能补出 Rice；
                # 「准备晚饭」这种只有动作的，模型会按正餐补主食。
                items, src = await decompose_menu(summary, limit=6, llm=self.ctx.llm)
                if items and src == "llm":
                    self._get_logger().info(
                        "日程摘要经 utils 拆出食材：%s（原文：%s）",
                        combo_summary(items),
                        summary,
                    )
            if slot:
                # 去重键落到插件自己身上并立刻随下一次自动存档落盘：
                # 只在 LifeLink 内存里时，重启后同一饭窗会被再吃一遍。
                if key:
                    self._link_meal_key = key
                if not engine._link_meal:
                    engine._link_meal = slot
                    # 日程点名的具体食物一起送进去；为空则引擎按餐段随机配。
                    engine._link_meal_items = list(items) if items else None
                    if items:
                        self._get_logger().info(
                            "麦生活日程触发进食：%s，日程点名 %s（日程：%s）",
                            slot,
                            combo_summary(items),
                            summary or "-",
                        )
                    else:
                        self._get_logger().info(
                            "麦生活日程触发进食：%s（日程：%s）",
                            slot,
                            summary or "-",
                        )

        # 内置时钟优先：两者都开时会互相拉扯（一个按系统时间写相位、
        # 一个按麦生活环境时间写相位），谁后跑谁生效，钟点会来回跳。
        if cfg.sync_clock and not bool(getattr(self.config.sim, "follow_system_clock", True)):
            delta = await link.clock_offset(self._s.hour)
            if delta:
                # s.hour 已经含 clock_offset，所以 delta 是"当前残差"，
                # 加回去即是正确的一次对齐；下一轮残差归零就不再调整（有 0.1h 死区）。
                engine.clock_offset = (engine.clock_offset + delta) % 24.0
                self._get_logger().info(
                    "麦麦身体时钟对齐：相位移动 %+.2f 小时，当前 %.1f 点",
                    delta,
                    self._s.hour,
                )

        if cfg.share_state:
            await self._share_body_state()

    async def _share_body_state(self) -> None:
        """住院或首次进入濒危时，把这件事交给麦生活作为一次主动分享契机。

        每件事只分享一次，靠 (事件类型, 计数) 去重；出院后不补发，
        避免身体好转了还在聊昨天那场急救。
        """

        link = self._link
        engine = self._engine
        if link is None or engine is None:
            return
        s = self._s

        # 住院：按 hospital_visits 去重
        if s.hospital_until > s.t and s.hospital_visits > self._shared_visit:
            self._shared_visit = s.hospital_visits
            left = (s.hospital_until - s.t) / H2S
            ok = await link.share(
                topic=f"我刚刚被送去医院急救了，原因是{s.hospital_reason or '身体撑不住'}",
                motive=f"身体状态真的出了问题，想让对方知道，大概还要 {left:.1f} 小时才能出院",
                weight=0.8,
            )
            if ok:
                self._get_logger().info("麦麦身体已向麦生活提交住院分享契机")
            return

        # 濒危但还没送医：按 critical_since 去重
        if s.critical and not self._share_armed:
            return
        if s.critical and self._share_armed and s.health <= float(engine.rescue_threshold) + 5.0:
            self._share_armed = False
            await link.share(
                topic=f"我现在身体很不对劲，{engine._rescue_reason()}",
                motive="说不清具体怎么了，但感觉需要被关心一下",
                weight=0.5,
            )
        elif not s.critical:
            # 恢复后重新上膛，下一次濒危才能再分享
            self._share_armed = True

    async def _ticker(self) -> None:
        """后台步进循环：按墙钟差值推进仿真，绝不做重计算。"""

        interval = 1.0
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._ready:
                    continue
                try:
                    self._advance_wall()
                except Exception as exc:  # noqa: BLE001 - 单步失败不能杀死 ticker
                    self._step_error = f"{type(exc).__name__}: {exc}"
                    self._get_logger().warning("麦麦身体步进失败：%s", self._step_error)
                if self.config.sim.autosave_seconds > 0 and (
                    time.time() - self._last_save_wall >= float(self.config.sim.autosave_seconds)
                ):
                    await self._save()
        except asyncio.CancelledError:
            raise

    def _sync_wall_clock(self) -> None:
        """内置时钟：把身体钟点对齐到系统时间。

        与麦生活的 sync_clock 是两条独立通路。开着内置时钟时以本机系统
        时间为准，联动那边的时钟同步会被让位（见 _link_cycle），
        否则两个源会互相拉扯，谁后写谁生效。
        """

        engine = self._engine
        if engine is None:
            return
        if not bool(getattr(self.config.sim, "follow_system_clock", True)):
            engine.follow_wall_clock = False
            engine._wall_clock_source = "sim"
            return
        engine.follow_wall_clock = True
        before = engine.s.hour
        after = engine.sync_wall_clock()
        engine._wall_clock_source = "system"
        # 首次对齐或大跨度校正时记一条日志，方便排查"时间怎么突然对了"
        if abs(((after - before + 12.0) % 24.0) - 12.0) > 0.5:
            self._get_logger().info(
                "麦麦身体时钟已对齐系统时间：%s -> %s",
                _clock(before), _clock(after),
            )

    def _advance_wall(self) -> None:
        """按墙钟推进仿真：攒够一个 dt 就走一步。"""

        cfg = self.config.sim
        now = time.time()
        elapsed = max(0.0, now - self._last_step_wall)
        self._last_step_wall = now
        self._debt += elapsed * max(0.01, float(cfg.speed))

        dt = max(1.0, min(20.0, float(cfg.dt)))
        steps = 0
        limit = 600
        # 内置时钟：先按系统墙钟校正相位，再走步。放在循环前，
        # 保证本轮每一步的饭点/睡眠判断都落在真实钟点上。
        self._sync_wall_clock()
        while self._debt >= dt and steps < limit:
            self._engine.step(dt)
            self._debt -= dt
            steps += 1
            self._sync_day_base()
            self._maybe_sample()
        if steps >= limit:
            self._debt = 0.0

    def _catch_up(self) -> None:
        """启动时补齐离线期间的身体运转。"""

        assert self._engine is not None
        cfg = self.config.sim
        if self._offline_wall <= 0.0:
            # 没有有效基准（1970 或未初始化）就不补算，否则会一次性补满 catch_up_max_hours
            return
        gap_wall = max(0.0, time.time() - self._offline_wall)
        gap_sim = gap_wall * max(0.01, float(cfg.speed))
        if bool(getattr(cfg, "follow_system_clock", True)):
            # 跟随系统钟时不能截断：截掉的那段生理演化永远补不回来，
            # 而钟点又已被拉回真实时间，两者会永久错开。步数另有 20000 上限兜底。
            sim_gap = gap_sim
        else:
            sim_gap = min(gap_sim, float(cfg.catch_up_max_hours) * H2S)
        if sim_gap <= 0.0:
            return
        dt = max(1.0, min(20.0, float(cfg.dt)))
        steps = int(sim_gap // dt)
        for _ in range(min(steps, 20000)):
            self._engine.step(dt)

    def _maybe_sample(self) -> None:
        """每 60 仿真秒记一个采样点。"""

        s = self._s
        if self._samples and s.t - self._samples[-1]["t"] < 59.0:
            return
        self._samples.append(self._sample_point())

    def _seed_samples(self) -> None:
        """把当前状态作为第一个采样点。"""

        if not self._samples:
            self._samples.append(self._sample_point())

    def _sample_point(self) -> dict[str, Any]:
        """构造一个采样点。"""

        s = self._s
        return {
            "t": _r(s.t, 1),
            "clock": _clock(s.hour),
            "day": self._body_sim_day(),
            "hr": _r(s.hr, 1),
            "sbp": _r(s.sbp, 1),
            "dbp": _r(s.dbp, 1),
            "glucose": _r(s.glucose, 2),
            "temp": _r(s.temp, 2),
            "energy": _r(s.energy, 1),
            "hunger": _r(s.hunger, 1),
            "thirst": _r(s.thirst, 1),
            "gastric": _r(s.gastric_volume, 1),
            "bac": _r(s.bac, 3),
            "na": _r(s.na, 2),
            "sao2": _r(s.sao2, 1),
        }

    def _on_event(self, ev: Any) -> None:
        """把引擎事件转存为 WebUI 可读的字典。"""

        self._events.append({
            "t": _r(getattr(ev, "t", 0.0), 1),
            # 与状态时钟（_sample_point 用 s.hour）必须用同一个相位：
            # s.hour = (t/H2S + clock_offset) % 24，事件侧漏加 clock_offset
            # 会让「最近发生的事」整条时间轴偏移 offset 小时，跟日程怎么比都对不上。
            "clock": _clock(
                (getattr(ev, "t", 0.0) / H2S + float(getattr(self._engine, "clock_offset", 0.0) or 0.0))
                % 24.0
            ),
            "day": int(getattr(ev, "t", 0.0) // 86400.0) + 1,
            "kind": str(getattr(ev, "kind", "")),
            "level": str(getattr(ev, "severity", "info")),
            "text": str(getattr(ev, "text", "")),
        })

    # ===== 存档 =====

    async def _save(self) -> None:
        """把身体快照与墙钟时间写入数据目录。"""

        if self._engine is None:
            return
        self._last_save_wall = time.time()
        payload = {
            "version": PLUGIN_VERSION,
            "saved_wall": self._last_save_wall,
            "seed": self._seed,
            "state": self._s.snapshot(),
            "samples": list(self._samples)[-600:],
            "events": list(self._events)[-200:],
            "day_base": list(self._day_base) if self._day_base else None,
            # 身体时钟的"上一次钟点"必须一起存。否则重启后引擎里恒为 0.0，
            # 第一步就会判定"跨过了 07:30"，凭空多吃一份早餐（见 2026-11-08 11:44/11:54/12:03 连吃）。
            "prev_hour": float(getattr(self._engine, "_prev_hour", 0.0)),
            # 时钟相位也要存：新身体从早上 8 点起步（不是 0 点），这个相位
            # 是 s.hour 与 s.t 的唯一换算系数。不存的话，重启后新引擎按 8 点
            # 起步、再套上存档里的 s.t，钟点会整整偏 8 小时，昼夜曲线全错位。
            "clock_offset": float(getattr(self._engine, "clock_offset", 0.0)),
            # 联动进餐的去重键也要存：它只在内存里时，重启后当天的餐段会被
            # 再触发一次，又变成"重启就多吃一顿"。
            "link_meal_key": str(self._link_meal_key or ""),
        }
        target = self._data_dir() / SAVE_FILE
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            self._get_logger().warning("麦麦身体存档失败：%s", exc)

    async def _restore(self) -> None:
        """读取存档；缺失或结构异常时从默认身体开始。"""

        # 基准取"现在"：无存档或存档损坏时不该补算离线时间，身体从零开始走
        self._offline_wall = time.time()
        target = self._data_dir() / SAVE_FILE
        if not target.exists():
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("存档根节点必须是对象")

            raw = payload.get("state")
            if raw is not None and not isinstance(raw, dict):
                raise TypeError("state 必须是对象")
            known = set(BodyState.__dataclass_fields__.keys())
            restored = {k: v for k, v in (raw or {}).items() if k in known}
            if restored:
                seed_state = BodyState(**restored)
                assert self._engine is not None
                self._engine.s = seed_state

            saved_wall = payload.get("saved_wall")
            self._offline_wall = float(saved_wall or 0.0)

            # 钟点基准：优先用存档值；老存档没有这个字段时退化为"当前钟点"。
            # 两种做法都保证重启不会把已经过去的饭点当成刚跨过。
            assert self._engine is not None
            clock_offset = payload.get("clock_offset")
            if isinstance(clock_offset, (int, float)) and 0.0 <= float(clock_offset) < 24.0:
                self._engine.clock_offset = float(clock_offset)
            else:
                # 老存档（本次改动之前）都是从 0 点相位跑出来的，
                # 不能套用新身体默认的 8 点相位，否则整条昼夜曲线偏 8 小时。
                self._engine.clock_offset = 0.0

            prev_hour = payload.get("prev_hour")
            if isinstance(prev_hour, (int, float)) and 0.0 <= float(prev_hour) < 24.0:
                self._engine._prev_hour = float(prev_hour)
            else:
                self._engine._prev_hour = self._engine.s.hour

            stored_key = payload.get("link_meal_key")
            if isinstance(stored_key, str):
                self._link_meal_key = stored_key
                # 同步到联动客户端，让本轮的去重判断直接接着上次的键走。
                if self._link is not None:
                    self._link._last_meal_key = stored_key

            samples = payload.get("samples") or []
            if not isinstance(samples, list):
                raise TypeError("samples 必须是数组")
            for point in samples:
                if isinstance(point, dict) and "t" in point:
                    self._samples.append(point)

            events = payload.get("events") or []
            if not isinstance(events, list):
                raise TypeError("events 必须是数组")
            for item in events:
                if isinstance(item, dict) and "text" in item:
                    self._events.append(item)

            base = payload.get("day_base")
            if base is not None:
                if not isinstance(base, list) or len(base) != 5:
                    raise TypeError("day_base 必须是长度为 5 的数组")
                self._day_base = (
                    int(base[0]), int(base[1]), int(base[2]), int(base[3]), float(base[4]),
                )
        except (OSError, ValueError, TypeError, KeyError, AssertionError) as exc:
            self._get_logger().warning("麦麦身体存档结构异常，重新开始：%s", exc)
            self._offline_wall = time.time()
            self._samples.clear()
            self._events.clear()
            self._day_base = None
            if self._engine is not None:
                self._engine.s = BodyState()

    # ===== WebUI API =====

    # ===== 住院静默 =====

    @HookHandler(
        "chat.receive.before_process",
        name="hospital_silence",
        description="住院急救期间不回应任何消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hospital_silence(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        """住院静默窗口内吞掉入站消息，直接中止处理链。"""

        del kwargs
        if self._engine is None or not self._ready:
            return None
        try:
            silent, why = self._silence_window()
        except Exception as exc:  # noqa: BLE001 - 钩子异常不能打断主链
            self._get_logger().debug("住院静默判断失败：%s", exc)
            return None
        if not silent:
            return None

        # 事件按 10 分钟节流，避免每条消息都记一笔
        s = self._s
        if s.t - self._last_silence_note >= 600.0:
            self._last_silence_note = s.t
            self._events.append({
                "t": _r(s.t, 1),
                "clock": _clock(s.hour),
                "day": self._body_sim_day(),
                "kind": "quiet",
                "level": "notice",
                "text": f"住院期间没有回应消息（{s.hospital_reason or '急救中'}）",
            })
        self._get_logger().debug("住院静默，忽略消息：%s", why)
        return {"action": "abort", "modified_kwargs": {"message": message} if message is not None else {}}

    @API(name="webui_now", description="麦麦此刻的身体状态：体征、体感、胃内容物与今日行为计数", version="1", public=False)
    async def webui_now(self, **kwargs: Any) -> dict[str, Any]:
        """返回实时身体快照。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            vitals = []
            for field, label, unit, digits, _span, _low, _high in VITALS:
                value = float(getattr(s, field, 0.0))
                vitals.append({
                    "key": field, "label": label, "unit": unit,
                    "value": _r(value, digits),
                    "text": _vital_text(field, value),
                    "tone": _tone(field, value),
                })
            feelings = []
            for field, label in FEELINGS:
                value = float(getattr(s, field, 0.0))
                feelings.append({
                    "key": field, "label": label,
                    "value": _r(value, 0),
                    "text": _vital_text(field, value),
                    "tone": _feeling_tone(field, value),
                })
            return {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "version": PLUGIN_VERSION,
                "clock": {
                    "time": _clock(s.hour),
                    "day": self._body_sim_day(),
                    "phase": phase(s.hour),
                    "t": _r(s.t, 1),
                    "speed": _r(self.config.sim.speed, 2),
                    "dt": _r(self.config.sim.dt, 1),
                    "alive": True,
                    "health": _r(s.health, 1),
                },
                "hospital": self._hospital_view(),
                "link": self._link_view(),
                "speech": {"describe": describe(s), "brief": brief(s)},
                "vitals": vitals,
                "feelings": feelings,
                "gut": {
                    "gastric_ml": _r(s.gastric_volume, 1),
                    "phase": eat_phase(s.gastric_volume),
                    "last_meal_items": str(s.last_meal_items or ""),
                    "last_drink_items": str(s.last_drink_items or ""),
                    "bac": _r(s.bac, 3),
                    "bac_text": drunk_level(s.bac) if s.bac > 0.10 else "",
                    "caffeine": _r(s.caffeine, 1),
                },
                "sleep": self._sleep_view(),
                "cycle": self._cycle_view(),
                "cycle_enabled": bool(getattr(self.config.cycle, "enabled", False)),
                "today": self._today(),
                "events": list(self._events)[-12:][::-1],
                "stock": list(self._samples)[-40:],
                "error": self._step_error,
            }
        except Exception as exc:  # noqa: BLE001 - 宿主只认异常
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_history", description="体征曲线：最近 N 小时的逐分钟采样点", version="1", public=False)
    async def webui_history(self, hours: int = 12, **kwargs: Any) -> dict[str, Any]:
        """返回指定小时窗内的采样序列。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            window = max(1, min(72, int(hours)))
            span = window * H2S
            now_t = self._s.t
            points = [p for p in self._samples if now_t - float(p.get("t", 0.0)) <= span]
            keys = ["hr", "sbp", "dbp", "glucose", "temp", "energy", "hunger", "thirst", "gastric", "bac", "na"]
            series = {k: [_r(p.get(k, 0.0), 2) for p in points] for k in keys}
            return {
                "hours": window,
                "count": len(points),
                "labels": [p.get("clock", "") for p in points],
                "days": [p.get("day", 1) for p in points],
                "series": series,
                "now": self._sample_point(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_events", description="行为事件流：进食、饮水、排尿与告警", version="1", public=False)
    async def webui_events(self, limit: int = 60, kind: str = "", **kwargs: Any) -> dict[str, Any]:
        """返回最近的引擎事件。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            size = max(1, min(400, int(limit)))
            wanted = str(kind or "").strip()
            items = [e for e in self._events if not wanted or e.get("kind") == wanted]
            counts: dict[str, int] = {}
            for item in self._events:
                key = str(item.get("kind") or "")
                counts[key] = counts.get(key, 0) + 1
            return {"items": items[-size:][::-1], "counts": counts, "total": len(self._events)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_waveforms", description="生理波形：ECG 心电与 EEG 脑电的实时合成数据", version="1", public=False)
    async def webui_waveforms(self, seconds: float = 10.0, **kwargs: Any) -> dict[str, Any]:
        """返回 ECG 与 EEG 的波形点数组。

        波形是**按当前生理状态实时合成**的，不落库 —— 真实采样率下
        一分钟就是上万点，存档会爆炸。前端每次刷新重新取。

        时间约定（前端依赖，改这里必须同步改 webui/organs.html）：
          - 相位用**墙钟**推进，不用仿真时间（dt=10s，拿它当相位会每步跳一次）。
          - 返回 t0 = 第一个采样点的 epoch 秒，缓冲区覆盖 [t0, t0+seconds]。
          - t0 固定取 「现在 - _WAVE_LEAD_SEC(8s)」，于是缓冲区覆盖
            [现在-8s, 现在+12s]：前端以 8 秒窗口显示「到此刻为止」，
            10 秒后预取下一批时正好接上，看不出接缝。

        2026-11-14 修：旧实现没有时间参数，generate_ecg/generate_eeg 每次都从
        t=0 开始生成，连续两次调用返回的点完全相同 —— 视觉上就是一段波形
        无限循环重播。现在实测连续两次调用结果不同，且跨批次重叠区逐点一致。

        注意：这是仿真波形，不是医学数据。
        """

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            from .body import ecg as _ecg, eeg as _eeg

            win = max(3.0, min(30.0, float(seconds)))

            # ---- 时间基准：用墙钟，不用仿真步 ----
            # 仿真 dt=10 秒一步，如果拿仿真时间当波形相位，扫描头会每 10 秒
            # 跳一次。波形要像监护仪一样「现在这一刻也在走」，就必须用墙钟。
            # t_off 是缓冲区第一个采样点对应的 epoch 秒；前端用
            # (Date.now()/1000 - t0) 算出自己该显示哪一段。
            t_off = time.time() - _WAVE_LEAD_SEC

            # ---- ECG：由心率与每搏输出量驱动 ----
            ecg_data = _ecg.generate_ecg(
                hr=float(getattr(s, "hr", 72.0)),
                sv=float(getattr(s, "sv", 82.0)),
                rr=float(getattr(s, "rr", 16.0)),
                seconds=win,
                t_offset=t_off,
            )

            # ---- EEG：由睡眠阶段驱动；情绪接口预留未接线 ----
            stage = str(getattr(s, "sleep_stage", "awake") or "awake")
            if not bool(getattr(s, "sleeping", False)):
                stage = "awake"
            # 情绪通路：字段还没建，取默认中性。
            # 将来 BodyState 加上 emotion_arousal / emotion_valence 后，
            # 这里会自动开始读取，无需改本接口。
            arousal = float(getattr(s, "emotion_arousal", 0.0) or 0.0)
            valence = float(getattr(s, "emotion_valence", 0.0) or 0.0)
            eeg_data = _eeg.generate_eeg(
                stage=stage,
                seconds=win,
                arousal=arousal,
                valence=valence,
                t_offset=t_off,
            )

            return {
                "ecg": ecg_data,
                "eeg": eeg_data,
                # 缓冲区第一个采样点的 epoch 秒。前端靠它把数据池对齐到墙钟，
                # 从而在每次重新拉取时接上上一批的尾巴，而不是每 16 秒跳回开头。
                "t0": round(t_off, 6),
                "lead": _WAVE_LEAD_SEC,
                "clock": {
                    "time": _clock(s.hour),
                    "day": self._body_sim_day(),
                    "phase": phase(s.hour),
                },
                "disclaimer": "仿真波形，非医学数据，不可用于诊断",
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_organs", description="内脏细则：心血管、呼吸、代谢、肾脏、消化道、内分泌、受体、体温与嗜好品", version="1", public=False)
    async def webui_organs(self, **kwargs: Any) -> dict[str, Any]:
        """返回分组后的全部器官字段。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            groups = []
            for gid, title, subtitle, fields in ORGAN_GROUPS:
                rows = []
                for field, label, unit, digits in fields:
                    rows.append({
                        "field": field, "label": label, "unit": unit,
                        "value": _r(getattr(s, field, 0.0), digits),
                    })
                groups.append({"id": gid, "title": title, "subtitle": subtitle, "rows": rows})
            return {
                "clock": {"time": _clock(s.hour), "day": self._body_sim_day(), "phase": phase(s.hour)},
                "groups": groups,
                "field_count": len(BodyState.__dataclass_fields__),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_foods", description="食物库：311 种食物的份量与成分（23 项 FoodType + 288 项扩展），以及三餐候选池", version="1", public=False)
    async def webui_foods(self, **kwargs: Any) -> dict[str, Any]:
        """返回只读食物表。"""

        del kwargs
        try:
            foods = []
            for item in ALL_FOODS.values() if isinstance(ALL_FOODS, dict) else ALL_FOODS:
                foods.append({
                    "key": item.key, "label": item.label, "group": item.group,
                    "portion": item.portion, "volume_ml": _r(item.volume_ml, 0),
                    "kcal": _r(item.kcal, 0), "carb_g": _r(item.carb_g, 1),
                    "protein_g": _r(item.protein_g, 1), "fat_g": _r(item.fat_g, 1),
                    "na_mmol": _r(item.na_mmol, 1), "alcohol_g": _r(item.alcohol_g, 1),
                    "caffeine_mg": _r(item.caffeine_mg, 0),
                    "selectable": bool(item.selectable),
                })
            def _pool_labels(pool: Any) -> list[str]:
                """候选池既可能是组合列表，也可能是单个食物 key，统一翻成中文。"""

                out: list[str] = []
                for entry in pool or []:
                    keys = [entry] if isinstance(entry, str) else list(entry)
                    text = combo_summary(keys)
                    if not text:
                        text = "、".join(FOODS[k].label for k in keys if k in FOODS)
                    out.append(text or str(entry))
                return out

            pools = {slot: _pool_labels(pool) for slot, pool in MEAL_POOLS.items()}
            pools["drink"] = _pool_labels(DRINK_POOL)
            pools["snack"] = _pool_labels(SNACK_POOL)
            return {"foods": foods, "pools": pools, "count": len(foods)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_intervene", description="对麦麦的身体做一次干预：喝水、进食、开始或停止运动", version="1", public=False)
    async def webui_intervene(self, kind: str = "drink", **kwargs: Any) -> dict[str, Any]:
        """执行一次行为干预。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            action = str(kind or "").strip()
            if action == "drink":
                self._engine._drink()
            elif action == "eat":
                self._engine._eat(self._engine._meal_slot(self._s.hour))
            elif action == "snack":
                self._engine._eat("snack")
            elif action == "exercise_on":
                self._s.exercising = True
            elif action == "exercise_off":
                self._s.exercising = False
            else:
                return {"success": False, "error": f"未知干预：{action}"}
            self._maybe_sample()
            return {"success": True, "action": action, "brief": brief(self._s),
                    "describe": describe(self._s)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    # ===== 给 LLM 的工具 =====

    # ===== 联动：身体状态注入对话 =====

    def _body_prompt_note(self) -> str:
        """把此刻的身体压成一两句自然语言，供注入对话上下文。

        只取会影响说话的几项，不倾倒全部 66 个字段：注入的是给她看的提示，
        不是监看屏。健康、饮食与精力三项用词刻意保留口语感。
        """

        s = self._s
        parts: list[str] = []

        if s.health < 40:
            parts.append(f"身体很虚（健康 {s.health:.0f}/100）")
        elif s.health < 70:
            parts.append(f"身体有点乏（健康 {s.health:.0f}/100）")

        if s.glucose < 3.9:
            parts.append(f"血糖偏低（{s.glucose:.1f} mmol/L），反应会迟钝些")
        elif s.glucose > 7.8:
            parts.append(f"血糖偏高（{s.glucose:.1f} mmol/L）")

        # 最近一餐：这是最常被问到的一项，3 小时内才说，超过就当没这回事，
        # 免得半夜还被当成"刚吃完"。用词跟 describe() 保持一致。
        since_meal = (s.t - s.last_meal_t) / H2S
        if getattr(s, "last_meal_items", "") and since_meal <= 3.0:
            parts.append(f"刚吃了{s.last_meal_items}")

        fasted = since_meal
        if s.hunger > 65:
            parts.append(f"饿得厉害（{fasted:.0f} 小时没吃东西了）")
        elif s.hunger > 45:
            parts.append("有点饿")

        if s.thirst > 55:
            parts.append("口渴")

        if s.energy < 45:
            parts.append(f"精力不济（{s.energy:.0f}/100）")

        if s.hospital_until > s.t:
            left = (s.hospital_until - s.t) / H2S
            parts.append(f"正在住院（{s.hospital_reason or '急救中'}，约 {left:.1f} 小时后出院）")

        if not parts:
            return ""
        return (
            "【你此刻的身体】"
            + "；".join(parts)
            + "。说到自己状态时请以此为准，不要夸大也不要隐瞒。"
        )

    @HookHandler(
        "chat.receive.before_process",
        name="body_wake_on_message",
        description="入站消息到达：睡着时把她叫醒，或按配置直接拦下消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_incoming_message(self, **kwargs: Any) -> dict[str, Any] | None:
        """消息到达时的睡眠联动。

        两件事：
          1. 睡着且开启了「消息唤醒」→ 叫醒她（唤醒后需连续安静若干分钟才重新入睡）
          2. 睡着且开启了「睡眠中不响应」→ 中止这条消息，不让它进入后续链路

        联动开启时这两项都交给麦生活的日程决定，本钩子不介入。
        """

        if not self._ready or self._engine is None:
            return None

        sleep_cfg = getattr(self.config, "sleep", None)
        if sleep_cfg is None:
            return None

        # 联动开启 → 以麦生活日程为准，本钩子不做任何拦截或唤醒
        link_on = bool(getattr(self.config.link, "enabled", False))

        s = self._s
        if not s.sleeping and not s.wake_pending:
            return None

        # 1) 拦截：睡眠中不响应消息
        if (not link_on) and bool(getattr(sleep_cfg, "block_while_asleep", False)):
            if s.sleeping:
                self._get_logger().info("睡眠中拦下一条消息（睡眠中不响应已开启）")
                return {
                    "action": "abort",
                    "reason": "麦麦正在睡觉，不处理消息",
                }

        # 2) 唤醒：来消息叫醒她
        if (not link_on) and bool(getattr(sleep_cfg, "wake_on_message", False)):
            if s.sleeping:
                self._engine.wake_by_message()
                self._get_logger().info("麦麦被消息唤醒，%.0f 分钟内保持清醒",
                                        float(getattr(sleep_cfg, "wake_hold_minutes", 10.0)))

        return None

    @HookHandler(
        "maisaka.planner.before_request",
        name="body_context",
        description="把当前身体状态注入对话上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_body_context(self, **kwargs: Any) -> dict[str, Any] | None:
        """在麦生活请求模型前注入身体状态。

        只改 items 里的 system text，不碰工具定义与历史，避免破坏载荷结构。
        开关关闭或没有值得说的内容时原样放行。
        """

        if not self._ready or self._engine is None:
            return None
        if not self.config.link.enabled or not self.config.link.inject_state:
            return None

        note = self._body_prompt_note()
        if not note:
            return None

        items = kwargs.get("items")
        if not isinstance(items, list):
            # 载荷契约变了就放弃注入，宁可不说也不要改坏请求
            return None

        try:
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                if item.get("item_type") != "SystemMessageItem":
                    continue
                parts = item.get("parts")
                if not isinstance(parts, list):
                    item["parts"] = [{"type": "text", "text": note}]
                    return {"action": "continue", "modified_kwargs": kwargs}
                if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == "text":
                    parts[-1]["text"] = (parts[-1].get("text") or "") + "\n\n" + note
                else:
                    parts.append({"type": "text", "text": note})
                return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # noqa: BLE001 - 注入失败按未注入处理
            self._get_logger().warning("身体状态注入失败：%s: %s", type(exc).__name__, exc)
            return None

        return None

    @Tool(
        "body_state",
        description="查询麦麦此刻的身体状态：体征、体感、最近的活动与吃下去的东西",
        brief_description="查麦麦的身体",
        detailed_description=(
            "返回一段可以直接读的身体自述和一行紧凑摘要。"
            "适合在聊天里关心对方身体时调用；"
            "被问到「你吃了什么」「饿不饿」「身体怎么样」时都应该调它。"
        ),
        parameters=[
            ToolParameterInfo(name="detail", param_type=ToolParamType.BOOLEAN,
                              description="是否返回详尽版自述", required=False, default=True),
        ],
        # 2026-11-11：宿主 src/plugin_runtime/component_query.py 的
        # _get_tool_visibility() 对没写 visibility 的插件工具一律判 deferred，
        # deferred 不进模型常驻工具表，只能靠内置 tool_search 主动搜到。
        # 服务器日志里 body_eat 出现 0 次就是这么来的 —— 模型根本不知道有这工具。
        visibility="visible",
    )
    async def handle_body_state(self, detail: bool = True, **kwargs: Any) -> dict[str, Any]:
        """返回身体状态文本。"""

        del kwargs
        if not self._ready:
            return {"name": "body_state", "content": "身体还没启动。"}
        s = self._s
        text = describe(s, detail=bool(detail))
        return {"name": "body_state", "content": f"{text}\n（摘要：{brief(s)}）"}

    @Tool(
        "body_eat",
        description="让麦麦真的吃下或喝下一份东西（用户点了外卖、买了奶茶时用）",
        brief_description="给麦麦喂东西",
        detailed_description=(
            "把一次真实发生的吃喝落进身体模型：血糖、胃排空、水分都会真的变化。"
            "只在用户明确表示要给她吃东西、或她自己答应要吃时调用，不要凭聊天内容自行推测。"
            "kind 用 meal（一顿饭）/ snack（零食）/ drink（喝的）；"
            "items 可以填具体食物名，留空则按当前餐段随机配一份。"
        ),
        parameters=[
            ToolParameterInfo(name="kind", param_type=ToolParamType.STRING,
                              description="meal 一顿饭 / snack 零食 / drink 喝的",
                              required=True, default="snack"),
            ToolParameterInfo(name="items", param_type=ToolParamType.STRING,
                              description=("具体食物，逗号分隔。中英文都认："
                                           "「米饭,红烧肉,汤」或 Rice,Pork,Soup；"
                                           "留空则按当前餐段自动配一份"),
                              required=False, default=""),
            ToolParameterInfo(name="note", param_type=ToolParamType.STRING,
                              description="这次吃喝的由来，如「用户点的外卖」",
                              required=False, default=""),
        ],
        # 常驻可见：见 handle_body_state 上方同一条注释。
        visibility="visible",
    )
    async def handle_body_eat(self, kind: str = "snack", items: str = "",
                              note: str = "", **kwargs: Any) -> dict[str, Any]:
        """把一次吃喝落进身体。"""

        del kwargs
        if self.config.feeding.trust_private_only:
            # 当前宿主 Tool runner 不向 handler 提供可靠的私聊/群聊标记。
            # 无法证明来源为私聊时必须拒绝，避免配置开关形同虚设。
            return {"name": "body_eat", "content": "私聊喂食来源暂时无法验证，已拒绝本次喂食。"}
        if not self.config.feeding.enabled:
            return {"name": "body_eat", "content": "当前没有开启聊天喂食。"}
        if not self._ready or self._engine is None:
            return {"name": "body_eat", "content": "身体还没启动。"}

        feed_kind = str(kind or "snack").strip().lower()
        if feed_kind not in {"meal", "snack", "drink"}:
            return {
                "name": "body_eat",
                "content": "喂食类型无效，只支持 meal、snack 或 drink。",
            }

        keys = [k.strip() for k in str(items or "").replace("，", ",").split(",") if k.strip()]
        # 中英文都认：先按 key 精确匹配，认不出的交给 food.parse_menu 按中文
        # 别名解析（「米饭」-> Rice、「红烧肉」-> Pork）。模型更常写中文，
        # 旧实现只认英文 key，一句「点了黄焖鸡米饭」会直接报"食物库里没有"。
        resolved: list[str] = []
        unknown: list[str] = []
        for k in keys:
            if k in FOODS and FOODS[k].selectable:
                resolved.append(k)
                continue
            guess = parse_menu(k, limit=4)
            if guess:
                resolved.extend(g for g in guess if g not in resolved)
            else:
                unknown.append(k)
        # 认不出的（"红烧肉盖饭""番茄炒蛋"这种复合菜名），交给 utils 模型
        # 拆成基础食材。旧实现到这里直接报"食物库里没有"，模型在聊天里
        # 报一个菜名就整次喂食失败 —— 用户反馈"点了外卖没进进食记录"。
        # 只认出一样时也要拆：模型报「番茄炒蛋」，关键词只给 Egg，缺主食。
        # 但「香蕉」「牛奶」这类本身就是一样东西，不拆（拆了反而可能被模型改坏）。
        thin = len(resolved) == 1 and not is_plain_food(keys[0] if keys else "")
        if unknown or thin:
            merged: list[str] = list(resolved)
            # 认不出时按单个菜名拆；只认出一样时按整句拆，让模型补主语/主食。
            targets = unknown if unknown else ["、".join(keys)]
            for name in targets:
                got, src = await decompose_menu(name, limit=4, llm=self.ctx.llm)
                if src == "llm" and got:
                    for g in got:
                        if g not in merged:
                            merged.append(g)
                    self._get_logger().info(
                        "喂食菜名经 utils 拆解：%s -> %s", name, combo_summary(got)
                    )
                elif not merged:
                    # 一样都没认出来才整单拒；已经认出的部分不能作废。
                    return {
                        "name": "body_eat",
                        "content": (
                            f"食物库里没有「{name}」，模型也没拆出可用的食材。"
                            f"可以直接说基础食材，例如：米饭,猪肉,汤"
                        ),
                    }
            resolved = merged
        keys = resolved

        try:
            result = self._engine.feed(
                kind=feed_kind, items=keys or None, note=str(note or "")
            )
        except Exception as exc:  # noqa: BLE001
            return {"name": "body_eat", "content": f"没吃成：{type(exc).__name__}: {exc}"[:200]}

        if not result.get("ok"):
            return {"name": "body_eat", "content": str(result.get("reason") or "没吃成")}

        self._maybe_sample()
        # 回话给模型看的是中文菜名，不是内部 key（旧实现回 "Rice、Pork、Soup"，
        # 模型转述给用户时会显得像在对代码说话）。
        eaten = combo_summary(result.get("items") or []) or "、".join(result.get("items") or [])
        return {
            "name": "body_eat",
            "content": (
                f"吃下了：{eaten}。"
                f"现在血糖 {self._s.glucose:.1f} mmol/L，胃内容物 {self._s.gastric_volume:.0f} mL。"
            ),
        }

    @Command("body", description="查看麦麦当前的身体状态", pattern=r"^/body$")
    async def handle_body_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """聊天里用 /body 查身体：先出一张状态图，出不了图就退回文字。"""

        del kwargs
        if not self._ready:
            return False, "身体还没启动", True
        if await self._send_body_card(stream_id):
            return True, "已发送身体状态图", True
        await self.ctx.send.text(describe(self._s), stream_id)
        return True, "已发送身体状态", True

    async def _send_body_card(self, stream_id: str) -> bool:
        """把当前快照渲染成 PNG 并发出去。任何一步失败都返回 False，由调用方走文字。"""

        if not stream_id:
            return False
        try:
            html_text = build_html(
                self._s,
                cycle_enabled=bool(getattr(self.config.cycle, "enabled", False)),
            )
        except Exception as exc:  # 快照缺字段等构造期问题
            self.ctx.logger.warning("身体卡片 HTML 构建失败，退回文字: %s", exc)
            return False
        try:
            result = await self.ctx.render.html2png(
                html_text,
                selector="#card",
                viewport={"width": 780, "height": 900},
                device_scale_factor=2.0,
            )
        except Exception as exc:
            self.ctx.logger.warning("身体卡片渲染失败，退回文字: %s", exc)
            return False
        if not isinstance(result, dict):
            self.ctx.logger.warning("身体卡片渲染返回形态异常 %r，退回文字", type(result).__name__)
            return False
        if result.get("success") is False:
            self.ctx.logger.warning("身体卡片渲染被拒: %s，退回文字", result.get("error"))
            return False
        image_base64 = str(result.get("image_base64") or "").strip()
        if not image_base64:
            self.ctx.logger.warning("身体卡片渲染结果为空，退回文字")
            return False
        try:
            if await self.ctx.send.image(image_base64, stream_id):
                return True
        except Exception as exc:
            self.ctx.logger.warning("身体卡片发送失败，退回文字: %s", exc)
        return False

    @HomeCard(
        "lifebody_card",
        title="麦麦此刻的身体",
        description="实时体征与体感，点击进入「麦麦身体」工作区",
        content=[
            {"type": "markdown", "content": "身体正在后台以 10 秒一步的节奏持续运转。"},
            {"type": "actions", "actions": [{"label": "打开麦麦身体", "url": "/plugin-page?page=now"}]},
        ],
        width="medium",
        order=200,
    )
    async def home_card(self) -> None:
        """首页卡片占位，内容由 WebUI 拉取。"""

        return None


def create_plugin() -> LifeBodyPlugin:
    """创建插件实例。"""

    return LifeBodyPlugin()