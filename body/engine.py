"""生理引擎核心：心血管/呼吸/代谢/肾脏/内分泌/体温 + 行为闭环 + 事件发射。

设计约束：
  1. 所有速率常数按「每秒」标定，避免单位混淆（前一版最大缺陷）。
  2. 每个子系统用一阶惯性 (tau) 逼近目标值，保证数值稳定，不会发散。
  3. 行为闭环：口渴 -> 喝水、饥饿 -> 进食、膀胱充盈 -> 排尿。
     没有行为闭环的生理引擎会单调漂移，这是必须的。
  4. 字段范围限定在 LIFE.apk il2cpp 元数据能对上的生理量（见 state.py 注释）。
"""
import math
import random
import time
from datetime import date as _date, datetime as _datetime
from .state import BodyState, clamp
from .events import Event, EventBus
from . import food

H2S = 3600.0
TAU = {}  # 各子系统时间常数（秒）


def _tau(key, sec):
    TAU[key] = sec
    return sec


def approach(cur, target, dt, key):
    """一阶惯性逼近，显式欧拉 + 稳定限幅。

    2026-11-08 修：旧实现一律写 cur + (target-cur)*dt/TAU，当 dt >= TAU 时
    系数 >= 1，显式欧拉越过稳定界 —— dt=20 s 时 TAU["hr"]=12 系数 1.667，
    心率与血压会在几十步内发散到负数（实测 SBP=-517、DBP=-1725）。
    限幅到 1.0 后，dt 再大也只是「一步到位」，不会过冲。
    """
    return cur + (target - cur) * min(1.0, dt / TAU[key])


def approach_tau(cur, target, dt, tau_sec):
    """同上，但时间常数直接给秒数（用于未登记进 TAU 的局部量）。"""
    return cur + (target - cur) * min(1.0, dt / max(tau_sec, 1e-6))

# 时间常数统一登记，便于审阅与调参
_tau("hr", 12.0)
_tau("bp", 20.0)
_tau("rr", 30.0)
_tau("o2", 8.0)
_tau("glucose", 240.0)
_tau("atp", 60.0)
_tau("renal", 60.0)
_tau("raas", 90.0)
_tau("ang2", 60.0)
_tau("aldo", 300.0)
_tau("cortisol", 600.0)
_tau("acth", 300.0)
_tau("thyroid", 1800.0)
_tau("adh", 300.0)
_tau("temp", 300.0)
_tau("affect", 900.0)


class BodyEngine:
    """一具独立运行的赛博身体。"""

    def __init__(self, seed_state: BodyState = None, bus: EventBus = None,
                 enable_behaviour: bool = True,
                 rescue_enabled: bool = True,
                 rescue_threshold: float = 25.0,
                 rescue_critical_seconds: float = 600.0,
                 rescue_stay_hours: float = 6.0,
                 rescue_restore_health: float = 74.0,
                 enable_void: bool = True,
                 enable_stool: bool = True):
        self.s = seed_state or BodyState()
        self.bus = bus or EventBus()
        self.enable_behaviour = enable_behaviour
        # 排泄开关：有些人不想在聊天里看到「排尿」「排便」，
        # 或者只想保留排尿而不要排便记录。关掉时对应事件不再发出，
        # 但体液/残渣的守恒计算照常进行（否则会导致水平衡失真）。
        self.enable_void = enable_void
        self.enable_stool = enable_stool
        # 急救：濒死足够久就送医，住院期间被"支持治疗"拉回正常区间。
        # 本建模不设死亡（见 _health 的注释）。
        self.rescue_enabled = rescue_enabled
        self.rescue_threshold = rescue_threshold
        self.rescue_critical_seconds = rescue_critical_seconds
        self.rescue_stay_hours = rescue_stay_hours
        self.rescue_restore_health = rescue_restore_health
        # 急救"上膛"标志：出院后必须等健康回到阈值之上才允许下一次送医，
        # 否则阈值配得高于恢复值时会出现"出院即送医"的无限循环。
        self._rescue_armed = True
        # 时钟相位修正（小时）：只在 sync_clock 打开时由外部写入。
        # s.hour 是昼夜/体温基线/饭点判断的唯一来源，改相位即可整体对齐，
        # 不需要改 s.t —— 仿真时长与生理积分不能因为对齐而跳变。
        self.clock_offset = 0.0
        # 内置时钟：钟点直接跟系统墙钟走，不依赖麦生活。
        # 旧实现里 s.hour 只由仿真时长 t 推算，而 t 靠 ticker 按墙钟增量累加：
        # 掉帧会被丢弃（debt 清零）、离线超过 catch_up_max_hours 会被截断，
        # 于是钟点只能一路滞后，且没有任何机制把它拉回真实时间。
        # 打开后每次同步都把 clock_offset 改写成「系统钟点 - t/3600」，
        # 既不动 s.t（生理积分不能跳变），又保证 s.hour 等于真实时间。
        self.follow_wall_clock = False
        self._wall_clock_source = "sim"
        # 新身体从「早上八点」开始：t=0 若对应午夜零点，第一秒就落进 23-7
        # 的睡眠窗口、立刻入睡，WebUI 一打开就是"在睡觉"。
        # s.t 与 s.hour 解耦，靠时钟相位对齐，不伪造仿真时长。
        self.start_hour = 8.0
        self._fresh_body = seed_state is None
        if seed_state is None:
            self.clock_offset = self.start_hour
            self.s.hour = self.start_hour
        # 初始钟点必须跟着起始相位走。留着 0.0 会让第一步就判定"跨过了 07:30"，
        # 新身体刚睁眼就凭空吃下一顿早餐（重启连吃的老问题在初始化里也有一份）。
        self._prev_hour = float(getattr(self.s, "hour", 0.0))
        if seed_state is None:
            # 开局体感对齐「早上八点、早饭已消化、还没到饿」：
            # BodyState 的默认时间戳是相对 t=0 写死的（-6h/-2h/-3h），
            # 直接沿用会让新身体一出生就空腹 6 小时：10 分钟后饥饿冲到 53
            # （译作「饿了」），而不是应有的「不饿」。
            # 30 分钟对应开屏 hunger≈6（译作「不饿」），一小时自然爬到 17（「有点想吃」）。
            # 只挪参照点，不改任何生理量 —— 血糖/糖原/血容量/渗透压都还是满值。
            self.s.last_meal_t = -0.5 * H2S
            self.s.last_drink_t = -0.5 * H2S
            self.s.last_urine_t = -1.0 * H2S
            self.s.last_stool_t = -2.0 * H2S
        # ---- 与麦生活联动的注入点（全部由外部写入，引擎自己不发起调用）----
        # link_enabled=False 时这三个值恒为中性，引擎行为与未联动时逐位一致。
        self.link_enabled = False
        # link_sleep 是三态：-1.0 = 外部没有给出睡眠信息（用引擎自己的作息窗口），
        # 0.0..1.0 = 外部明确表态（0 清醒、1 深睡）。
        # 早前用 0.0 兼作"没有信息"，导致麦生活说"清醒"时被当成没说话，
        # 引擎于是退回自己的 23-7 窗口 —— 麦生活白天说没睡、身体照样在睡。
        self.link_sleep = -1.0
        self.link_activity = 0.0   # 0=静息，1=高强度活动
        self._link_meal = ""       # 待处理的餐段，由外部日程触发，处理完清空
        # 日程/聊天里点名的具体食物（food key 列表）。为空则按餐段菜谱池随便配一份。
        # 2026-11-11 加：日程写"点了一份生榨米粉配油条外卖"，旧实现只拿到
        # kind=meal 就按 lunch 池随机配餐，具体吃什么完全没进身体。
        self._link_meal_items = None
        self._meals_external = False  # True = 三餐交给麦生活日程，本插件不自己定饭点
        # 外部三餐的兜底：交给日程之后，若连续 link_meal_fallback_hours 小时
        # 没收到任何 meal 时段（日程里没排饭、或麦生活掉线），就恢复自己的饭点判断。
        # 没有这条兜底时实测 5 天只吃到 8 次加餐：糖原清零、血糖掉到 3.62、
        # 触发一次濒死送医 —— 联动一断就等于让身体绝食。
        self._last_ext_meal_t = 0.0
        self._ext_meal_epoch = 0.0
        # 一旦判定为断粮并自行顶上，就连续用自己饭点跑满一天，
        # 否则每顿只补一次、之后又要等 16 小时，实测 10 天只吃到 20 顿。
        self._fallback_until = 0.0
        # 16 小时：真人晚饭 18:40 到次日早餐 8:10 之间本就隔 13.5 小时，
        # 阈值低于这个间隔会在每天凌晨误判"断粮"、凭空多补一顿早餐。
        self.link_meal_fallback_hours = 16.0
        # 睡眠窗口（小时）。真实作息有社会性差异，这里取最常见的 23:00-07:00；
        # 外部 link_sleep 一旦给出非零值，就以外部为准（见 _sleep）。
        self.sleep_start_hour = 23.0
        self.sleep_end_hour = 7.0
        # 交感张力 = base + circ_amp*昼夜相位 + 应激项。三个系数 2026-11-08 扫描定标：
        # 要求日间静息 SBP 113-120、夜间 100-105、dipping 10-15%（真人参考带）。
        # 睡眠折扣只作为小幅叠加，过大时会与昼夜相位重复计账（实测叠加 0.50
        # 使夜间 DBP 跌到 51，低于真人下限）。
        self.sleep_symp_discount = 0.15   # 深睡时交感张力的下调比例
        self.symp_base = 0.40             # 交感张力昼夜基线
        self.symp_circ = 0.25             # 昼夜相位对交感的调制幅度
        self._last_alert = {}
        # 食单选择用固定种子，保证同一 seed 的 30 天回归可复现
        self.rng = random.Random(getattr(self.s, "_seed", 20261106))

    # ------------------------------------------------------------------
    # 内置时钟：把 s.hour 对齐到系统墙钟
    # ------------------------------------------------------------------
    def wall_hour(self) -> float:
        """当前系统钟点（小时，带小数）。"""
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    def sync_wall_clock(self, wall_hour: float | None = None) -> float:
        """把钟点相位对齐到系统墙钟，返回对齐后的 s.hour。

        只改 clock_offset（相位），不改 s.t（仿真时长与生理积分）。
        跳变超过 15 分钟时把 _prev_hour 一起重置：否则一次大跨度的
        "向后对齐"会被 _behaviour 当成"跨过了午饭"，凭空多吃一顿。
        """
        s = self.s
        target = self.wall_hour() if wall_hour is None else float(wall_hour)
        target %= 24.0
        cur = (s.t / H2S + self.clock_offset) % 24.0
        delta = ((target - cur + 12.0) % 24.0) - 12.0
        self.clock_offset = (self.clock_offset + delta) % 24.0
        s.hour = target
        if abs(delta) > 0.25:
            self._prev_hour = target
        return s.hour

    # ------------------------------------------------------------------
    # 主步进
    # ------------------------------------------------------------------
    def step(self, dt: float = 1.0):
        s = self.s
        s.t += dt
        s.hour = (s.t / H2S + self.clock_offset) % 24.0

        self._circadian()
        self._sleep(dt)
        self._menstrual(dt)
        self._exercise_tick(dt)
        self._update_habit(dt)
        self._behaviour(dt)
        self._autonomic(dt)
        self._cardio(dt)
        self._respiratory(dt)
        self._acidbase(dt)
        self._metabolic(dt)
        self._renal(dt)
        self._void()
        self._stool()
        self._endocrine(dt)
        self._thermo(dt)
        self._affect(dt)
        self._health(dt)
        self._rescue(dt)
        self._alerts()

    # ------------------------------------------------------------------
    # 昼夜节律
    # ------------------------------------------------------------------
    def _circadian(self):
        s = self.s
        s.circ = 0.5 - 0.5 * math.cos(2 * math.pi * (s.hour - 4.0) / 24.0)

    # ------------------------------------------------------------------
    # 睡眠（2026-11-08 新增）
    # ------------------------------------------------------------------
    def _in_sleep_window(self, hour: float) -> bool:
        a, b = self.sleep_start_hour, self.sleep_end_hour
        if a > b:                      # 跨零点窗口（默认 23 -> 7）
            return hour >= a or hour < b
        return a <= hour < b

    # 一个 NREM-REM 周期约 90 分钟。整夜前半段深睡占比高、后半段 REM 变多，
    # 这是健康成人睡眠结构的普遍规律（PSG 实测）。
    SLEEP_CYCLE_MIN = 90.0

    # 周期内各阶段的相对占比（浅睡 / 深睡 / REM）。周期序号越大，深睡越少、
    # REM 越多 —— 用两组系数在 cycle_profile() 里按周期线性插值。
    _CYCLE_PROFILE_EARLY = (0.55, 0.35, 0.10)   # 第一个周期：浅/深/REM
    _CYCLE_PROFILE_LATE = (0.60, 0.10, 0.30)    # 第 4+ 个周期：深睡几乎消失

    def cycle_profile(self, index: int) -> tuple:
        """按周期序号给出 (浅睡, 深睡, REM) 的时长占比。

        前 4 个周期线性插值，之后固定为 LATE，避免后半段深睡变负。
        """
        t = min(1.0, max(0.0, index / 3.0))
        a = self._CYCLE_PROFILE_EARLY
        b = self._CYCLE_PROFILE_LATE
        return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

    def _sleep(self, dt):
        """维护睡眠深度与四态睡眠周期。

        旧实现把睡眠完全外包给 link_sleep，而部署配置没有 [link] 段，
        这个值恒为 0 —— 于是 24 小时都按清醒代谢：夜间照常口渴喝水、
        膀胱到 300 mL 就起夜、交感张力不下调。真人 6-9 小时连续睡眠里
        基本不饮水、起夜 0-1 次，夜间心率血压体温同步下移。

        2026-11-14：在原有「窗口 + 深度」之上叠加四态分期
        （awake / light / deep / rem），一个周期 90 分钟，整夜 4~6 个。
        sleep_depth 不再直接由 target 驱动，而是由当前阶段派生，
        这样原有 6 处耦合（交感、体温、饮水、进食、憋尿、排便）无需改动。
        """
        s = self.s
        in_window = self._in_sleep_window(s.hour)

        # ---- 连续清醒计时 ----
        # 醒着就累加，睡着就归零。用于判断是否该强制入睡。
        if s.sleeping:
            s.awake_hours = 0.0
        else:
            s.awake_hours += dt / H2S

        # ---- 长时间不睡的即时欠债 ----
        # 2026-11-14 修：旧实现只在「睡眠窗口内清醒」时累积债，
        # 于是把睡眠窗口设成空（模拟一直不睡）时，债恒为 0 ——
        # 连续 19 小时不睡一点都不欠债，这显然不对。
        # 真实机制：只要超过一个正常清醒日（约 16 小时）就该开始欠债，
        # 与他几点睡、窗口怎么设无关。
        if not s.sleeping and s.awake_hours > 16.0:
            s.sleep_debt = clamp(s.sleep_debt + dt / H2S, 0.0, 48.0)

        # ---- 强制睡眠 / 微睡眠（2026-11-14 新增）----
        # 依据 [R6] Durmer & Dinges 2005：连续清醒越久，
        # "microsleeps / sleep attacks" 出现频率越高，且不可用意志抵抗。
        # 建模为两级：
        #   连续清醒 > force_sleep_after_hours（默认 20h）→ 强制入睡，
        #       且无视睡眠窗口（站着也能睡着）
        #   强制入睡持续 force_sleep_hours（默认 6h）后解除
        # 这不是"建议"，是生理性的——所以放在 want_sleep 之前直接覆盖。
        if not s.forced_sleep and s.awake_hours >= float(self.force_sleep_after_hours):
            s.forced_sleep = True
            s.forced_sleep_at = s.t
            s.microsleep_count += 1
            self.bus.emit(Event(s.t, "forced_sleep", "critical",
                                f"连续清醒 {s.awake_hours:.0f} 小时，撑不住睡过去了",
                                {"awake_h": round(s.awake_hours, 1),
                                 "count": s.microsleep_count}))
        elif s.forced_sleep and s.sleeping and s.awake_hours <= 0.0:
            # awake_hours 在睡着时归零；用一个单独计时判断强制睡眠是否结束
            pass

        if s.forced_sleep:
            elapsed_sleep = s.t - float(getattr(s, "forced_sleep_at", 0.0) or s.t)
            if elapsed_sleep >= float(self.force_sleep_hours) * H2S:
                s.forced_sleep = False
                self.bus.emit(Event(s.t, "forced_sleep_end", "info",
                                    "补够了觉，终于自然醒来", {}))

        # ---- 是否应该睡（外部联动优先）----
        want_sleep = 1.0 if in_window else 0.0
        if s.forced_sleep:
            # 强制睡眠优先级高于一切：不看在不在窗口、不看联动
            want_sleep = 1.0
        elif self.link_enabled:
            if self.link_sleep >= 0.0:
                # 外部明确表态就完全听它的：说清醒（0.0）就是清醒，
                # 不要因为"没到窗口"或"窗口到了"再改回去。
                want_sleep = min(1.0, self.link_sleep)
            elif self.link_activity > 0.6:
                want_sleep = 0.0

        was_sleeping = s.sleeping

        # ---- 生理性唤醒：渴醒 / 憋尿醒 ----
        # 「睡着的她不会起夜」是旧实现刻意做的简化，但真实人夜里会因为
        # 极度口渴或膀胱过满而醒。这里把这两个事件接进唤醒计数，
        # 同时把睡眠打断成「清醒一小段，再重新从浅睡入睡」。
        if s.sleeping and not s.wake_pending:
            if s.thirst > 88.0:
                self._wake_up("thirst", "渴醒")
            elif s.bladder > 550.0:
                self._wake_up("bladder", "憋尿醒")

        # ---- 唤醒后等待重新入睡 ----
        # 被叫醒后要连续安静一段时间才重新睡回去，期间再来消息重新计时。
        # 注意：唤醒是即时生效的（sleeping 立刻置 False），否则 _advance_stage
        # 会在同一拍把深度推回 0.55 以上，导致"刚被叫醒又睡回去"。
        if s.wake_pending:
            hold = max(0.0, self.wake_hold_minutes) * 60.0
            if s.t - s.wake_since >= hold:
                # 安静够了，允许重新入睡。清除标记，走正常入睡流程。
                s.wake_pending = False
                # 重新入睡从浅睡周期的起点开始
                s.stage_index = 0
                s.stage_elapsed = 0.0
                s.cycle_pos = 0.0
            else:
                # 仍然清醒：强制保持清醒态，累计清醒时长
                s.sleeping = False
                s.sleep_stage = "awake"
                s.sleep_depth = approach_tau(s.sleep_depth, 0.0, dt, 60.0)
                s.dur_awake += dt
                return

        # ---- 入睡 / 醒来：带一段过渡（不抖动）----
        # 入睡时间常数受核心体温调制：真实机制是核心体温必须下降约 0.3℃ 才
        # 容易入睡，黄体期基础体温偏高会推迟入睡（0.3~0.5℃ 约推迟 20~40 分钟）。
        # 基准 300 秒（约 5 分钟），体温每高 0.1℃ 增加 60 秒，上限 1200 秒。
        onset_tau = 300.0 + max(0.0, (s.temp - 36.8)) * 600.0
        onset_tau = min(1200.0, onset_tau)
        if want_sleep > 0.5 and not was_sleeping:
            # 入睡过程：一阶趋近，到 0.5 判定睡着
            s.sleep_depth = approach_tau(s.sleep_depth, 1.0, dt, onset_tau)
            if s.sleep_depth > 0.5:
                resumed = s.sleep_onset_t > 0.0 and s.wake_count > 0
                s.sleeping = True
                # 重新入睡一律从浅睡开始；首次入睡同样从浅睡
                s.stage_index = 0
                s.stage_elapsed = 0.0
                s.cycle_pos = 0.0
                s.sleep_stage = "light"
                if not resumed:
                    # 全新的一个夜晚：清零累计
                    s.dur_light = s.dur_deep = s.dur_rem = s.dur_awake = 0.0
                    s.wake_count = 0
                    s.wake_by_message = s.wake_by_thirst = s.wake_by_bladder = 0
                    s.sleep_onset_t = s.t
                s.last_sleep_t = s.last_sleep_t or s.t
                self.bus.emit(Event(s.t, "sleep_start", "info",
                                    "重新入睡" if resumed else "睡着",
                                    {"stage": "light"}))
        elif want_sleep <= 0.5 and was_sleeping:
            # 醒来过程
            s.sleep_depth = approach_tau(s.sleep_depth, 0.0, dt, 300.0)
            if s.sleep_depth <= 0.5:
                s.sleeping = False
                s.sleep_stage = "awake"
                self._settle_sleep()
                s.sleep_onset_t = 0.0
        else:
            # 保持当前状态，按阶段推进深度
            if s.sleeping:
                self._advance_stage(dt)
            else:
                s.sleep_depth = approach_tau(s.sleep_depth, 0.0, dt, 300.0)

        # ---- 睡眠债累积 ----
        # 2026-11-14 重写。旧实现只在「睡眠窗口内且清醒」时累积，
        # 于是「每天 3 点睡 5 点起」这种情况债几乎不涨（20 天才 1 小时）——
        # 因为绝大部分时间根本不在窗口内。这与现实不符：一个每天只睡 2 小时
        # 的人，债务是持续累积的，跟他几点睡无关。
        #
        # 新口径：以「每晚所需睡眠」为基准，睡得不够就按缺口累积。
        #   每晚基准 sleep_need_h 小时（默认 7.5）
        #   每个 24 小时周期结束时结算：缺口 = max(0, 基准 - 实际睡)
        # 这样无论几点睡、睡多久，只要总量不够就会持续欠债。
        #
        # 同时保留「窗口内清醒」的即时累积，用于捕捉「该睡不睡」的短期债。
        if not s.sleeping and in_window:
            s.sleep_debt = clamp(s.sleep_debt + dt / H2S, 0.0, 48.0)

    # ------------------------------------------------------------------
    # 日程驱动的运动（2026-11-14）
    # ------------------------------------------------------------------
    exercise_enabled: bool = True
    exercise_max_minutes: float = 45.0
    exercise_interval_minutes: float = 15.0
    # 运动所需的最低有效精力（含睡眠债扣分后）。默认 55：
    # 要"精神不错"才去跑，而不是"还没死"就去跑。
    exercise_min_energy: float = 55.0
    # 昨晚睡眠低于这个小时数就不运动（熬夜次日不该晨跑）
    exercise_min_sleep_hours: float = 6.0

    # 不适合运动的身体/环境条件。返回拒绝原因，空串表示可以运动。
    def exercise_block_reason(self) -> str:
        """判断现在能不能运动。麦生活的日程是 LLM 编的，
        外面暴晒/刮风/正在痛经时硬跑没道理，所以这里做一次否决。"""

        s = self.s
        if s.hospital_until > s.t:
            return "正在住院"
        if s.health < 45.0:
            return f"身体太虚（健康 {s.health:.0f}）"
        if s.pain > 35.0:
            return f"正在疼痛（{s.pain:.0f}/100），不适合运动"
        # DOMS 期间肌肉微损伤未修复，继续运动会加重（[R3] Cheung 2003 明确
        # 建议 DOMS 后 1-2 天降低强度）。这里用 30 分做门槛。
        if float(getattr(s, "doms", 0.0) or 0.0) > 30.0:
            return f"肌肉还在酸痛（{s.doms:.0f}/100），需要恢复"
        if s.sleeping:
            return "正在睡觉"
        # 环境：只在室外时才拦。室内有空调暖气，外面天气不构成理由。
        if not bool(getattr(s, "env_indoor", True)):
            load = clamp(float(getattr(s, "env_load", 0.0) or 0.0), 0.0, 1.0)
            if load >= 0.70:
                feels = float(getattr(s, "env_feels_c", 22.0) or 22.0)
                return f"室外体感 {feels:.0f}℃ 太极端，不适合出门运动"
            wind = float(getattr(s, "env_wind_ms", 0.0) or 0.0)
            if wind >= 10.0:
                return f"室外风太大（{wind:.0f} m/s）"
        if s.thirst > 85.0:
            return "严重缺水"

        # ---- 精力门槛（2026-11-14 调高）----
        # 旧阈值 25 太低：那已经接近"走两步就喘"的水平，根本跑不动。
        # 真实运动需要较好的状态，尤其熬夜后第二天不该去跑步。
        # 门槛用「有效精力」而不是裸 energy：熬夜欠的睡眠债要折算进扣分。
        eff_energy = float(s.energy) - self._sleep_debt_penalty()
        need = max(10.0, float(self.exercise_min_energy))
        if eff_energy < need:
            if self._sleep_debt_penalty() > 0.0:
                return (f"精力不足（{eff_energy:.0f}/100，含睡眠债扣 "
                        f"{self._sleep_debt_penalty():.0f}），不适合运动")
            return f"精力不足（{eff_energy:.0f}/100，需要 {need:.0f} 以上）"

        # 睡得太少也不该运动：连续熬夜后即使 energy 显示尚可，
        # 身体实际恢复不足。以昨晚睡眠时长做二次确认。
        last_sleep_h = float(getattr(s, "last_sleep_hours", 0.0) or 0.0)
        if last_sleep_h > 0.0 and last_sleep_h < float(self.exercise_min_sleep_hours):
            return f"昨晚只睡了 {last_sleep_h:.1f} 小时，不适合运动"
        return ""

    def _sleep_debt_penalty(self) -> float:
        """睡眠债折算成精力扣分。

        睡眠债 1 小时 ≈ 扣 4 点有效精力，上限扣 25 点。
        依据：连续两晚少睡 2 小时，主观精力明显下降是常识性结论，
        具体系数是估算值（无文献）。
        """

        debt = max(0.0, float(getattr(self.s, "sleep_debt", 0.0) or 0.0))
        return min(25.0, debt * 4.0)

    def _update_habit(self, dt):
        """运动习惯随时间衰减，运动时提升。

        重复运动效应 [R5]：一次运动后即产生保护，但保护会随时间消退
        （文献未给精确消退速率，这里按 14 天衰减完估算）。
        """

        s = self.s
        # 每天衰减 1/14
        decay = dt / (14.0 * 24.0 * H2S)
        s.exercise_habit = clamp(s.exercise_habit - decay, 0.0, 1.0)
        # 运动中持续提升
        if s.exercising:
            s.exercise_habit = clamp(s.exercise_habit + dt / (6.0 * H2S), 0.0, 1.0)

    def _fatigue_factor(self) -> float:
        """当前疲劳度 0~1。越高表示越撑不住。

        三个来源合成（各自归一到 0~1 后加权）：
          1. 本段运动已进行时间 —— 跑了 40 分钟当然比跑了 10 分钟累
          2. 精力消耗 —— energy 越低越累
          3. 乳酸累积 —— 6 mmol/L 以上开始明显影响发力
        """

        s = self.s
        elapsed_min = max(0.0, (s.t - float(getattr(s, "exercise_start_t", s.t)))) / 60.0
        # 时间项：以 60 分钟为满，但前 20 分钟增长慢（有氧系统还没吃紧）
        t_term = min(1.0, (elapsed_min / 60.0) ** 0.8)

        # 精力项：energy 从 80 掉到 20 视为完全疲劳
        e_now = float(getattr(s, "energy", 80.0) or 80.0)
        e_term = clamp((80.0 - e_now) / 60.0, 0.0, 1.0)

        # 乳酸项：1~8 mmol/L 线性映射（8 已是相当高的血乳酸）
        lac = float(getattr(s, "lactate", 1.0) or 1.0)
        l_term = clamp((lac - 2.0) / 6.0, 0.0, 1.0)

        return clamp(0.45 * t_term + 0.30 * e_term + 0.25 * l_term, 0.0, 1.0)

    def _jitter(self, base_sec: float, ratio: float) -> float:
        """在 base 的 ±ratio 范围内随机取一个时长（秒）。

        用引擎自己的 rng，好处是同一个 seed 下仿真可复现，便于回归测试。
        """

        base = max(0.0, float(base_sec))
        if base <= 0.0:
            return 180.0
        lo = base * (1.0 - max(0.0, min(0.9, ratio)))
        hi = base * (1.0 + max(0.0, min(0.9, ratio)))
        try:
            return float(self.rng.uniform(lo, hi))
        except Exception:
            import random as _r
            return float(_r.uniform(lo, hi))

    def request_exercise(self, level: str, duration_min: float = 30.0) -> bool:
        """外部（日程）请求运动。返回是否接受。

        接受后不是立刻开始跑：由 _exercise_tick 按间歇节奏驱动，
        跑一段歇一段，直到 duration_min 到点或身体条件变差。
        """

        s = self.s
        if not self.exercise_enabled or not level:
            return False
        reason = self.exercise_block_reason()
        if reason:
            s.exercise_refused = reason
            return False
        cap = max(5.0, float(self.exercise_max_minutes))
        minutes = min(max(5.0, float(duration_min)), cap)
        s.exercise_intent = level
        s.exercise_until = s.t + minutes * 60.0
        # 强度决定间歇：高强度跑短歇长，中等反过来
        base = max(5.0, float(self.exercise_interval_minutes)) * 60.0
        if level == "high":
            s.exercise_on_phase = base
            s.exercise_off_phase = base * 0.4
        else:
            s.exercise_on_phase = base * 1.2
            s.exercise_off_phase = base * 0.3
        # 每次循环的时长不是定值：真实跑步每一段的配速、休息长短都不同。
        # 这里只给基准值，具体时长在 _exercise_tick 每次换相位时重新抽。
        s.exercise_cycle_t = 0.0
        s.exercising = True
        s.exercise_refused = ""
        s.exercise_start_t = s.t
        s.last_exercise_t = s.t
        s._next_phase_change = s.t + s.exercise_on_phase
        self.bus.emit(Event(s.t, "exercise_start", "info",
                            f"开始运动（{level}）",
                            {"level": level, "minutes": round(minutes, 1)}))
        return True

    def _trigger_doms(self) -> None:
        """运动结束后按运动量与习惯触发 DOMS。

        依据 [R5] 重复运动效应：首次做某类运动酸痛最重，之后显著减轻。
        这里用 exercise_habit 表示"最近是否常运动"：
          habit = 0（很久没动）→ 全额 DOMS
          habit = 1（天天动）   → 只剩 30%
        """

        s = self.s
        # 运动量：以"跑了多少分钟 × 强度"估算
        elapsed_min = max(0.0, (s.t - float(getattr(s, "exercise_start_t", s.t)))) / 60.0
        if elapsed_min < 8.0:
            return          # 太短，不构成 DOMS 刺激
        level = str(s.exercise_intent or "mid")
        intensity = 1.0 if level == "high" else 0.65
        # 基础峰值：30 分钟高强度约 55 分酸痛
        base = clamp(elapsed_min / 30.0 * 55.0 * intensity, 0.0, 85.0)
        # 重复运动效应：习惯越好，酸痛越轻（文献 SMD 0.51-1.23）
        habit = clamp(float(getattr(s, "exercise_habit", 0.0) or 0.0), 0.0, 1.0)
        peak = base * (1.0 - 0.70 * habit)
        if peak < 5.0:
            return
        # 峰值出现在运动后 48h
        s.doms_from_exercise = peak
        s.doms_peak_at = s.t + 48.0 * H2S
        self.bus.emit(Event(s.t, "doms", "info",
                            "运动后肌肉开始酸痛（48 小时后达峰）",
                            {"peak": round(peak, 1), "habit": round(habit, 2)}))

    def _exercise_tick(self, dt):
        """推进间歇式运动：跑一段、歇一段，到点结束。"""

        s = self.s
        if not s.exercise_intent:
            return
        if s.t >= s.exercise_until:
            self._trigger_doms()
            s.exercising = False
            s.exercise_intent = ""
            s.exercise_until = 0.0
            s.exercise_cycle_t = 0.0
            s.last_exercise_t = s.t
            self.bus.emit(Event(s.t, "exercise_end", "info", "运动结束", {}))
            return

        # 运动过程中身体变差（太渴/太累/开始痛）就提前收工
        if s.exercising and (s.thirst > 90.0 or s.energy < 12.0 or s.pain > 60.0):
            s.exercising = False
            s.exercise_refused = "身体撑不住，提前结束"
            self.bus.emit(Event(s.t, "exercise_end", "info", "身体撑不住，提前结束", {}))

        s.exercise_cycle_t += dt

        # ---- 随机化间歇 + 疲劳调制 ----
        # 旧实现用固定 on/off 相位循环，于是每段跑步都是精确的 15 分钟、
        # 每次休息都是精确的 6 分钟 —— 太机械。真实跑步的段长与配速本来就
        # 在波动（身体状态、路面、心情），所以每次换相位时重新抽一次时长。
        #
        # 2026-11-14 加入疲劳：越到后面跑得越短、歇得越长。疲劳度由三个量
        # 合成 —— 本段运动已进行时间、精力消耗、乳酸累积。
        # 这符合真实感受：跑了 40 分钟后，第 5 段撑不到第 1 段那么久。
        next_change = float(getattr(s, "_next_phase_change", 0.0) or 0.0)
        fatigue = self._fatigue_factor()

        # 疲劳到顶就直接收工，不要拖出「跑 1 分钟」这种碎片段。
        # 真实感受也是这样：真的跑不动了就结束，不会勉强挪两下。
        if fatigue >= 0.85:
            self._trigger_doms()
            s.exercising = False
            s.exercise_intent = ""
            s.exercise_until = 0.0
            s.exercise_cycle_t = 0.0
            s.last_exercise_t = s.t
            s.exercise_refused = ""
            self.bus.emit(Event(s.t, "exercise_end", "info",
                                "太累了，结束运动", {"fatigue": round(fatigue, 2)}))
            return

        if next_change > 0.0 and s.t >= next_change:
            if s.exercising:
                # 这次跑够了，进入休息：疲劳越高歇得越久
                s.exercising = False
                base = s.exercise_off_phase * (1.0 + 0.80 * fatigue)
                dur = self._jitter(base, 0.35)
            else:
                # 休息够了开始下一段：疲劳越高这段越短
                base = s.exercise_on_phase * max(0.40, 1.0 - 0.60 * fatigue)
                dur = self._jitter(base, 0.30)
                # 边界检查：如果这段跑不完就被窗口截断，就别开了。
                # 旧实现在剩 90 秒时还开一段，产出「跑 1 分 / 歇 0 分」的碎片。
                remain = s.exercise_until - s.t
                if dur > remain * 0.55:
                    self._trigger_doms()
                    s.exercising = False
                    s.exercise_intent = ""
                    s.exercise_until = 0.0
                    s.exercise_cycle_t = 0.0
                    s.last_exercise_t = s.t
                    self.bus.emit(Event(s.t, "exercise_end", "info",
                                        "时间差不多了，结束运动",
                                        {"fatigue": round(fatigue, 2)}))
                    return
                s.exercising = True
                self.bus.emit(Event(s.t, "exercise_rep", "debug",
                                    "开始新一段",
                                    {"phase_s": round(dur),
                                     "fatigue": round(fatigue, 2)}))
            s.last_exercise_t = s.t
            s._next_phase_change = s.t + max(180.0, dur)
        elif next_change <= 0.0:
            # 兼容没有初始化的情况
            s.exercising = True
            s._next_phase_change = s.t + self._jitter(s.exercise_on_phase, 0.30)

    # ------------------------------------------------------------------
    # 睡眠分期
    # ------------------------------------------------------------------
    # 各阶段对「睡眠深度」的贡献，用于驱动原有生理耦合：
    #   浅睡 0.55（交感仍部分活跃，易被唤醒）
    #   深睡 1.00（副交感主导，心率血压体温最低）
    #   REM  0.35（脑活跃但肌张力消失，自主神经不稳定）
    STAGE_DEPTH = {"awake": 0.0, "light": 0.55, "deep": 1.00, "rem": 0.35}

    # ------------------------------------------------------------------
    # 觉醒
    # ------------------------------------------------------------------
    # 月经周期总开关与参数，由插件从配置注入。默认关闭。
    cycle_enabled: bool = False
    cycle_length_days: float = 28.0
    cycle_pain_enabled: bool = True
    # 现实日历锚点（date 或 None）。由配置 cycle.last_period_date 解析后注入。
    # 为 None 时沿用旧行为：从启用那一刻开始累加仿真天数，零点是身体自己的纪年，
    # 与现实日期无关。有锚点时改为「今天 - 上次月经首日」，直接对实现实日历。
    cycle_anchor_date = None
    # 改锚点后跳过下一次回绕检测：否则「昨天填上月、今天改成上周」会让
    # day 突然倒退，被误判成跨过周期末，凭空发一条「新一轮开始」。
    _cycle_skip_wrap_once = False

    # 每晚所需睡眠小时数，用于结算睡眠债缺口。默认 7.5。
    sleep_need_hours: float = 7.5
    # 连续清醒超过这个小时数就强制入睡（微睡眠）。默认 20 小时。
    # 依据 [R6]：微睡眠随睡眠剥夺加剧出现。10-20 小时是常见区间，
    # 取 20 是保守值（现实中 24 小时不睡已明显出现 microsleep）。
    force_sleep_after_hours: float = 20.0
    # 强制入睡持续多久才解除。默认 6 小时（一次补偿性长睡）。
    force_sleep_hours: float = 6.0
    # 强制睡眠期间能否被消息叫醒。默认 True（现实里大声喊是能叫醒的），
    # 但醒来状态很差。设为 False 则任何人都叫不醒，只能等自然醒。
    force_sleep_wakeable: bool = True

    # 唤醒等待时长（分钟），由插件从配置注入。默认 10 分钟。
    wake_hold_minutes: float = 10.0
    # 是否允许被消息唤醒。插件从配置注入；联动开启时会被置 False。
    wake_on_message: bool = True

    def _wake_up(self, reason: str, label: str) -> None:
        """记一次唤醒并按来源计数。

        唤醒后不清零整夜累计 —— 这仍然是同一夜，只是中间醒了一次。
        重新入睡时 stage_index 归零，从浅睡重新开始一个周期。
        """
        s = self.s
        s.wake_count += 1
        if reason == "message":
            s.wake_by_message += 1
        elif reason == "thirst":
            s.wake_by_thirst += 1
        elif reason == "bladder":
            s.wake_by_bladder += 1
        s.wake_since = s.t
        s.wake_pending = True
        s.last_wake_reason = label
        self.bus.emit(Event(s.t, "wake", "info", f"夜间唤醒：{label}",
                            {"reason": reason, "count": s.wake_count}))

    def wake_by_message(self) -> None:
        """外部（插件消息钩子）通知：来消息了，叫她一声。

        是否真的唤醒由 wake_on_message 决定；联动开启时插件不会调用本方法。
        """
        s = self.s
        if not self.wake_on_message:
            return
        if not s.sleeping:
            return

        # ---- 强制睡眠（微睡眠）的可叫醒性 ----
        # 连续清醒 20 小时后睡过去，那不是普通小睡，是身体撑不住。
        # 但现实里大声喊、推搡还是能叫醒的 —— 只是醒来后状态很差。
        # 由 force_sleep_wakeable 控制（默认开）。
        if s.forced_sleep:
            if not bool(getattr(self, "force_sleep_wakeable", True)):
                # 开关关闭：叫不醒，消息只能干等
                s.last_wake_reason = "强制睡眠中，叫不醒"
                return
            # 可叫醒：结束强制睡眠状态，并留一次很重的唤醒记录
            s.forced_sleep = False
            s.forced_sleep_at = 0.0
            self._wake_up("message", "被硬叫醒（强撑着）")
            # 被叫醒后状态很差：强制拉低精力、抬高压力的感受
            s.energy = clamp(s.energy - 12.0, 10.0, 100.0)
            self.bus.emit(Event(s.t, "forced_wake", "warn",
                                "被强行叫醒，脑子还很糊",
                                {"awake_h": round(s.awake_hours, 1)}))
            return

        self._wake_up("message", "来消息")

    def _advance_stage(self, dt):
        """推进四态分期。一个周期 90 分钟，按 cycle_profile 分配三段时长。"""
        s = self.s
        s.stage_elapsed += dt

        cycle_sec = self.SLEEP_CYCLE_MIN * 60.0
        light_frac, deep_frac, rem_frac = self.cycle_profile(s.stage_index)

        # 月经周期对睡眠结构的影响（真实且显著）：
        #   黄体后期（PMS 期）—— 核心体温升高 0.3~0.5℃ 延迟入睡、深睡减少、
        #   夜间觉醒增多（真实研究中深睡可减少 20~30%）。
        #   行经期 —— 前列腺素致痛与不适，入睡更浅。
        # 这里把深睡占比按 pms/pain 压缩，压缩部分转给浅睡；REM 基本不变
        # （REM 受激素影响小于 NREM 深睡，与文献一致）。
        penalty = 0.0
        if s.pms_load > 0.0:
            penalty += 0.30 * clamp(s.pms_load, 0.0, 1.0)   # 最多压 30% 深睡
        if s.pain > 20.0:
            penalty += 0.15 * clamp((s.pain - 20.0) / 60.0, 0.0, 1.0)
        penalty = clamp(penalty, 0.0, 0.45)
        if penalty > 0.0:
            moved = deep_frac * penalty
            deep_frac -= moved
            light_frac += moved
        # 每个周期内部固定顺序：浅睡 → 深睡 → 浅睡 → REM
        # （两次浅睡合计 light_frac，中间插一段深睡，最后进 REM）
        seg_light_a = cycle_sec * light_frac * 0.5
        seg_deep = cycle_sec * deep_frac
        seg_light_b = cycle_sec * (light_frac * 0.5 + rem_frac * 0.0)
        seg_rem = cycle_sec * rem_frac

        # 周期内位置（0..1），用于外部展示
        s.cycle_pos = min(1.0, s.stage_elapsed / cycle_sec)

        # 判定当前阶段应处于哪一段
        if s.stage_elapsed < seg_light_a:
            new_stage = "light"
        elif s.stage_elapsed < seg_light_a + seg_deep:
            new_stage = "deep"
        elif s.stage_elapsed < seg_light_a + seg_deep + seg_light_b:
            new_stage = "light"
        else:
            new_stage = "rem"

        # 累计时长
        if new_stage == "light":
            s.dur_light += dt
        elif new_stage == "deep":
            s.dur_deep += dt
        else:
            s.dur_rem += dt

        if new_stage != s.sleep_stage:
            self.bus.emit(Event(s.t, "sleep_stage", "debug", "睡眠分期",
                                {"from": s.sleep_stage, "to": new_stage,
                                 "cycle": s.stage_index}))
            s.sleep_stage = new_stage

        # 深度跟随阶段，但不硬切（30 秒一阶过渡，避免心率阶跃）
        s.sleep_depth = approach_tau(s.sleep_depth,
                                     self.STAGE_DEPTH.get(new_stage, 0.0),
                                     dt, 30.0)

        # 周期结束 → 下一周期
        if s.stage_elapsed >= cycle_sec:
            s.stage_index += 1
            s.stage_elapsed = 0.0
            s.cycle_pos = 0.0

    def _settle_sleep(self):
        """醒来结算：算睡眠质量，按质量还睡眠债。"""
        s = self.s
        total = s.dur_light + s.dur_deep + s.dur_rem
        if total <= 0.0:
            return

        total_h = total / H2S
        s.last_sleep_hours = total_h   # 供运动门槛判断"昨晚睡够没有"
        deep_r = s.dur_deep / total
        rem_r = s.dur_rem / total
        awake_h = s.dur_awake / H2S

        # 质量评分（0~100）：
        #   时长分（满分 40）—— 7~9 h 最佳，少于 6 h 明显扣分
        #   深睡分（满分 30）—— 健康区间 20~25%，偏离线性扣分
        #   REM 分（满分 20）—— 健康区间 20~25%
        #   连续性（满分 10）—— 中途清醒越少越高
        if total_h >= 7.0:
            dur_score = 40.0
        elif total_h >= 6.0:
            dur_score = 40.0 * (total_h - 5.0) / 2.0
        else:
            dur_score = max(0.0, 40.0 * (total_h - 4.0) / 2.0)

        deep_score = 30.0 * max(0.0, 1.0 - abs(deep_r - 0.225) / 0.225)
        rem_score = 20.0 * max(0.0, 1.0 - abs(rem_r - 0.225) / 0.225)
        cont_score = 10.0 * max(0.0, 1.0 - awake_h * 2.0)

        s.sleep_quality = clamp(dur_score + deep_score + rem_score + cont_score,
                                0.0, 100.0)

        # 还债：以睡眠质量为核心，不是只看时长。
        # 2026-11-14 修：旧实现 eff = min(1, quality/80) 上限封顶，
        # 于是质量 95 和质量 80 的还债速度完全一样 —— 质量实际上没起作用。
        # 改为「恢复效率」：把整夜睡出来的有效恢复折算成"等效睡眠小时"。
        #
        # 恢复效率 = 时长 × 质量系数
        #   质量系数 = 0.4 + 0.6 × (quality/100)
        #   即：质量 100 → 系数 1.0（一小时顶一小时）
        #       质量 60  → 系数 0.76
        #       质量 30  → 系数 0.58
        # 这样"睡 8 小时但整夜浅睡（质量 40）"只有 4.5 小时等效，还不清债。
        # 先按「昨晚睡够没有」结算缺口 —— 这是睡眠债的主体。
        # 基准取每晚所需时长（默认 7.5h）。睡得少就欠，睡够不欠。
        # 注意：这里用原始时长而非等效时长，因为「睡了但质量差」由还债环节的
        # q_factor 处理，两者不重复扣。
        need = max(4.0, float(getattr(self, "sleep_need_hours", 7.5)))
        shortfall = max(0.0, need - total_h)
        if shortfall > 0.0:
            s.sleep_debt = clamp(s.sleep_debt + shortfall, 0.0, 48.0)

        # 再按质量还债：等效睡眠小时超过 1 小时的部分才抵消债务。
        q_factor = 0.4 + 0.6 * clamp(s.sleep_quality / 100.0, 0.0, 1.0)
        effective_h = total_h * q_factor
        if total_h >= 3.0:
            # 前 1 小时视为入睡过渡，不计入还债
            s.sleep_debt = clamp(s.sleep_debt - max(0.0, effective_h - 1.0) * 0.9,
                                 0.0, 48.0)

        self.bus.emit(Event(s.t, "sleep_end", "info", "醒来", {
            "slept_h": round(total_h, 2),
            "light_h": round(s.dur_light / H2S, 2),
            "deep_h": round(s.dur_deep / H2S, 2),
            "rem_h": round(s.dur_rem / H2S, 2),
            "quality": round(s.sleep_quality, 1),
            "cycles": s.stage_index + 1,
        }))

    # ------------------------------------------------------------------
    # 行为闭环 —— 修复「口渴只升不降」的关键
    # ------------------------------------------------------------------
    def _behaviour(self, dt):
        s = self.s
        if not self.enable_behaviour:
            return
        if s.hospital_until > s.t:
            # 住院期间由医护供能补水，她自己不再主动吃喝
            return

        # --- 口渴 -> 喝水：加的是水，不是血容量数字。
        # 早前版本只抬 blood_vol 而渗透压每步被公式重算，喝水等于没喝，
        # 导致口渴恒在阈值上方、每 46 秒喝一次。改为对守恒量加水后自然解渴。
        # 20 分钟不应期：真实饮水是离散事件，不是每秒 250 mL
        # 睡眠中不饮水：真人深睡时口渴感知被抑制，6-9 小时里基本不喝水。
        # 只有极度缺水（thirst > 88）才会渴醒 —— 这正是夜间被渴醒的真实场景。
        # 渴阈 42（旧 55）：真人血浆渗透压升 2% 就有渴感，对应评分约 40，
        # 而不是等到 55（≈渗透压升 4.6 mmol/L）才喝。阈值定在 55 时实测
        # 第一天整天一次水都没喝 —— 渗压正常的人根本到不了 55。
        # 睡眠中不饮水：真人深睡时口渴感知被抑制，6-9 小时里基本不喝水。
        # 只有极度缺水（thirst > 88）才会渴醒 —— 这正是夜间被渴醒的真实场景。
        if s.thirst > 42.0 and (s.t - s.last_drink_t) > 1200.0:
            if (not s.sleeping) or s.thirst > 88.0:
                self._drink()

        # --- 外部日程驱动的进餐 ---
        # 麦生活日程里 kind=meal 的时段开始时，由外部把餐段塞进 _link_meal。
        # 这边只负责"吃下"，不判断时间——判断时间的是麦生活的日程表。
        # 外部餐段：收到就吃掉（睡着的除外），并记下最近一次收到的时间。
        if self._link_meal:
            slot = self._link_meal
            keys = self._link_meal_items
            self._link_meal = ""
            self._link_meal_items = None
            self._last_ext_meal_t = s.t
            self._fallback_until = 0.0   # 外部日程恢复，撤销兜底
            # 睡着的人不会爬起来吃饭：麦生活的日程若与睡眠阶段冲突，
            # 以"她在睡"为准，直接吃掉这次触发（不排队，睡醒不补吃）。
            if s.gastric_volume < 900.0 and not s.sleeping:
                if keys:
                    # 日程/聊天点名了具体食物，就吃那份，不再随机配餐
                    self._eat_keys(slot, list(keys), note="日程点的")
                else:
                    self._eat(slot)

        # --- 饥饿 -> 进食：三餐时间到 或 饥饿 > 80 ---
        # 联动且已接管三餐时，本插件不再自己定饭点，避免一天吃两遍。
        # 外部接管三餐时，超过兜底时长没收到饭点就自己顶上，避免联动一断就绝食。
        # 时间基准取 max(最后一次外部餐段, 引擎起步时刻)：联动刚打开时还没收到
        # 过任何餐段，不能直接把"从 0 秒算起"当成饿了 10 小时。
        ext_starved = False
        if self._meals_external:
            # 接管时刻懒初始化：插件是直接 setattr(_meals_external=True) 的，
            # 这里补记起点。否则 s.t 是从仿真零点累计的，一接管就误判"饿了很久"。
            if self._ext_meal_epoch <= 0.0:
                self._ext_meal_epoch = s.t
            since = s.t - max(self._last_ext_meal_t, self._ext_meal_epoch)
            ext_starved = (since > self.link_meal_fallback_hours * H2S
                           or s.t < self._fallback_until)
        meal_times = () if (self._meals_external and not ext_starved) else (7.5, 12.5, 18.5)
        prev_h = self._prev_hour
        for mt in meal_times:
            # 只在「本步恰好跨过饭点」时触发。跨零点时 prev_h 大于 hour，单独处理。
            if s.hour >= prev_h:
                crossed = prev_h < mt <= s.hour
            else:
                crossed = (mt > prev_h) or (mt <= s.hour)
            # 饭点是「社会时刻」，不是饥饿驱动的：到点就吃，只要胃里不是满的。
            # 早先这里挂了 hunger > 30，实测 7:30 空腹饥饿只有 7.5、12:30/18:30
            # 因胃未排空恒为 0，结果三餐一次都没吃到，30 天 90 餐全是 hunger>80
            # 触发的加餐，餐段分布变成 snack 30 / lunch 30 / dinner 30。
            # 真实机制是胃扩张（GastricDistention）抑制食欲，不是「饿了才吃」。
            # 睡眠中不吃正餐：睡着的人不会爬起来吃早饭。
            # 跨过饭点时若在睡（例如熬到 7:30 才睡下），该餐跳过而不是补吃。
            if crossed and s.gastric_volume < 600.0 and not s.sleeping:
                self._eat(self._meal_slot(mt))
                if self._meals_external:
                    # 兜底进食：计时重置，并保持兜底状态满一天，让三餐节奏接上
                    # （只重置计时的话，下一顿又要等 16 小时，一天只吃两顿）。
                    self._last_ext_meal_t = s.t
                    self._fallback_until = s.t + 24.0 * H2S
                break
        # 加餐：睡眠中不吃。另外加餐与正餐互斥 —— 旧实现里 7:22 的加餐
        # 和 7:30 的早餐会连着来（hunger 阈值与饭点判定互不知情）。
        if s.hunger > 80.0 and s.gastric_volume < 300.0 and not s.sleeping:
            if not self._near_meal_time(s.hour, 0.75):
                self._eat("snack")

        self._prev_hour = s.hour

    # ------------------------------------------------------------------
    # 排尿
    # ------------------------------------------------------------------
    def _void(self):
        """排尿判定。

        分两层：
        * 行为层（enable_behaviour=True）给的是"尿意阈值"——清醒 300 mL
          （真人首次尿意 150-250、耐受 400-500），睡眠抬到 550 mL
          （正常人不会被 300 mL 叫醒，6-9 小时睡 0-1 次夜尿）。
        * 生理层是硬上限——膀胱最大容量约 600 mL，超过就必须排，
          与行为开关无关。旧实现把排尿整段写在 _behaviour 里，关掉行为层
          后膀胱能一路蓄到 4905 mL 而不产生任何后果（既不自溢也不回吸收），
          这既不合生理，也让"断水"实验测不出脱水。
        """
        s = self.s
        if self.enable_behaviour:
            limit = 550.0 if s.sleeping else 300.0
        else:
            limit = 600.0
        # 睡眠中即使行为层打开，只要超出生理硬上限也必须排（不自溢就出问题）
        if s.bladder < min(limit, 600.0):
            return
        voided_l = s.bladder / 1000.0
        s.urine_total += s.bladder            # mL 累计（state.py 注释已标明 UO）
        s.urinations += 1
        s.last_urine_t = s.t
        # 水的唯一出口（钠已在 _renal 按负荷单独排出，不在此重复扣）
        s.body_water = max(30.0, s.body_water - voided_l)
        s.bladder = 0.0
        if self.enable_void:
            self.bus.emit(Event(s.t, "urination", "info", "排尿",
                                {"urine_total": s.urine_total,
                                 "nocturnal": bool(s.sleeping)}))

    def _stool(self):
        """排便判定。

        结肠把液态食糜里的水吸回，残渣逐步变干成粪便。真人排便频率
        从 3 次/天到 3 次/周都算正常，典型 1 次/天（布里斯托 3-4 型）。
        这里按胃结肠反射建模：进食后（尤其早餐后）最容易产生便意。
        阈值 90 g 干残渣约对应一次成型便（真人粪便干重 20-30 g/次、
        总量 100-200 g，这里取偏保守的上端）。
        """
        s = self.s
        # 胃结肠反射：餐后 30 分钟内阈值下调，其余时刻保持高位
        since_meal = (s.t - s.last_meal_t) / 60.0      # 分钟
        reflex = 0.55 if 0.0 <= since_meal <= 30.0 else 1.0
        limit = 75.0 * reflex
        # 睡眠中不发生（正常人夜里不起来排便），但残渣累积不暂停
        if s.sleeping and s.colon_residue < 260.0:
            return
        if s.colon_residue < limit:
            return
        voided = s.colon_residue * 0.90
        s.colon_residue = max(0.0, s.colon_residue - voided)
        s.last_stool_t = s.t
        s.stools = getattr(s, "stools", 0) + 1
        if self.enable_stool:
            self.bus.emit(Event(s.t, "stool", "info", "排便",
                                {"residue_g": round(voided, 1)}))

    @staticmethod
    def _near_meal_time(hour: float, window_h: float) -> bool:
        """当前时刻是否紧邻某个饭点（用于加餐与正餐互斥）。"""
        for mt in (7.5, 12.5, 18.5):
            d = abs(hour - mt)
            d = min(d, 24.0 - d)        # 跨零点
            if d <= window_h:
                return True
        return False

    @staticmethod
    def _meal_slot(hour: float) -> str:
        if hour < 10.0:
            return "breakfast"
        if hour < 15.0:
            return "lunch"
        return "dinner"

    def feed(self, kind: str = "snack", items: list = None, note: str = "") -> dict:
        """外部喂食：把一次真实发生的吃喝灌进身体。

        与 _eat 的区别：_eat 从固定食谱池里随机挑，feed 接受指定食物，
        用于"我在聊天里说我点了外卖"这类需要落到具体食物的场景。
        kind 支持 meal(按当前钟点选餐段) / snack / drink。
        返回实际吃进去的组合，供上层回述。
        """
        s = self.s
        if s.hospital_until > s.t:
            return {"ok": False, "reason": "住院中，由医护供能"}

        if kind == "drink":
            keys = list(items) if items else food.pick_drink(self.rng)
            s.last_drink_t = s.t
            s.drinks += 1
            tot = food.combo_totals(keys)
            self._load_stomach(keys, tot, water_already=True)
            self.bus.emit(Event(s.t, "drink", "info", "喝水",
                                {"items": keys, "note": note}))
            return {"ok": True, "kind": "drink", "items": keys}

        slot = "snack" if kind == "snack" else self._meal_slot(s.hour)
        keys = list(items) if items else (
            food.pick_snack(self.rng) if slot == "snack" else food.pick_meal(slot, self.rng)
        )
        actual = self._eat_keys(slot, keys, note=note)
        return {"ok": True, "kind": slot, "items": list(actual or keys)}

    def _drink(self):
        """喝水：全部作为内容物进胃，由胃排空速率决定入血快慢。

        2026-11-06 修：原实现把 water_ml 直接加到 body_water，只把剩下的残渣
        （酒精 14 g、水 28 mL）留在胃里 —— 胃里水少、体积也小，liquid_bias 被算成 0，
        纯液体饮反而按混合固体餐的 58 min 半排空走，酒精还没入血就被零级代谢掉。
        实测伏特加 45 mL 峰值 BAC 只有 0.014 g/L，生理值约 0.25。
        现在水也走胃，液体饮 20 min 半排空，BAC 与糖负荷都能跑到生理量级。
        """
        s = self.s
        s.last_drink_t = s.t
        s.drinks += 1
        keys = food.pick_drink(self.rng)
        tot = food.combo_totals(keys)
        self._load_stomach(keys, tot)
        name = food.combo_summary(keys)
        s.last_drink_items = name
        self.bus.emit(Event(s.t, "drink", "info", f"喝水：{name}",
                            {"item": name, "water": round(s.body_water, 2)}))

    def _load_stomach(self, keys, tot, water_already=False):
        """把一组食物按成分灌进胃里（对应 APK 的 AddContent / GastricVolume）。"""
        s = self.s
        s.gastric_volume = min(1500.0, s.gastric_volume + tot["volume_ml"])
        s.chyme_carb += tot["carb_g"]
        s.chyme_protein += tot["protein_g"]
        s.chyme_fat += tot["fat_g"]
        s.chyme_na += tot["na_mmol"]
        s.chyme_alcohol += tot["alcohol_g"]
        s.chyme_caffeine += tot["caffeine_mg"]
        if not water_already:
            s.chyme_water += tot["water_ml"]

    def _eat(self, slot: str):
        """进食：按餐段挑一份食物组合，交给 _eat_keys 走同一条胃排空通路。"""
        if slot == "snack":
            keys = food.pick_snack(self.rng)
        else:
            keys = food.pick_meal(slot, self.rng)
        self._eat_keys(slot, keys)

    def _eat_keys(self, slot, keys, note=""):
        """按指定食物组合进餐：胃内容物 -> 逐项成分 -> 排空吸收。

        2026-11-06 改造：原实现是 glucose += 2.2 / na_mass += 40 的常量注入，
        与 APK 的 AddContent -> Chyle -> 分段吸收 结构不符（FoodType 23 项无法体现）。
        现改为胃内容物 + 逐项成分 + 排空吸收，见 _metabolic。
        """
        s = self.s
        # 正餐补到七分饱：日程/工具点名了具体食物时，那几句话往往只描述了
        # 一道主菜（"点了碗米粉"），按原样落地胃内容物只有两三百毫升。
        # 这里对所有正餐统一补足到 MEAL_TARGET_ML；零食（snack）不补。
        if slot in ("breakfast", "lunch", "dinner"):
            keys = food.ensure_meal_size(keys)
        keys = list(keys)
        tot = food.combo_totals(keys)
        self._load_stomach(keys, tot)

        s.last_meal_t = s.t
        s.meals += 1
        s.gut_empty_t = s.t
        # 胃扩张抑制食欲（对应 APK 的 GastricDistention / ReceptiveRelaxation）
        # 2026-11-11：不再直接把 hunger 写成 0。原写法让"吃一口就归零"，
        # 与激素链无关，也掩盖了份量不足的问题（吃 366 mL 和吃 900 mL
        # 在饥饿上完全一样）。现改为：进食立刻压低 ghrelin（胃底 G 细胞
        # 被扩张与营养共同抑制），饱腹感由胃扩张与肠道激素在下一步
        # _affect 里算出，吃多少就压多少。
        s.gut_full = clamp(s.gastric_volume / 900.0, 0.0, 1.0)
        s.ghrelin = min(s.ghrelin, clamp(0.90 * (1.0 - min(s.gut_full, 1.0)), 0.0, 1.20))
        s.last_meal_items = food.combo_summary(keys)

        self.bus.emit(Event(s.t, "meal", "info",
                            f"进食：{s.last_meal_items}（{tot['kcal']:.0f} kcal）",
                            {"items": s.last_meal_items,
                             "slot": slot,
                             "note": note,
                             "kcal": round(tot["kcal"], 0),
                             "carb_g": round(tot["carb_g"], 1),
                             "protein_g": round(tot["protein_g"], 1),
                             "fat_g": round(tot["fat_g"], 1),
                             "na_mmol": round(tot["na_mmol"], 1),
                             "meal_no": s.meals}))
        return keys

    # ------------------------------------------------------------------
    # 自主神经张力
    # ------------------------------------------------------------------
    def _autonomic(self, dt):
        s = self.s
        # 2026-11-08 重标定：旧式 0.25 + 0.50*circ 使交感昼夜摆幅达 0.25~0.75，
        # 夜间 MAP 掉 22-27%（真实 dipping 10-20%），舒张压跌破 55。
        # 抬高夜间基底、收窄相位权重，把摆幅压到 0.32~0.72。
        symp = (self.symp_base + self.symp_circ * s.circ
                + 0.40 * (s.stress / 100.0))
        if s.exercising:
            symp = min(1.0, symp + 0.45)
        # 睡眠期交感张力显著下降（非快动眼期以副交感为主），这是夜间心率、
        # 血压、体温同步下移的共同上游。睡眠深度 1 时交感降到清醒基线的 35%。
        # 睡眠期交感张力显著下降（NREM 以副交感为主），这是夜间心率、血压、
        # 体温同步下移的共同上游。深度 1 时交感降到清醒基线的 35%。
        # 2026-11-08：改用引擎自持的 s.sleep_depth（含外部 link 覆盖），
        # 旧版直接读 link_sleep，部署环境没有 [link] 段因而永不为 0。
        if s.sleep_depth > 0.0:
            # 睡眠只温和下调交感（按实测扫描定标）。下调过多会同时压低 CO 与 SVR，
            # 使夜间 MAP 掉 26%（真实只掉 10-15%），舒张压跌破 55。
            symp *= (1.0 - self.sleep_symp_discount * min(1.0, s.sleep_depth))
        # ---- 酒精与咖啡因对自主神经的作用（2026-11-08 新增）----
        # 旧版这两条通路是断的：bac / caffeine 被算出来、被写进 describe()，
        # 但没有任何一个生理模块读取，A/B 实测三罐啤酒与三杯水逐位相同。
        # 真实机制：乙醇增强 GABA-A、抑制 NMDA，中枢交感外流下降；
        # 同时乙醇代谢产物乙醛使皮肤血管扩张，外周阻力下降、散热增加。
        # 咖啡因阻断腺苷 A1/A2A 受体，交感外流上升、肾血流与尿量增加。
        # 幅度按 A/B 实测收敛：交感 -42% 叠加 SVR -28% 使三罐啤酒后 SBP 掉 19 mmHg，
        # 真实约 5-10（且部分被心率代偿抵消）。这里降到 -25%，SVR 降到 -18%。
        if s.bac > 0.0:
            symp *= max(0.50, 1.0 - 0.25 * clamp(s.bac / 1.0, 0.0, 1.0))
        if s.caffeine > 0.0:
            symp += 0.14 * clamp(s.caffeine / 200.0, 0.0, 1.0)
        # 活动强度小幅抬高交感；与运动标志分开，因为日常走动不等于锻炼。
        if self.link_activity > 0.0:
            symp += 0.18 * min(1.0, self.link_activity)
        symp = clamp(symp, 0.02, 1.0)
        para = clamp(1.05 - symp * 0.80, 0.05, 1.05)

        s.symp = symp
        s.para = para
        s.r_beta1 = clamp(0.12 + 0.55 * symp + 0.10 * s.adrenaline, 0, 1)
        s.r_alpha1 = clamp(0.08 + 0.45 * symp + 0.07 * s.ang2, 0, 1)
        s.r_m2 = clamp(0.15 + 0.55 * para, 0, 1)

    # ------------------------------------------------------------------
    # 心血管：心率 -> 每搏量 -> 心输出量 -> MAP -> SBP/DBP
    # ------------------------------------------------------------------
    def _cardio(self, dt):
        s = self.s

        # 2026-11-08 重标定：旧系数(62/45/22)把日间静息 HR 拉到 82(beta)
        # 与真实 70-75 偏高，同时夜间因 r_m2 项过重掉到 44。现按
        # 日间 72、夜间 58 两个实测靶点反解：beta1 与 m2 的摆动幅度都收窄。
        hr_t = (58.0 + 32.0 * s.r_beta1 - 16.0 * s.r_m2
                + 3.0 * (s.temp - 36.8) + 8.0 * (s.t3 - 1.0))
        if s.exercising:
            hr_t += 40.0
        # 睡眠时心率随笔迷走张力下降，深睡可降到 50 上下；不用运动那套 +40 的档
        # 睡眠对心率的效应已经全部通过 r_beta1 / r_m2 传进来了（交感下调 ->
        # beta1 降、迷走相对占优 -> M2 升）。这里不再另减，否则重复计账：
        # 实测叠加 -14 时夜间 HR 掉到 44、SBP 掉到 86，低于真人夜间下限。
        if self.link_activity > 0.0:
            hr_t += 10.0 * min(1.0, self.link_activity)
        hr_t = clamp(hr_t, 35.0, 185.0)
        s.hr = approach(s.hr, hr_t, dt, "hr")

        preload = clamp(s.blood_vol / 5000.0, 0.70, 1.15)
        afterload = 1.0 - 0.25 * max(0.0, s.r_alpha1 - 0.5)
        # 运动时静脉回流增加 + 心肌收缩力增强，每搏量**上升**（真实 70→110 mL）。
        # 2026-11-08 修：旧实现没有这一项，心率一涨 SV 就被 (1-0.30*(hr-70)/180)
        # 压下去，CO 只到 9 L/min，脉压反而收窄、舒张压上行 —— 与真实相反。
        sv_ex = 1.55 if s.exercising else 1.0
        s.sv = clamp(80.0 * preload * afterload
                     * (1.0 - 0.30 * (s.hr - 70.0) / 180.0) * sv_ex, 30.0, 150.0)
        s.co = max(1.5, s.hr * s.sv / 1000.0)

        # 运动时骨骼肌代谢性血管扩张，外周阻力**下降**；但心输出量升得更多，
        # 因此 MAP 维持或略升、收缩压**上行**、舒张压基本不变。
        # 2026-11-08 修：旧实现把 SVR 一刀砍到 55%，CO 只到 9.1 L/min，
        # 结果 MAP 从 121 掉到 96 —— 「越运动血压越低」，与生理相反。
        # 现按运动强度给 SVR -28%、并让 CO 按运动档上升（见 _cardio 的 sv/hr），
        # 净效应是 MAP 略升、PP 扩张（收缩压上去、舒张压不动）。
        # SVR 与 CO 的配比：真实中等强度运动 CO 升到 3-4 倍、SVR 降到 40-50%，
        # 两者相抵使 MAP 大致维持，收缩压因脉压扩张而上行、舒张压持平或略降。
        vasodil = 0.43 if s.exercising else 1.0
        # 酒精性血管扩张：乙醇/乙醛直接松弛血管平滑肌，SVR 下降 -> 血压略降、
        # 皮肤潮红、散热增加（真人喝一两罐啤酒收缩压降 5-10 mmHg）。
        if s.bac > 0.0:
            vasodil *= 1.0 - 0.18 * clamp(s.bac / 1.0, 0.0, 1.0)
        # 1450 -> 1280：旧系数下日间 MAP 到 114、SBP 148，比真实静息高 20 mmHg。
        # 按日间 MAP 93 反解（CO 5.7 L/min 时 SVR 需约 1300 dyn）。
        s.svr = 1280.0 * (0.72 + 0.55 * s.r_alpha1) * vasodil
        map_t = s.co * s.svr / 80.0
        s.map = approach(s.map, map_t, dt, "bp")
        s.map = clamp(s.map, 40.0, 180.0)

        # 脉压：PP = SV / C，C 为动脉顺应性 (mL/mmHg)。
        # 交感张力升高时血管变硬、C 下降，脉压随之扩张 —— 这是 PP 随 MAP 同向
        # 上升的物理来源。早前版本让 PP 恒定在 40，导致 MAP 冲高时舒张压顶穿。
        # 2.05 -> 2.35：旧值让静息脉压扩张到 50 mmHg（真实 35-45），
        # 于是 SBP 被抬、DBP 被压，收缩/舒张同时偏离。
        compliance = 2.35 - 0.55 * s.symp
        pp_t = max(18.0, s.sv / compliance)
        s.pp = approach(s.pp, pp_t, dt, "bp")
        # 用平滑后的 MAP 拆压，避免瞬时耦合把舒张压顶穿
        # 2026-11-08 修：旧实现硬编码 dt/5.0，dt=10 时系数恰为 2.0 —— 每一步把
        # 偏差翻倍反号，SBP/DBP 呈周期 2 抖动（实测 158.2/147.5 交替）。
        # 改用与 MAP 同一个时间常数并限幅，dt=10 系数降到 0.5，平滑收敛。
        s.sbp = approach(s.sbp, s.map + s.pp * 2.0 / 3.0, dt, "bp")
        s.dbp = approach(s.dbp, s.map - s.pp / 3.0, dt, "bp")

    # ------------------------------------------------------------------
    # 呼吸
    # ------------------------------------------------------------------
    def _respiratory(self, dt):
        s = self.s
        rr_t = 14.0 + 6.0 * (s.stress / 100.0) + 0.8 * max(0.0, s.paco2 - 40.0) / 5.0
        if s.exercising:
            rr_t += 8.0
        s.rr = approach(s.rr, clamp(rr_t, 6.0, 40.0), dt, "rr")

        vent = s.rr * s.tidal / 1000.0
        o2_t = clamp(0.90 + 0.10 * (vent / 7.0) - 0.5 * max(0.0, s.blood_vol / 5000.0 - 1.0), 0.60, 0.995)
        # dt 大于 TAU["o2"] 时一阶惯性会过冲（实测 dt=10 得到 107%），必须钳位
        s.sao2 = clamp(approach(s.sao2, o2_t * 100.0, dt, "o2"), 0.0, 100.0)

        # CO2 与通气互为反比
        # 肺泡 CO2 分压方程：pCO2 = 863 × VCO2 / VA
        #   VCO2 静息 0.20 L/min（约对应 1200 kcal/天），运动升到 0.55
        #   VA 肺泡通气 = 0.65 × 分钟通气（其余是死腔）
        # 旧实现 paco2_t = clamp(700/vent, 25, 65)：vent≈10 时得 70 被 65 截断，
        # 实测 pCO2 恒为 65.0（std=0），属于「钉死的假数值」。
        # 解剖死腔按绝对量算：150 mL/次（潮气量 500 时占 30%）
        va = max((s.tidal - 150.0) * s.rr / 1000.0, 1.0)
        # 静息 VCO2 0.23 L/min（约 2000 kcal/天，RQ 0.85）；运动升到 0.58
        vco2 = 0.255 + (0.35 if s.exercising else 0.0)
        vco2 += max(0.0, s.temp - 37.0) * 0.06
        paco2_t = clamp(863.0 * vco2 / va, 25.0, 70.0)
        s.paco2 = approach_tau(s.paco2, paco2_t, dt, 30.0)

    # ------------------------------------------------------------------
    # 酸碱平衡：pH 由碳酸氢根与 pCO2 共同决定
    # ------------------------------------------------------------------
    def _acidbase(self, dt):
        s = self.s
        # 肾脏对慢性高碳酸血症的代偿：pCO2 每高于 40 mmHg 10，HCO3- 升 3.5 mmol/L。
        # 时间常数取 6 小时（真实肾脏代偿需数小时到数天，这里取快端以免长期偏酸）。
        chronic = max(0.0, s.paco2 - 40.0)
        hco3_t = 24.0 + chronic / 10.0 * 3.5
        # 急性呼吸性酸中毒时 HCO3 尚未代偿；用 6 h 的一阶惯性表现这个滞后
        s.hco3 = approach_tau(s.hco3, hco3_t, dt, 6.0 * H2S)
        # Henderson-Hasselbalch：pH = 6.1 + log10(HCO3 / (0.03 × pCO2))
        denom = max(0.03 * max(s.paco2, 5.0), 1e-6)
        # 生理可达范围：碱中毒上限约 7.65（极端），酸中毒下限约 6.80
        s.ph = clamp(6.1 + math.log10(max(s.hco3, 1.0) / denom), 6.80, 7.65)

    # ------------------------------------------------------------------
    # 代谢：血糖 / 糖原 / ATP / 乳酸 / 酮体
    # ------------------------------------------------------------------
    def _metabolic(self, dt):
        s = self.s

        # ---- 胃肠内容物：排空 -> 吸收（对应 APK 的 AddContent / Chyle / AbsorbableType）----
        self._digest(dt);

        # 胃扩张程度（0..1），供饱腹与食欲使用
        s.gut_full = clamp(s.gastric_volume / 900.0, 0.0, 1.0)

        # 胰岛素：血糖 + 肠促胰岛素效应（incretin effect）+ 血氨基酸。
        # 旧实现只看血糖，餐后血糖 6.0 时胰岛素只有 0.40 —— 而真人此时
        # GIP/GLP-1 已在门静脉把 β 细胞推到 2~3 倍基值，是先于血糖升高
        # 出现的（口服葡萄糖比静脉注射同样负荷的胰岛素反应强 2~3 倍）。
        # 缺这一项会让餐后血糖清得过慢、且降糖与升糖两个方向都失真。
        incretin = (0.55 * max(0.0, min(s.gip, 1.6) - 0.10)
                    + 0.35 * max(0.0, min(s.glp1, 1.6) - 0.10))
        amino_drive = 0.12 * clamp(s.gut_amino / 20.0, 0.0, 1.0)
        s.insulin = clamp(0.30 + 0.50 * (s.glucose - 5.0) / 5.0
                          + incretin + amino_drive, 0.10, 1.60)

        # 葡萄糖动力学：显式单位（mmol/min ÷ 分布容积 L），不用混合量纲系数。
        # 旧版 dg*dt/4.0 是量纲糊在一起的产物，实测 14.0 -> 9.5 只用 5.1 分钟，
        # 比真实（餐后 30~60 分钟回 baseline）快一个数量级。
        # V_g = 14 L 葡萄糖分布容积；肝输出基础 0.78 mmol/min（≈2 mg/kg/min × 70 kg）。
        GLUCOSE_V = 14.0
        # 2026-11-08 重写糖原闭环：旧实现把肝输出当成无限量的常数 0.78 mmol/min，
        # 只在「净消耗（net<0）」时扣糖原，而 net 几乎从不为负 —— 于是吃什么都不
        # 补糖原、糖原只减不增，30 天长跑吃 105 餐仍掉到 4.3，实测连吃 4 顿
        # 米饭糖原反而从 379 递减到 83。真实机制相反：肝输出**就是**在分解
        # 糖原，血糖升高时肝输出被抑制、多余葡萄糖回补糖原。
        # 现在按「肝糖原存量」限制肝输出，糖原见底则肝输出塌陷、出现低血糖。
        gly_frac = clamp(s.glycogen / 80.0, 0.0, 1.0)     # 80 g 以上输出不受限
        # 餐后肝输出必须被抑制：真实进食后门静脉胰岛素直接把肝糖输出压到
        # 接近零，血糖才降得下来。旧实现不管血糖多高都恒定输出 0.78 mmol/min，
        # 结果餐后仍在往血里倒糖、同时又因为 net<0 而不补糖原 —— 实测 30 天
        # 吃 87 餐糖原仍从 400 掉到 14，夜间血糖被拖到 3.7。
        # 2026-11-11：改成胰岛素主导的抑制（门静脉胰岛素是肝糖输出的
        # 第一信号，血糖只作辅助）。原式纯按血糖，加入肠促胰岛素后仍
        # 压不住肝输出，餐后血糖峰值被削到 5.9 反而低于改动前。
        hgp_suppress = clamp(1.0 - max(0.0, s.glucose - 5.0) * 0.30
                             - max(0.0, s.insulin - 0.30) * 0.80, 0.0, 1.0)
        hepatic_mmol_min = (0.78 * (1.0 + 1.5 * max(0.0, (5.0 - s.glucose) / 3.0))
                            * gly_frac * hgp_suppress)
        # 糖异生：糖原耗尽后肝脏转向脂肪/蛋白来源的糖异生，这是禁食能维持
        # 血糖 3.5-5.0 的关键（真人禁食 24 h 肝糖原即耗尽，血糖并不塌）。
        # 旧实现没有这一项，糖原一空肝输出就归零，实测禁食第 5 天血糖掉到 2.0。
        # 原料优先用脂肪储备；但真实禁食主要靠肌肉蛋白分解，因此即使
        # fat_reserve 为 0（BodyState 里它只是"刚吸收待氧化的脂肪"中间量，
        # 不是身体脂肪库）也必须保留一条基线糖异生，否则血糖会一路掉到 2.0。
        if gly_frac < 1.0:
            gng_fat = 0.40 * (1.0 - gly_frac) * clamp(s.fat_reserve / 10.0, 0.0, 1.0)
            gng_protein = 0.34 * (1.0 - gly_frac)     # 肌肉蛋白分解，禁食 24 h 后主导
            gng = gng_fat + gng_protein
            hepatic_mmol_min += gng
            # 糖异生的原料来自脂肪储备，按 1 mmol 葡萄糖 ≈ 0.09 g 脂肪折算
            s.fat_reserve = max(0.0, s.fat_reserve - gng_fat * 0.09 * (dt / 60.0))
        # 外周摄取对胰岛素的斜率 0.90 -> 0.45：真实餐后葡萄糖处置里
        # 肝输出抑制占大头，肌肉摄取是次要项。旧斜率让胰岛素一升就把
        # 血糖清光，峰值根本起不来。
        tissue_mmol_min = 0.19 * s.glucose * (0.55 + 0.45 * s.insulin)
        if s.exercising:
            tissue_mmol_min *= 1.8
        net_mmol_min = hepatic_mmol_min - tissue_mmol_min
        s.glucose = clamp(s.glucose + net_mmol_min / GLUCOSE_V * (dt / 60.0), 2.0, 25.0)
        # 糖原账本（单位：g）。GLUCOSE_G_PER_MMOL = 180 mg = 0.18 g，
        # 旧实现漏了这个换算，把 mmol/min 直接当 g/min 扣，糖原消耗快 5.6 倍
        # （实测 400 g 在 10 h 内耗尽，真实需 24-48 h）。
        GLUCOSE_G_PER_MMOL = 0.18
        s.glycogen = clamp(s.glycogen
                           - hepatic_mmol_min * GLUCOSE_G_PER_MMOL * (dt / 60.0),
                           0.0, 500.0)
        # 回补糖原的驱动量是「血糖高出设定点的部分」，与 net 正负无关 ——
        # 餐后血糖上升靠的是肠吸收（不在 net 里），旧实现的 net>0 门几乎
        # 永不成立，于是吃什么都不补糖原。
        if s.glucose > 5.0:
            store = max(0.0, s.glucose - 5.0) * s.insulin * 3.0
            s.glycogen = clamp(s.glycogen
                               + store * GLUCOSE_G_PER_MMOL * (dt / 60.0),
                               0.0, 500.0)

        # ---- 乳酸：产生快、清除慢的双相模型（2026-11-14 重写）----
        # 旧实现用单一 approach_tau(..., 180s) 逼近一个离散目标值：
        # 一停跑就以 3 分钟时间常数往下掉，实测 5 分钟内从 8.0 掉到 1.8。
        # 这与生理不符 —— 静息恢复时血乳酸清除的半衰期约 25 分钟
        # （主动恢复更快），完全回到基线需要 1~2 小时。差了十倍以上。
        #
        # 文献：
        #   [R1] Devlin J, et al. Blood lactate clearance after maximal exercise
        #        depends on active recovery intensity. J Sports Med Phys Fitness.
        #        2014;54(3):271-278. PMID 24739289
        #        → 支撑「主动恢复清除更快」的定性方向（本模型 ×1.25 的依据）
        #   [R2] 半衰期 25 分钟 —— 二手转述（科普文），非一手论文。
        #        数字与教科书一致但未核实原始研究，后续应替换。
        # 产率 28 / 15 mmol/L/h 为估算（反推自 15 分钟达 8 mmol/L）。
        #
        # 现在拆成两条独立通路：
        #   1) 产生：运动时按强度持续生成，与当前乳酸浓度无关（线性产率）
        #   2) 清除：始终进行，速率与浓度成正比（一阶，半衰期 25 分钟）
        # 运动停止后清除项继续作用，所以曲线是"慢衰减尾巴"而非断崖。
        lac_base = 1.0                      # 静息基线 mmol/L
        # 产生速率：高强度运动时血乳酸通常升到 6~12 mmol/L。
        # 目标是在 15 分钟持续高强度下爬到约 8，所以产率取 28 mmol/L/h
        # （同时清除项一直在起作用，净累积约一半）。
        if s.exercising:
            level = str(getattr(s, "exercise_intent", "") or "mid")
            prod = 28.0 if level == "high" else 15.0
            # 肌糖原见底时无氧供能占比上升，产乳酸更快
            if s.glycogen < 80.0:
                prod *= 1.35
            s.lactate += prod * (dt / H2S)
        # 肾上腺素偏高也会轻度促乳酸（应激性糖解）
        if s.adrenaline > 0.6:
            s.lactate += (s.adrenaline - 0.6) * 1.2 * (dt / H2S)
        # 清除：一阶，半衰期 25 分钟。运动刚结束时清除略快（肌肉仍在泵血），
        # 但没有旧模型那么夸张 —— 这里用 1.25 倍系数体现"运动后即刻"。
        clear_halflife_min = 25.0
        clear_k = math.log(2.0) / (clear_halflife_min * 60.0)
        recent = (s.t - float(getattr(s, "last_exercise_t", 0.0))) < 600.0
        if recent:
            clear_k *= 1.25
        above = max(0.0, s.lactate - lac_base)
        s.lactate -= above * (1.0 - math.exp(-clear_k * dt))
        s.lactate = clamp(s.lactate, 0.5, 25.0)

        # 脂肪储备氧化：吸收进来的脂肪按 6 h 半衰期进入供能池。
        # 没有这一项时高脂餐（坚果/巧克力/三明治）与白米饭对能量毫无区别。
        if s.fat_reserve > 0.0:
            s.fat_reserve = max(0.0, s.fat_reserve * math.exp(-math.log(2.0) * dt / (6.0 * H2S)))

        # 细胞能量
        atp_t = clamp(0.35 + 0.65 * (1.0 - math.exp(-s.glucose * 0.35)), 0.0, 1.0)
        # 脂肪供能（脂肪酸氧化直接抬 ATP 底位）
        atp_t = clamp(atp_t + 0.12 * clamp(s.fat_reserve / 40.0, 0.0, 1.0), 0.0, 1.0)
        # 乙醇供能但抑制糖异生：短时抬 ATP，随后拖低（对应 BAC 的醉酒表现）
        if s.bac > 0.05:
            atp_t = clamp(atp_t + 0.08 * clamp(s.bac / 0.8, 0.0, 1.0), 0.0, 1.0)
        s.atp = approach(s.atp, atp_t, dt, "atp")

    # ------------------------------------------------------------------
    # 消化吸收：胃排空 -> 肠吸收 -> 入血
    #   原版结构（元数据可证）：LMFood.AddContent 把食物放进胃，
    #   胃内容物变成 Chyle（食糜），经 Pylorus 进入十二指肠/空肠/回肠，
    #   各段按 DuodenumAbsorbableType / JejeunumAbsorbableType /
    #   ReticulumAbsorbableType 吸收，WaterAbsorption 单独处理水。
    # ------------------------------------------------------------------
    # 碳水只有一部分以葡萄糖形式直接入血，其余被肝脏首过摄取转为糖原。
    # 2026-11-11 由 0.35 提到 0.70：实测 0.35 时 63 g 碳水的一顿午饭
    # 峰值只有 6.00 mmol/L（升 1.0），而真人同餐升 2~3 mmol/L。
    # 0.35 相当于假定 65% 的碳水首过就被肝扣下，远高于真实的 30~40%。
    # 0.70 → 实测同餐峰值 6.98 mmol/L（升 ~2.0），与生理一致。
    CARB_TO_BLOOD = 0.70

    # 乙醇分布容积（Widmark r × 体重 70 kg）：0.68 L/kg × 70 = 47.6 L
    ETHANOL_VD = 47.6

    def _digest(self, dt):
        s = self.s

        # ---- 胃排空 -> 十二指肠 ----
        # 2026-11-11：旧实现把幽门排出的食糜直接倒进"待吸收入血"池，等于
        # 没有小肠。APK 里 Duodenum / DuodenumVolume / Jejunum / Ileum 四段
        # 都在（见 apk_report/il2cpp_strings.txt:5164-5168, 9393-9396），
        # 分段是原版的结构，不是可选的精细化。
        # 现改为 胃 -> 十二指肠（酸中和/渗透压感受）-> 空肠（吸收）-> 回肠（刹车）。
        if s.gastric_volume > 0.05:
            # 液体半排空约 20 min，混合固体餐约 90 min。用「可自由水占比」插值。
            liquid_bias = clamp(s.chyme_water / max(s.gastric_volume, 1.0), 0.0, 1.0)
            half_min = 90.0 - 70.0 * liquid_bias
            # 固形物按质量判定：蛋白 + 脂肪 + 碳水都算固形物。
            solid_g = s.chyme_protein + s.chyme_fat + s.chyme_carb * 0.25
            if solid_g < 1.0:
                half_min = 20.0                      # 纯液体饮
            frac = 1.0 - math.exp(-math.log(2.0) / (half_min * 60.0) * dt)
            frac = clamp(frac, 0.0, 0.25)

            s.gastric_volume = max(0.0, s.gastric_volume * (1.0 - frac))

            # 各成分按比例离胃；脂肪排空最慢（0.6 倍），蛋白 0.9 倍
            carb_out = s.chyme_carb * frac
            prot_out = s.chyme_protein * frac * 0.9
            fat_out = s.chyme_fat * frac * 0.6
            na_out = s.chyme_na * frac
            alc_out = min(s.chyme_alcohol, s.chyme_alcohol * min(1.0, frac * 2.5))
            caf_out = s.chyme_caffeine * frac
            water_out = s.chyme_water * frac

            s.chyme_carb -= carb_out
            s.chyme_protein -= prot_out
            s.chyme_fat -= fat_out
            s.chyme_na -= na_out
            s.chyme_alcohol -= alc_out
            s.chyme_caffeine -= caf_out
            s.chyme_water -= water_out

            # 水与钠直接入血（胃/小肠吸水很快，无酶限速）
            s.body_water = s.body_water + water_out / 1000.0
            s.na_mass = min(6500.0, s.na_mass + na_out)
            if na_out > 0.0:
                inst = na_out / dt * 86400.0
                s.na_intake_24h += (inst - s.na_intake_24h) * dt / (24.0 * H2S)
            if fat_out > 0.0:
                s.fat_reserve = min(400.0, s.fat_reserve + fat_out)

            # 乙醇：小分子、脂水两溶，吸收不随食糜固相一起慢走
            if alc_out > 0.0:
                s.bac += alc_out / self.ETHANOL_VD
            if caf_out > 0.0:
                s.caffeine += caf_out * 0.9

            # 排入十二指肠（不再直接入血）
            s.duodenum_volume = min(500.0, s.duodenum_volume + max(0.0, water_out))
            s.duodenum_carb += carb_out
            s.duodenum_protein += prot_out
            s.duodenum_fat += fat_out

        # ---- 十二指肠：酸中和 + 渗透感受，激发 secretin / CCK ----
        if s.duodenum_volume > 0.05:
            # 十二指肠转运很快（半时约 12 min），主要作用是让食糜接触
            # 胰液/胆汁并把信号传给腺体，不是主要吸收场所。
            k_duo = 1.0 - math.exp(-math.log(2.0) / (12.0 * 60.0) * dt)
            duo_frac = clamp(k_duo, 0.0, 0.30)
            duo_carb = s.duodenum_carb * duo_frac
            duo_prot = s.duodenum_protein * duo_frac
            duo_fat = s.duodenum_fat * duo_frac
            # 脂肪与蛋白是 CCK 的最强刺激；酸负荷（近似按体积）刺激 secretin
            chyme_conc = (s.duodenum_fat + s.duodenum_protein) / max(s.duodenum_volume, 1.0)
            secretin_t = clamp(0.10 + 1.20 * clamp(s.duodenum_volume / 220.0, 0.0, 1.0), 0.10, 1.40)
            cck_t = clamp(0.10 + 2.60 * clamp(chyme_conc / 0.30, 0.0, 1.0), 0.10, 1.60)
            s.duodenum_volume = max(0.0, s.duodenum_volume * (1.0 - duo_frac))
            s.duodenum_carb -= duo_carb
            s.duodenum_protein -= duo_prot
            s.duodenum_fat -= duo_fat
            # 十二指肠只吸收少量：碳水 15%、蛋白 10%、脂肪 5%
            s.gut_glucose += duo_carb * 0.15 * 1000.0 / 180.0 * self.CARB_TO_BLOOD
            s.glycogen = min(500.0, s.glycogen + duo_carb * 0.15 * (1.0 - self.CARB_TO_BLOOD) * 0.8)
            s.gut_amino += duo_prot * 0.10
            s.fat_reserve = min(400.0, s.fat_reserve + duo_fat * 0.05)
            # 剩余进空肠
            s.jejunum_volume = min(900.0, s.jejunum_volume + max(0.0, s.duodenum_volume) * duo_frac)
            s.jejunum_carb += duo_carb * 0.85
            s.jejunum_protein += duo_prot * 0.90
            s.jejunum_fat += duo_fat * 0.95
        else:
            secretin_t = 0.10
            cck_t = 0.10

        # ---- 空肠：主吸收段（APK: Jejunum / JejunumGland）----
        if s.jejunum_carb > 1e-9 or s.jejunum_protein > 1e-9 or s.jejunum_fat > 1e-9:
            # 2026-11-11：半时 35 -> 14 min。空肠绒毛表面积巨大，糖/氨基酸
            # 吸收极快（真人口服葡萄糖后 15 min 内血糖即明显上升）。旧的
            # 35 min 把整条曲线拖成"3 小时缓坡"，峰值推迟到 122 min。
            k_jej = 1.0 - math.exp(-math.log(2.0) / (14.0 * 60.0) * dt)
            jej_frac = clamp(k_jej, 0.0, 0.30)
            j_carb = s.jejunum_carb * jej_frac
            j_prot = s.jejunum_protein * jej_frac
            j_fat = s.jejunum_fat * jej_frac
            s.jejunum_carb -= j_carb
            s.jejunum_protein -= j_prot
            s.jejunum_fat -= j_fat
            s.jejunum_volume = max(0.0, s.jejunum_volume * (1.0 - jej_frac))
            # 空肠吸收：碳水 92%、蛋白 78%、脂肪 55%（脂肪主在回肠/淋巴）
            s.gut_glucose += j_carb * 0.92 * 1000.0 / 180.0 * self.CARB_TO_BLOOD
            s.glycogen = min(500.0, s.glycogen + j_carb * 0.92 * (1.0 - self.CARB_TO_BLOOD) * 0.8)
            s.gut_amino += j_prot * 0.78
            s.fat_reserve = min(400.0, s.fat_reserve + j_fat * 0.55)
            # 未吸收部分入回肠
            s.ileum_residue = min(300.0, s.ileum_residue
                                  + j_carb * 0.08 + j_prot * 0.22 + j_fat * 0.45)

        # ---- 回肠：PYY / GLP-1 刹车（"回肠刹车"）----
        if s.ileum_residue > 0.05:
            s.ileum_residue = max(0.0, s.ileum_residue - dt / H2S * 4.0)
        pyy_t = clamp(0.10 + 2.40 * clamp(s.ileum_residue / 40.0, 0.0, 1.0), 0.10, 1.60)
        glp1_t = clamp(0.10 + 1.80 * clamp(s.ileum_residue / 40.0, 0.0, 1.0)
                       + 0.90 * clamp((s.glucose - 6.0) / 6.0, 0.0, 1.0), 0.10, 1.60)

        # ---- 肠道激素动力学（一阶趋近，半时各自不同）----
        s.secretin = approach_tau(s.secretin, secretin_t, dt, 8.0 * 60.0)
        s.cck = approach_tau(s.cck, cck_t, dt, 10.0 * 60.0)
        s.pyy = approach_tau(s.pyy, pyy_t, dt, 12.0 * 60.0)
        s.glp1 = approach_tau(s.glp1, glp1_t, dt, 6.0 * 60.0)
        # 胃泌素：蛋白入胃刺激 G 细胞，胃排空后回落
        gastrin_t = clamp(0.15 + 2.20 * clamp(s.chyme_protein / 25.0, 0.0, 1.0), 0.15, 1.60)
        s.gastrin = approach_tau(s.gastrin, gastrin_t, dt, 15.0 * 60.0)
        # GIP：碳水在小肠的浓度（K 细胞），促胰岛素
        gip_t = clamp(0.10 + 2.00 * clamp((s.duodenum_carb + s.jejunum_carb) / 40.0, 0.0, 1.0),
                      0.10, 1.60)
        s.gip = approach_tau(s.gip, gip_t, dt, 8.0 * 60.0)

        # ---- 胃饥饿素 ghrelin：空腹升高、进食即降 ----
        # APK: Ghrelin。由胃底分泌，胃扩张与营养入血共同抑制。
        fasted_h = (s.t - s.last_meal_t) / H2S
        # 2026-11-11：原式 -0.80*gastric/600 让胃一扩张 ghrelin 立刻归零，
        # 20 min 趋近时间常数又在胃排空后让它瞬间回爬，实测 18:03 进食
        # 后 26 分钟 ghrelin 就跌到 0.0073，之后靠 fasted_h 一路涨回来。
        # 真实 ghrelin 在进食后 1~2 h 内被压制、且随餐次能量密度而非单纯
        # 胃体积变化，故把机械压制降到 -0.45，并把小肠营养也算作抑制项；
        # 趋近时间常数 20 -> 35 min，回落不再那么陡。
        ghrelin_t = clamp(1.10 * (1.0 - math.exp(-fasted_h / 5.0))
                          - 0.45 * clamp(s.gastric_volume / 600.0, 0.0, 1.0)
                          - 0.35 * clamp((s.jejunum_carb + s.duodenum_carb) / 40.0, 0.0, 1.0)
                          - 0.12 * clamp(s.pyy, 0.0, 1.6),
                          0.0, 1.20)
        s.ghrelin = approach_tau(s.ghrelin, ghrelin_t, dt, 35.0 * 60.0)

        # ---- 肠吸收：糖池 -> 血（空肠已分段，这里只做最终入血）----
        if s.gut_glucose > 1e-9:
            # 半时 18 -> 8 min：门静脉到体循环的转运本身只需几分钟。
            k_abs = math.log(2.0) / (8.0 * 60.0)
            absorbed = s.gut_glucose * (1.0 - math.exp(-k_abs * dt))
            s.gut_glucose -= absorbed
            s.glucose = clamp(s.glucose + absorbed / 14.0, 2.0, 25.0)

        # ---- 氨基酸池：蛋白 -> 糖原（糖异生）+ 缓慢供能 ----
        if s.gut_amino > 1e-9:
            k_aa = math.log(2.0) / (60.0 * 60.0)
            aa_out = s.gut_amino * (1.0 - math.exp(-k_aa * dt))
            s.gut_amino -= aa_out
            s.glycogen = min(500.0, s.glycogen + aa_out * 0.35)

        # ---- 不可消化残渣入结肠 ----
        s.colon_residue = min(400.0, s.colon_residue
                              + s.ileum_residue * dt / H2S * 0.5)

        # ---- 乙醇：Widmark 分布，零级消除 0.15 g/L/h ----
        if s.bac > 0.0:
            s.bac = max(0.0, s.bac - 0.15 * dt / H2S)

        # ---- 咖啡因：一阶消除，半衰期 5 h ----
        if s.caffeine > 0.0:
            s.caffeine = max(0.0, s.caffeine * math.exp(-math.log(2.0) * dt / (5.0 * H2S)))
    # ------------------------------------------------------------------
    # 肾脏与体液
    # ------------------------------------------------------------------
    def _renal(self, dt):
        s = self.s

        # GFR 随肾灌注压自调（60-180 mmHg 之间基本恒定，过低线性下降）
        # 肾小球滤过率自调节（管球反馈 + 肌源性反应）。
        # 早期写法 125*clamp((map-40)/55) 是纯正比，夜间 MAP 掉到 70 时 GFR 腰斩到 66，
        # 尿量随之崩到 0.7 L/天、排尿 1.8 次/天。真实肾脏在 MAP 80~180 之间把 GFR
        # 稳定在 125 左右（平台段），低于 80 才随压力线性下降，高于 160 才缓慢升高。
        if s.map < 80.0:
            gfr_t = 125.0 * max(0.20, s.map / 80.0)        # 低压段：失去自调节
        elif s.map <= 160.0:
            gfr_t = 125.0                                   # 平台段：自调节
        else:
            gfr_t = 125.0 * min(1.15, 1.0 + (s.map - 160.0) * 0.0015)
        s.gfr = approach(s.gfr, gfr_t, dt, "renal")

        # ---- 钠平衡：独立调节轴，按负荷排出 ----
        # 真实肾脏按「摄入 - 需要」调节钠排出，与尿量是两条独立的轴。
        # 早前把钠排出挂在尿量上，尿量又被 ADH 压住，钠排不掉、一路顶到上限。
        # 标定：设定点 5880 mmol；排出目标 = max(60, 近 24 h 膳食钠吸收速率)，
        # 再乘负荷因子。原实现把目标写死成 120 mmol/天，加入食物表后
        # 不同餐单的钠差异（汤 21.7 / 披萨 26 / 伏特加 0）无法进入平衡，
        # 血钠会被硬拉回同一点，食物等于没吃。
        na_load = s.na_mass / 5880.0
        na_target_day = max(60.0, s.na_intake_24h)
        na_excrete = (na_target_day / 86400.0) * clamp(na_load ** 4, 0.15, 8.0) * dt
        # 醛固酮保钠：削减排出量
        na_excrete *= (1.0 - 0.45 * clamp(s.aldosterone - 0.5, 0.0, 1.5))
        s.na_mass = max(4500.0, s.na_mass - na_excrete)

        # 醛固酮「回收」：真实靶点是肾小管 Na+ 重吸收，效果是「少排」而非「凭空加」。
        # 2026-11-14 修：旧实现在 _endocrine 里写 na < 140 就 min(6200, +aldo*0.02*dt)，
        # 只加不减，90 天实测 na_mass 从 6060 一路顶到 6200 天花板再也不回落。
        # 现在改为从本步排出量里「省下」一部分，保证总量守恒、能回落。
        if s.na < 140.0 and na_excrete > 0.0:
            deficit = clamp((140.0 - s.na) / 5.0, 0.0, 1.0)
            s.na_mass += na_excrete * 0.55 * deficit

        # ---- 水平衡：尿量 = 溶质排出 / 尿渗透压 ----
        # 早期写法用「滤过量 × (1-重吸收率)」，重吸收率又被标定成 0.9917 这种半经验数，
        # 结果是尿量被钉死在 0.7 L/天、排尿 1.8 次/天，与摄入量无关 —— 物理上说不通：
        # 只有当摄入变化能改变尿量时，体液平衡才是自洽的。
        # 真实机制：每天必须排掉约 700 mOsm 溶质（尿素+电解质），ADH 只决定尿液浓缩程度，
        # 于是尿量 = 溶质 / 尿渗透压。ADH 高 → 尿浓 → 尿少；ADH 低 → 尿稀 → 尿多。
        # 溶质负荷随膳食走：基础 600 mOsm + 钠负荷（120 mmol 时≈696，与原标定 700 等价）
        # 2026-11-08 新增渗透性利尿项：血糖超过肾糖阈（约 10 mmol/L，个体 8.9-10）
        # 后，滤过葡萄糖超出肾小管 SGLT 重吸收上限（Tm ≈ 375 mg/min），
        # 未重吸收的葡萄糖留在管腔形成渗透压，把水一起带走 —— 这就是糖尿病
        # 多尿的机制。旧实现 solute 只有钠项，A/B 实测 glucose=20 时尿量反而比
        # glucose=5 更少（因为高血糖经胰岛素把 GFR 那条轴也压低了），与真实相反。
        # 但渗透性利尿有一个关键特征：它**不受 ADH 浓缩能力约束**。未重吸收的
        # 葡萄糖在管腔里形成渗透梯度，即使 ADH 拉满、尿液已浓缩到 1200 mOsm/L，
        # 这部分水照样被留住排出去。所以不能只往 solute 里加数 —— 实测那样做时
        # ADH 会同步升高把尿重新浓缩，glucose=20 的尿量反而比 glucose=5 更少。
        # 正确做法：在 ADH 决定的尿量之外，单独加一条 osmotically obligated 水。
        # 定量：肾糖阈 10 mmol/L、GFR 125 mL/min，超出部分滤过负荷超过 Tm
        # （375 mg/min）后被排出；每 mmol 葡萄糖约带走 18 mL 水。
        # 定量推导：GFR 125 mL/min = 180 L/天。肾糖阈 10 mmol/L 以上每 mmol/L
        # 每天多滤过 180 mmol 葡萄糖，全部排入管腔（已超 Tm，重吸收饱和）。
        # 管腔里这些葡萄糖按 0.6 osmol/L 的等效浓度携水，故每 mmol/L 超出
        # 带来 180/0.6 = 300 mL/天的强制排水。glucose=20 时约 3 L/天多尿，
        # 与临床糖尿病失控期的 3-5 L/天吻合。
        glu_excess = max(0.0, s.glucose - 10.0)
        glu_urine_day_ml = glu_excess * 300.0
        solute_mosm_day = 600.0 + 0.80 * s.na_intake_24h
        # 300 -> 620：旧系数让基线尿渗透压只有 255 mOsm/L（比重约 1.007），
        # 而真人正常尿 500-800 mOsm/L（比重 1.015-1.025）。尿太稀的直接后果是
        # 每日尿量被抬到 2.4-3.0 L，长期水负平衡把总体水从 42 拖到 39，
        # 30 天里反复触发脱水送医 —— 这是"正常人被模型渴死"的假象，不是脱水。
        urine_osmol = clamp(620.0 * s.adh, 50.0, 1200.0)   # mOsm/L
        urine_ml = solute_mosm_day / urine_osmol * 1000.0 * (dt / 86400.0)
        # 渗透性利尿：不受 ADH 约束的强制排水项
        urine_ml += glu_urine_day_ml * (dt / 86400.0)
        # 只累积到膀胱，水在排尿那一刻才真正离开身体
        s.bladder += urine_ml

        # ---- 不显性失水：皮肤蒸发 + 呼吸 + 粪便 ----
        # 生理值约 0.95 L/天（皮肤 0.4、呼吸 0.4、粪便 0.15）。
        # 漏掉这一项时总水周转只有 1.08 L/天，口渴几乎触发不到，
        # 三天只喝两次水，与真实生理相去甚远。
        vent = s.rr * s.tidal / 1000.0                      # L/min
        insensible_l_day = 0.40 + 0.40 * (vent / 7.0) + 0.15
        if s.temp > 37.0:                                    # 发热额外蒸发
            insensible_l_day += (s.temp - 37.0) * 0.35
        s.body_water = max(30.0, s.body_water - insensible_l_day * dt / 86400.0)

        # 血容量：由总体水线性映射（TBW 42L <-> 5000 mL 血浆）
        s.blood_vol = clamp(5000.0 * (s.body_water / 42.0), 3800.0, 5600.0)

        # 钠浓度与渗透压：全部由守恒量导出
        s.na = clamp(s.na_mass / s.body_water, 125.0, 160.0)
        # 血浆渗透压标准公式：Posm = 2[Na] + [Glu] + [Urea]
        s.osmol = clamp(2.0 * s.na + s.glucose + 3.0, 265.0, 340.0)

    # ------------------------------------------------------------------
    # 内分泌：RAAS / HPA / 甲状腺 / ADH
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 月经周期（2026-11-14 新增）
    # ------------------------------------------------------------------
    # 依据 LIFE.apk 元数据的两套并列相位枚举：
    #   MenstrualPhase: Follicular / Ovulation / Luteal
    #   UterusPhase:    Proliferative / Secretory （另有 Menstruation）
    # 以及腺体 EstrogenGland / ProgesteroneGland 与激素 Estradiol/Estrone/Estriol。
    # 因此这里保留两条独立相位，而不是压成「周期第 N 天」。
    MENSTRUAL_LENGTH = 5.0      # 行经持续天数

    def set_cycle_anchor(self, anchor) -> None:
        """设置/清除现实日期锚点（date 或 None）。

        换锚点时清掉回绕基准：否则「昨天填的是上月，今天改成上周」会让
        day 突然倒退，被误判成跨过了周期末，凭空发一条「新一轮开始」。
        """
        if anchor == self.cycle_anchor_date:
            return
        self.cycle_anchor_date = anchor
        self._cycle_skip_wrap_once = True

    @staticmethod
    def _calendar_cycle_raw(anchor) -> float:
        """(今天 - 上次月经首日) 的天数，含当天已过的小数部分。

        以本地 00:00 为日界：一天之内相位平滑推进，跨天时正好落在整日上。
        DST 那天的 23/25 小时忽略不计（最多差一小时，对相位没意义）。
        日期在未来（用户填错）时按 0 处理，等价于「今天刚开始」。
        """
        now = _datetime.now()
        frac = (now.hour * 3600 + now.minute * 60 + now.second
                + now.microsecond / 1e6) / 86400.0
        raw = float((now.date() - anchor).days) + frac
        return raw if raw >= 0.0 else 0.0

    def _menstrual(self, dt):
        """推进月经双相位机，并派生雌激素/孕激素。

        总开关关闭时直接返回并把相关量清零：不推进周期、不产生激素、
        不会有痛经与 PMS，界面也不显示。开关在引擎上（cycle_enabled），
        由插件从配置注入。
        """
        s = self.s
        if not self.cycle_enabled:
            # 关闭态：保证这些量恒为中性，避免残留值影响体温/压力公式
            s.menstrual_phase = ""
            s.uterus_phase = ""
            s.menstruating = False
            s.menstrual_flow = 0.0
            s.estrogen = 0.0
            s.progesterone = 0.0
            s.prostaglandin = 0.0
            s.pms_load = 0.0
            s.follicle_maturing = False
            s.luteal_maturing = False
            return

        anchor = getattr(self, "cycle_anchor_date", None)
        if not s.menarche_done:
            s.menarche_done = True
            s.cycle_length = float(self.cycle_length_days)
            if anchor is None:
                # 旧行为：新身体从行经结束后起步（day 6），不立刻来月经。
                # 有现实锚点时不做这个猜测 —— 相位完全由日历决定。
                s.menstrual_day = 6.0

        L = max(18.0, float(s.cycle_length))
        prev_day = float(s.menstrual_day or 0.0)
        if anchor is not None:
            # 现实日历锚点：第几天直接由「今天 - 上次月经首日」算出。
            # 注意不能用累加：累加的零点是"启用那一刻"，与日历无关，
            # 而且掉帧/离线截断会让它一路滞后。
            raw = self._calendar_cycle_raw(anchor)
        else:
            raw = prev_day + dt / (24.0 * H2S)
        day = raw % L
        s.menstrual_day = day
        s.cycle_length = L

        # ---- 相位判定 ----
        # 行经：cycle 起点后 0~5 天
        # 卵泡期：5 天 ~ 排卵前（约 L*0.5）
        # 排卵：一个窄窗口（约 1 天）
        # 黄体期：排卵后 ~ 周期结束
        ovulation_day = L * 0.5
        if day < self.MENSTRUAL_LENGTH:
            uterus = "menstrual"
            menstr = "follicular"   # 行经仍属卵泡期总体（激素上 E2 在上升）
            flowing = True
            flow = 1.0 - day / self.MENSTRUAL_LENGTH
        elif day < ovulation_day - 1.0:
            uterus = "proliferative"
            menstr = "follicular"
            flowing = False
            flow = 0.0
        elif day < ovulation_day + 1.0:
            uterus = "proliferative"
            menstr = "ovulation"
            flowing = False
            flow = 0.0
        else:
            uterus = "secretory"
            menstr = "luteal"
            flowing = False
            flow = 0.0

        s.menstrual_phase = menstr
        s.uterus_phase = uterus
        s.menstruating = flowing
        s.menstrual_flow = clamp(flow, 0.0, 1.0)

        # ---- 雌激素 / 孕激素曲线 ----
        # 雌激素：卵泡期逐渐升高，排卵前达峰，黄体期次峰后回落
        # 孕激素：排卵前接近 0，黄体期升到高位，行经前骤降
        if menstr == "follicular":
            # 行经期低，随后单调上升
            e2 = 0.15 + 0.75 * clamp((day - self.MENSTRUAL_LENGTH) /
                                     max(1.0, ovulation_day - 1.0 - self.MENSTRUAL_LENGTH),
                                     0.0, 1.0)
        elif menstr == "ovulation":
            e2 = 1.0          # 排卵前峰值
        else:
            # 黄体期为次峰，向周期末回落
            tail = clamp((day - ovulation_day) / max(1.0, L - ovulation_day), 0.0, 1.0)
            e2 = 0.65 * (1.0 - 0.55 * tail)

        if menstr in ("follicular", "ovulation"):
            p4 = 0.05
        else:
            p4 = 0.20 + 0.80 * clamp((day - ovulation_day) / max(1.0, L - ovulation_day),
                                     0.0, 1.0)

        s.estrogen = approach_tau(s.estrogen, e2, dt, 3.0 * H2S)
        s.progesterone = approach_tau(s.progesterone, p4, dt, 3.0 * H2S)

        # 卵泡成熟 / 黄体成熟（对应 APK 的两个 Maturation 枚举）
        s.follicle_maturing = menstr == "follicular" and day > self.MENSTRUAL_LENGTH
        s.luteal_maturing = menstr == "luteal" and p4 > 0.3

        # ---- 前列腺素（APK: ProstaglandinF2 / ProstaglandinD2）----
        # 行经期子宫内膜脱落释放前列腺素，峰值在行经第 1~2 天。
        # 它不只致痛，还引起子宫收缩、恶心、腹泻、血管扩张。
        pgn_t = 0.0
        if menstr == "follicular" and flowing:
            # 行经前 2 天最高，之后衰减
            pgn_t = clamp(1.0 - (day - 1.0) / max(1.0, self.MENSTRUAL_LENGTH - 1.0), 0.15, 1.0)
        elif menstr == "luteal":
            # 黄体末期（行经前 2~3 天）前列腺素开始上升，这是 PMS 的前驱
            pre = clamp((day - (L - 3.0)) / 3.0, 0.0, 1.0)
            pgn_t = 0.35 * pre
        s.prostaglandin = approach_tau(s.prostaglandin, pgn_t, dt, 4.0 * H2S)

        # ---- PMS（经期综合征）----
        # 黄体后期孕激素高位后回落、雌激素同时下降，触发情绪低落与体感不适。
        # 真实机制涉及 5-HT 能系统，这里简化为"黄体后期 + 激素双降"的负荷。
        if menstr == "luteal":
            pre = clamp((day - (L - 7.0)) / 7.0, 0.0, 1.0)
            s.pms_load = clamp(pre * (0.5 + 0.5 * clamp(1.0 - s.estrogen, 0.0, 1.0)),
                               0.0, 1.0)
        else:
            s.pms_load = approach_tau(s.pms_load, 0.0, dt, 6.0 * H2S)

        # 周期回绕：day 由大跳到小即进入新一轮行经。
        # 旧写法判断 day >= L，但锚定模式下 day 是取模结果、永远小于 L，
        # 那个条件再也不成立 —— 所以改成「比上一步明显倒退」。
        # 回绕那一拍直接切到行经第 0 天，否则仍显示 luteal/secretory
        # （实测采样点看到过这个现象）。
        # 基准取「上一步写进 state 的 day」而不是私有缓存：首次调用也要能判，
        # 否则从存档恢复时（存档里是第 27.5 天）第一次就漏发新一轮事件。
        skip = bool(getattr(self, "_cycle_skip_wrap_once", False))
        self._cycle_skip_wrap_once = False
        if not skip and day < prev_day - 0.5:
            if anchor is None:
                # 累加模式：把累积值归零，等价于旧行为
                s.menstrual_day = 0.0
            # 锚定模式保留日历算出的小数（例如第 0.87 天），
            # 归零会丢到当天已过的进度
            s.menstrual_phase = "follicular"
            s.uterus_phase = "menstrual"
            s.menstruating = True
            s.menstrual_flow = 1.0
            self.bus.emit(Event(s.t, "menstrual_cycle", "info", "新一轮周期开始", {}))

    def _endocrine(self, dt):
        s = self.s
        dif = (s.osmol - 290.0)

        # ---- RAAS ----
        renin_t = clamp(1.0 + (1.0 - s.blood_vol / 5000.0) * 3.0 + (90.0 - s.map) / 40.0, 0.2, 4.0)
        s.renin = approach(s.renin, renin_t, dt, "raas")
        s.ang2 = approach(s.ang2, clamp(s.renin * 1.2, 0.2, 3.0), dt, "ang2")
        s.aldosterone = approach(s.aldosterone, clamp(s.ang2 * 0.8, 0.0, 3.0), dt, "aldo")

        # 醛固酮保钠的「回收」动作已移入 _renal 里与排钠同处计算，
        # 因为需要用到当步的 na_excrete 才能守恒（钠只能从尿里省，不能凭空造）。
        # 这里只保留说明，不做任何加减。

        # ---- ADH ----
        # 容量抑制：血容量偏高时 ADH 下降（水负荷抑制），这是打破
        # 「高钠 -> 高 ADH -> 少尿 -> 钠更积」正反馈的关键
        vol_suppress = clamp(5000.0 / max(s.blood_vol, 3800.0), 0.55, 2.0)
        adh_t = clamp((1.0 + dif * 0.15) * vol_suppress, 0.05, 4.0)
        # 酒精抑制 ADH 释放（渗透压与容量调节之外的独立通路）：
        # 这是"喝酒利尿"的直接原因 —— 三罐啤酒能多排出 500-800 mL 自由水。
        if s.bac > 0.0:
            adh_t = clamp(adh_t * (1.0 - 0.62 * clamp(s.bac / 1.0, 0.0, 1.0)), 0.03, 4.0)
        s.adh = approach(s.adh, adh_t, dt, "adh")

        # ---- HPA ----
        cort_t = clamp(0.5 + 0.09 * s.circ + 1.20 * (s.stress / 100.0)
                       + 0.30 * max(0.0, s.glucose - 8.0) * 0.2, 0.10, 3.0)
        s.cortisol = approach(s.cortisol, cort_t, dt, "cortisol")
        s.acth = approach(s.acth, clamp(s.cortisol * 0.8, 0.05, 2.0), dt, "acth")

        # 皮质醇升高血糖
        s.glucose += 0.004 * max(0.0, s.cortisol - 0.6) * dt / 60.0

        # ---- 肾上腺素 ----
        adr_t = clamp(0.15 + 0.6 * (s.stress / 100.0) + (0.5 if s.exercising else 0.0), 0.05, 1.5)
        s.adrenaline = approach_tau(s.adrenaline, adr_t, dt, 30.0)

        # ---- 甲状腺 ----
        s.tsh = approach(s.tsh, 1.0, dt, "thyroid")
        s.t3 = approach(s.t3, clamp(1.0 + 0.15 * s.circ, 0.7, 1.4), dt, "thyroid")

    # ------------------------------------------------------------------
    # 体温调节
    # ------------------------------------------------------------------
    def _thermo(self, dt):
        s = self.s
        base = 36.8 + 0.35 * s.circ - 0.30 * s.adrenaline

        # ---- 室外环境对核心体温的影响（2026-11-14 新增）----
        # 旧实现完全没有环境项：零下十度和三十五度对身体来说完全一样。
        # 真实机制是热平衡：冷时血管收缩 + 战栗产热，热时血管扩张 + 出汗散热。
        # 这里只做「核心体温的轻微位移」，因为人体有很强的恒温能力 ——
        # 环境极端时核心体温也只偏移零点几度，真正剧烈变化的是体感与出汗量。
        # env_load 是 0~1 的冷热压迫；室内要打折（现代住宅保温良好）。
        eff_load = clamp(float(getattr(s, "env_load", 0.0) or 0.0), 0.0, 1.0)
        indoor_temp = getattr(s, "env_indoor_temp_c", None)
        if bool(getattr(s, "env_indoor", True)):
            if indoor_temp is not None:
                # 预留接口已接入：有真实室内温度时，用它重新算压迫
                # 空调房 26℃ 和暖气房 18℃ 不该是同一种体感
                from . import weather as _wx
                _hum = getattr(s, "env_indoor_humidity", None)
                eff_load = _wx.outdoor_load(
                    float(indoor_temp),
                    float(_hum) if _hum is not None else 50.0,
                    0.0,   # 室内无风
                )
            else:
                # 未接入：沿用旧逻辑，室外压迫打 25 折
                eff_load *= 0.25
        if eff_load > 0.0:
            feels = float(getattr(s, "env_feels_c", 22.0) or 22.0)
            if feels < 18.0:
                # 冷：核心体温下移，最多 0.4℃（暴露在严寒里）
                base -= 0.40 * eff_load
            elif feels > 24.0:
                # 热：核心体温上移，最多 0.35℃
                base += 0.35 * eff_load
        # 月经周期对基础体温的影响（真实且显著，是临床判断排卵的依据）：
        # 排卵后孕激素升高使基础体温上移 0.3~0.5℃，行经前回落。
        # 依据 APK 元数据 ProgesteroneGland / Thermometer。
        base += 0.40 * clamp(s.progesterone / 1.0, 0.0, 1.0)
        # 前列腺素 PGE2 是内源性致热原：经期可致低热（0.1~0.3℃）
        base += 0.25 * clamp(s.prostaglandin, 0.0, 1.0)
        if s.exercising:
            base += 0.8
        # 睡眠期核心体温下移 0.3~0.5 ℃，是昼夜曲线的低谷成因之一
        if s.sleep_depth > 0.0:
            base -= 0.30 * min(1.0, s.sleep_depth)
        # 酒精：皮肤血管扩张 + 散热增加，核心体温小幅下降（真人 0.2-0.5 ℃）；
        # 咖啡因：腺苷阻断使代谢率略升，体温小幅上行。
        if s.bac > 0.0:
            base -= 0.30 * clamp(s.bac / 1.0, 0.0, 1.0)
        if s.caffeine > 0.0:
            base += 0.12 * clamp(s.caffeine / 200.0, 0.0, 1.0)
        if self.link_activity > 0.0:
            base += 0.15 * min(1.0, self.link_activity)
        s.temp = approach(s.temp, base, dt, "temp")

        # 出汗与战栗
        # 旧阈值 37.0 ℃：正常人体温本就 36.8-37.1，于是整个白天 sweat≈0.16，
        # 按 0.8 L/h 折算每天凭空蒸发 3 L —— 实测不显性失水 1.5-2.7 L/天
        # （生理值 0.95 L/天），这是把人渴坏的直接原因之一。
        # 真实出汗阈值在 37.5 ℃ 以上（核心温度升高 1 ℃ 才明显出汗）。
        # 出汗与战栗：除了核心体温，还受室外环境影响。
        # 炎热时即使核心体温没升多少，皮肤温度高、汗腺被直接激活；
        # 寒冷时同理。这一点用 eff_load 叠加，系数控制在不会失控的范围。
        heat_env = eff_load if float(getattr(s, "env_feels_c", 22.0) or 22.0) > 24.0 else 0.0
        cold_env = eff_load if float(getattr(s, "env_feels_c", 22.0) or 22.0) < 18.0 else 0.0
        s.sweat = clamp((s.temp - 37.5) * 2.0 + 0.55 * heat_env, 0.0, 1.0)
        s.shiver = clamp((36.5 - s.temp) * 3.0 + 0.60 * cold_env, 0.0, 1.0)
        if s.sweat > 0.1:
            lost = s.sweat * 0.8 * dt / 1000.0          # L
            # 34.0 的硬底相当于体重 42 L 的 81%（失水 19%），真人此时已死亡多时。
            # 改为 30.0（28.6%）并让 _health 的脱水惩罚接管 —— 不再假装「没有底」，
            # 而是让失水真的变成伤害，而不是一个默默截断的常数。
            s.body_water = max(30.0, s.body_water - lost)
            s.na_mass = max(4500.0, s.na_mass - lost * 50.0)  # 汗钠约 50 mmol/L

    # ------------------------------------------------------------------
    # 体感层：从生理量投影出来
    # ------------------------------------------------------------------
    def _affect(self, dt):
        s = self.s
        fasted_h = (s.t - s.last_meal_t) / H2S
        drank_h = (s.t - s.last_drink_t) / H2S

        # 饥饿：三通道合成，缺一不可
        #   (a) 时间通道 —— 距上一餐的小时数，指数趋近 100。这是最基础的驱动。
        #   (b) 血糖通道 —— 低血糖是最强的饥饿信号（APK 有 BloodSugarLevel /
        #       GetBloodSugarLevel / Ghrelin / CNS_Appetite）。旧版完全没有这一项，
        #       实测血糖钉在 2.5 与 20 时饥饿值一模一样，是硬缺陷。
        #   (c) 胃扩张通道 —— 用乘法抑制，不是加法。吃饱了当场就不饿，
        #       但胃排空后旧值立刻回来（APK 的 GastricDistention / ReceptiveRelaxation）。
        #
        # (a) 与 (b) 用互补合成（见下方 h_core），不是相加也不是 max：
        # 相加会双重计数，max 会让弱通道永远失效（空闲 8h 时 a 恒大于 b）。
        # 2026-11-11 重构：饥饿改由胃肠激素驱动，不再是"时间 + 血糖"两个量
        # 的合成。依据 LIFE.apk 的腺体族结构（GastrinGland / CholecystokininGland /
        # GIPGLand / PYYGland / DuodenumGland / JejunumGland 全部存在，而
        # Hunger/Satiety/Fullness 是症状枚举而非标量字段）—— 原版的饥饿就是
        # 由 ghrelin（胃饥饿素，促食欲）与 PYY/GLP-1/CCK/GIP + 胃扩张
        # （抑食欲）相互对抗算出来的。旧实现里高血糖对饥饿的抑制上限只有
        # 15%、胃扩张只有 45%，于是出现"血糖 8.8 还觉得饿"的观感。
        # 2026-11-11：加 3 小时免计窗口 + 时间常数 8 -> 9 h。
        # 原式从吃完那一刻就开始按 8 h 常数爬，餐后 1.7 h 就给 19 分驱动，
        # 于是出现用户反馈的「胃还没消化完就开始饿」。真实人的时间性饥饿
        # 在餐后 3~4 h 内基本不参与（这段由肠道激素接管）。
        h_fast = 100.0 * (1.0 - math.exp(-max(0.0, fasted_h - 3.0) / 9.0))
        # 低血糖 = 饥饿驱动（正向）。3.0 mmol/L -> 36，2.5 -> 45。
        h_glu = clamp((5.0 - s.glucose) * 18.0, 0.0, 100.0)
        # (a)(b) 互补合成：血糖正常时 b=0，血糖崩到 2.5 时把饥饿抬上去。
        h_core = 100.0 - (100.0 - h_fast) * (100.0 - h_glu) / 100.0

        # 饥饿驱动 = 胃饥饿素为主，时间/血糖作兜底（两者取大）。
        # ghrelin 满值 1.2 对应 100 分饥饿，所以乘 82 留出余量。
        drive = max(s.ghrelin * 82.0, h_core)

        # 饱腹抑制：五条肠道信号相加，再整体限幅。
        #   gut_full 胃扩张（GastricDistention，机械信号，最快）
        #   pyy      回肠刹车（脂肪/蛋白到达回肠，最持久）
        #   glp1     GLP-1（回肠 + 血糖，兼促胰岛素）
        #   cck      缩胆囊素（脂肪/蛋白，抑制胃排空）
        #   gip      抑胃肽（碳水，K 细胞）
        # 限幅 0.88 而不是 0.60：吃饱后饥饿该接近 0，而不是留 40% 的尾巴。
        #   gut_load 小肠营养负荷：胃已排空但食糜还在十二指肠/空肠时，
        #            真实的人依然不饿（营养尚未吸收完）。旧实现只看胃体积，
        #            胃一空 satiety 就塌，于是出现"还没消化完就饿"的观感。
        gut_load = clamp((s.duodenum_volume + s.jejunum_volume) / 500.0, 0.0, 1.0)
        satiety = (0.36 * clamp(s.gut_full, 0.0, 1.0)
                   + 0.20 * gut_load
                   + 0.26 * clamp(s.pyy, 0.0, 1.6)
                   + 0.18 * clamp(s.glp1, 0.0, 1.6)
                   + 0.10 * clamp(s.cck, 0.0, 1.6)
                   + 0.08 * clamp(s.gip, 0.0, 1.6)
                   + clamp((s.glucose - 11.0) * 0.012, 0.0, 0.12))
        s.hunger = clamp(drive * (1.0 - min(satiety, 0.88)), 0.0, 100.0)
        # 口渴双通道，缺一不可：
        #   (a) 渗透性口渴 —— 感知血浆渗透压
        #   (b) 容量性口渴 —— 感知血容量/血管紧张素 II。
        # 只有 (a) 时，等渗失水（尿同时带走水与钠）不会触发口渴，
        # 30 天长跑血容量一路漏到下限，血压随之塌陷。
        thirst_osm = clamp((s.osmol - 288.0) * 12.0, 0.0, 100.0)
        thirst_vol = clamp((5000.0 - s.blood_vol) * 0.14 + max(0.0, s.ang2 - 1.0) * 25.0,
                           0.0, 100.0)
        thirst_dry = clamp(100.0 * (1.0 - math.exp(-drank_h / 14.0)), 0.0, 100.0)
        s.thirst = clamp(0.55 * thirst_osm + 0.30 * thirst_vol
                         + 0.10 * thirst_dry + 0.05 * s.bladder / 5.0, 0.0, 100.0)

        # 精力
        # 精力 = ATP 储备 + 无饥饿/无口渴。早前含「睡眠债」项，但 APK 元数据里
        # 没有睡眠债这个量（Sleep 仅为症状枚举之一），该项已删除。
        s.energy = clamp(100.0 * (0.45 * s.atp
                                  + 0.35 * (1.0 - s.hunger / 100.0)
                                  + 0.20 * (1.0 - s.thirst / 100.0)), 0.0, 100.0)

        # ---- 延迟性肌肉酸痛 DOMS（2026-11-14 新增）----
        # 这**不是**乳酸造成的：乳酸几小时就清除，而 DOMS 峰值在运动后 48-72h。
        # 机制是肌纤维微损伤 + 局部炎症反应（Hotfiel 2018 [R4]）。
        # 曲线按三段建模：
        #   0 ~ 24h   逐渐上升（炎症启动）
        #   24 ~ 72h  达峰并平台（"峰值在 48-72 小时"）
        #   > 72h     线性衰减，约 5-7 天完全消退
        doms_now = 0.0
        if s.doms_from_exercise > 0.0 and s.doms_peak_at > 0.0:
            # doms_peak_at = 运动结束 + 48h（未来时刻）。
            # 运动结束时刻 = doms_peak_at - 48h，所以"运动后小时数"：
            #   since_h = (now - (peak_at - 48h)) / 1h
            #           = (now - peak_at)/1h + 48
            # 旧实现少了这个减法，刚触发时 since_h = 0 直接落进 <=0 分支归零，
            # 导致 DOMS 永远算不出来（实测事件发了但 doms 恒为 0）。
            since_h = (s.t - (s.doms_peak_at - 48.0 * H2S)) / H2S
            peak = s.doms_from_exercise
            if since_h <= 0.0:
                doms_now = 0.0
            elif since_h < 24.0:
                doms_now = peak * (since_h / 24.0) * 0.75      # 上升段
            elif since_h < 72.0:
                # 24-72h 平台，48h 附近最高
                t_rel = (since_h - 24.0) / 48.0
                doms_now = peak * (0.75 + 0.25 * math.sin(math.pi * t_rel))
            else:
                # 72h 后线性消退，到 168h（7 天）归零
                fade = clamp(1.0 - (since_h - 72.0) / 96.0, 0.0, 1.0)
                doms_now = peak * fade
            # 只在「已经过了消退期」才清空状态。
            # 2026-11-14 修：旧条件写的是 doms_now <= 0.1，于是上升段刚开始
            # （since_h 才几分钟，doms_now 算出来 0.03）就被判定为"没了"，
            # 第一次 step 就把 doms_from_exercise 清零 —— DOMS 永远长不出来。
            if since_h >= 168.0:
                s.doms_from_exercise = 0.0
                s.doms_peak_at = 0.0
                doms_now = 0.0
        s.doms = clamp(doms_now, 0.0, 100.0)

        # ---- 疼痛（2026-11-14 新增：此前 pain 恒为 0，是个死字段）----
        # 旧实现全仓只有一处 `s.pain = max(0.0, s.pain * (1-k))`，
        # 而且那处在 _treat() 里 —— 只在住院时执行。于是平时 pain 永远是 0，
        # FEELING_LEVELS["pain"] 五档从不触发，stress/health 里乘的都是 0。
        #
        # 现在的唯一疼痛源是痛经，依据是 APK 元数据里的 ProstaglandinF2
        # （前列腺素 F2α，子宫内膜脱落的关键介质，也是临床引产/痛经的直接成因）。
        # 疼痛强度由三件事决定：
        #   (a) 经血量 menstrual_flow —— 流量越大前列腺素越多
        #   (b) 孕激素回落幅度 —— 黄体期末 P4 骤降触发前列腺素合成（真实机制）
        #   (c) 雌激素水平 —— E2 高的周期前列腺素受体更敏感
        pain_menstrual = 0.0
        if self.cycle_enabled and self.cycle_pain_enabled and s.menstruating and s.menstrual_flow > 0.0:
            # 前列腺素是痛经的直接介质（APK: ProstaglandinF2）。
            # 真实机制：PGF2α 引起子宫平滑肌强烈收缩 + 局部缺血，同时提高
            # 痛觉神经末梢敏感性。雌激素高时受体更敏感，故乘 e2_sens。
            e2_sens = 0.6 + 0.4 * clamp(s.estrogen, 0.0, 1.0)
            pain_menstrual = clamp(80.0 * s.prostaglandin * e2_sens, 0.0, 85.0)

        # ---- 内源性镇痛（β-内啡肽）----
        # 持续疼痛会触发内源性镇痛：痛得越久、越强，内啡肽释放越多，
        # 实际痛感被"打折"。这是"痛久了反而麻木"的生理基础。
        endo_t = clamp(0.1 + s.pain / 100.0 * 0.8, 0.0, 1.0)
        s.endorphin = approach_tau(s.endorphin, endo_t, dt, 30.0 * 60.0)

        # 疼痛合成：痛经 + DOMS，取较大者而非相加。
        # 理由：两种疼痛的神经通路有重叠，简单相加会重复计数；
        # 实际体验中"又痛经又酸痛"确实不会痛到两倍。
        pain_total = max(pain_menstrual, s.doms)
        # 内啡肽镇痛对两种疼痛都有效
        perceived = clamp(pain_total * (1.0 - 0.45 * s.endorphin), 0.0, 100.0)
        s.pain = approach_tau(s.pain, perceived, dt, 20.0 * 60.0)

        # 压力
        stress_t = clamp(s.cortisol * 32.0
                         + max(0.0, s.thirst - 50.0) * 0.30
                         + max(0.0, s.hunger - 70.0) * 0.35
                         + max(0.0, s.pain) * 0.5
                         # PMS：黄体后期激素双降引起的情绪负荷（5-HT 能紊乱的简化）
                         + s.pms_load * 18.0, 0.0, 100.0)
        s.stress = approach(s.stress, stress_t, dt, "affect")

        # 体感温度（50 为舒适）。
        # 2026-11-14：加入室外环境项。旧公式只看核心体温/出汗/战栗，
        # 而核心体温的恒温能力很强，导致"外面 38℃ 和 22℃ 体感一样"。
        # 真实体感主要由皮肤温度与环境热交换决定，这里用 env_feels_c 直接投影：
        #   体感 30℃ 以上 → 偏热（每度约 2 分）
        #   体感 15℃ 以下 → 偏冷（每度约 2 分）
        # 室内时按 env_indoor 衰减，因为室内有空调/暖气。
        env_thermal = 0.0
        _feels = float(getattr(s, "env_feels_c", 22.0) or 22.0)
        _indoor = bool(getattr(s, "env_indoor", True))
        _in_temp = getattr(s, "env_indoor_temp_c", None)
        if _indoor and _in_temp is not None:
            # 预留接口已接入：室内有真实温度时直接用它，不再用室外值打折。
            # 否则空调 26℃ 和暖气 18℃ 会算出同一个体感（实测都是 65.0）。
            from . import weather as _wx2
            _in_hum = getattr(s, "env_indoor_humidity", None)
            _feels = _wx2.feels_like(float(_in_temp),
                                     float(_in_hum) if _in_hum is not None else 50.0,
                                     0.0)
            _w = 1.0
        else:
            _w = 0.30 if _indoor else 1.0     # 室内只按三成计入
        if _feels > 28.0:
            env_thermal += min(35.0, (_feels - 28.0) * 2.2) * _w
        elif _feels < 15.0:
            env_thermal -= min(35.0, (15.0 - _feels) * 2.0) * _w
        s.thermal = clamp(
            50.0 + (s.temp - 36.8) * 18.0 - s.sweat * 20.0 + s.shiver * -25.0
            + env_thermal,
            0.0, 100.0,
        )

        # 低落（APK 症状枚举 Depression 的直接对应物）。
        # 口径：持续的应激负荷累积 + 当前压力水平，无社会/睡眠输入 —— 那些量
        # APK 里查无依据。stress_load 是应激对情绪的累积积分，τ=6h 缓慢消退。
        s.stress_load += (max(0.0, s.stress - 50.0) * dt / H2S
                          - s.stress_load * dt / (6.0 * H2S) * 6.0)
        s.stress_load = clamp(s.stress_load, 0.0, 100.0)
        dep_t = clamp(2.0 + 0.9 * s.stress_load + 0.20 * max(0.0, s.stress - 60.0), 0.0, 100.0)
        s.depression = approach(s.depression, dep_t, dt, "affect")

    # ------------------------------------------------------------------
    # 健康与死亡
    # ------------------------------------------------------------------
    def _health(self, dt):
        s = self.s
        pen = 0.0
        pen += max(0.0, 3.0 - s.glucose) * 6.0
        pen += max(0.0, s.glucose - 15.0) * 1.2
        pen += max(0.0, 88.0 - s.sao2) * 0.8
        pen += max(0.0, s.map - 160.0) * 0.15
        pen += max(0.0, 55.0 - s.map) * 0.35
        pen += max(0.0, s.temp - 39.5) * 3.0
        pen += max(0.0, 35.0 - s.temp) * 3.0
        # 疼痛对健康的扣分：痛经属功能性疼痛，不造成组织损伤，故不计入健康惩罚。
        # 演变过程：旧系数 0.05 会让 85 分痛经扣 4.25 健康/步，第 2 天就跌破
        # 急救阈值 25 被送医；改 0.008 后虽不再住院，但健康仍在经期 5 天里
        # 从 100 掉到 68（恢复速率仅 0.6/h），相当于"来次月经虚弱一周"，不符合实际。
        # 疼痛对说话的观感已经由 stress 通路承担（stress_t 含 pain*0.5），
        # 这里不再重复计入健康。器质性疼痛（创伤/感染）留给后续 injury 通路单独计。
        # pen += s.pain * 0.0  ← 保留占位说明，不参与计算

        # ---- 缺失过程的后果（2026-11-08 新增）----
        # 旧版只有 7 项惩罚，且没有任何一项与「摄入」挂钩：实测 30 天零摄入
        # 健康仍为 100、alive 恒 True，7 天断水血钠 156.8/渗透压 322 也毫发无伤。
        # 真人脱水超过体重 10% 就进入低血容量休克，禁食 30 天（有水）虽可存活
        # 但会严重虚弱。这里补四条，让「不喝不吃」产生可观测代价。
        #
        # 1) 脱水：以体重 42 L 体液为基准，失水比例 >5% 起算，>12% 危及生命
        # 阈值取 8%：真人失水 5% 只是口渴加重、尿浓缩，8-10% 才出现明显
        # 循环不稳定，12% 以上才休克。取 5% 会让正常一天的波动就扣分。
        dehydration = clamp((42.0 - s.body_water) / 42.0, 0.0, 1.0)
        if dehydration > 0.08:
            pen += (dehydration - 0.08) * 1200.0
        # 2) 高渗：渗透压 >300 起算（正常 285-295）
        pen += max(0.0, s.osmol - 300.0) * 1.6
        # 3) 长期饥饿：空腹超过 24 h 起算，随时间加重（脂肪储备仍在时较缓）
        fasted_h = (s.t - s.last_meal_t) / H2S
        if fasted_h > 24.0:
            pen += min(6.0, (fasted_h - 24.0) / 24.0 * 1.2)
        # 4) 糖原枯竭 + 持续消耗：血糖尚可但储备见底，属应激状态
        if s.glycogen < 20.0:
            pen += (20.0 - s.glycogen) * 0.12

        if pen > 0:
            s.health = clamp(s.health - pen * dt / H2S, 0.0, 100.0)
        else:
            s.health = clamp(s.health + 0.6 * dt / H2S, 0.0, 100.0)

        # 本建模不设死亡：health 归零只把她钉在濒死线上，绝不做 alive=False。
        # （上一版在这里置 alive=False 并把引擎停摆，导致死亡后身体仍然推进，
        #   既不是活人也不是尸体。现状是"永远活着，但可以病得很重"。）
        s.alive = True

    # ------------------------------------------------------------------
    # 濒死 -> 送医急救（可关）
    # ------------------------------------------------------------------
    def _rescue(self, dt):
        s = self.s

        if s.hospital_until > s.t:
            self._treat(dt)
            return

        # 出院瞬间：结算一次，并把急救"待发"状态复位。
        # 复位后必须等健康重新爬回阈值之上才算重新上膛 —— 否则若把
        # rescue_threshold 配得比 restore_health 还高（如阈值 95、恢复 74），
        # 出院当帧就会再次判濒死，形成"出院-送医"无限循环。
        if s.hospital_until > 0.0 and s.hospital_until <= s.t:
            s.hospital_until = 0.0
            s.hospital_reason = ""
            s.critical = False
            s.critical_since = 0.0
            self._rescue_armed = False
            return

        if not self.rescue_enabled:
            # 开关关掉：允许在濒死线上长期徘徊，但不会死、也不送医
            s.critical = False
            s.critical_since = 0.0
            return

        if s.health > self.rescue_threshold:
            self._rescue_armed = True

        dying = s.health <= self.rescue_threshold and self._rescue_armed
        if not dying:
            s.critical = False
            s.critical_since = 0.0
            return

        if not s.critical:
            s.critical = True
            s.critical_since = s.t
            self.bus.emit(Event(s.t, "critical", "critical",
                                f"生命体征不稳（健康 {s.health:.0f}/100），观察中",
                                {"health": round(s.health, 1)}))
            if self.rescue_critical_seconds > 0.0:
                return

        if s.t - s.critical_since < self.rescue_critical_seconds:
            return

        # 濒死持续够久 —— 送医
        s.critical = False
        s.critical_since = 0.0
        s.hospital_visits += 1
        s.hospital_since = s.t
        s.hospital_until = s.t + max(0.0, self.rescue_stay_hours) * H2S
        s.hospital_reason = self._rescue_reason()
        self.bus.emit(Event(s.t, "hospital", "critical",
                            f"送医急救：{s.hospital_reason}（住院 {self.rescue_stay_hours:.1f} 小时）",
                            {"visits": s.hospital_visits,
                             "stay_hours": round(self.rescue_stay_hours, 2),
                             "health": round(s.health, 1)}))

    def _rescue_reason(self) -> str:
        """按最严重的异常给一个可读的送医理由。"""

        s = self.s
        cands = [
            (max(0.0, 3.0 - s.glucose) * 6.0, f"严重低血糖 {s.glucose:.1f} mmol/L"),
            (max(0.0, s.glucose - 15.0) * 1.2, f"高血糖危象 {s.glucose:.1f} mmol/L"),
            (max(0.0, 88.0 - s.sao2) * 0.8, f"低氧血症 {s.sao2:.0f}%"),
            (max(0.0, 55.0 - s.map) * 0.35, f"循环衰竭 MAP {s.map:.0f}"),
            (max(0.0, s.map - 160.0) * 0.15, f"高血压危象 MAP {s.map:.0f}"),
            (max(0.0, s.temp - 39.5) * 3.0, f"超高热 {s.temp:.1f} ℃"),
            (max(0.0, 35.0 - s.temp) * 3.0, f"重度失温 {s.temp:.1f} ℃"),
            (s.pain * 0.05, f"剧痛 {s.pain:.0f}/100"),
        ]
        cands.sort(reverse=True)
        if cands and cands[0][0] > 0.0:
            return cands[0][1]

        # 单项都没超阈值却已经垮了：长期消耗下的综合衰竭。
        # 这里只报健康值本身，不拿"精力 80/100"之类还正常的指标凑理由。
        return f"整体衰竭（健康 {s.health:.0f}/100）"

    def _treat(self, dt):
        """住院期间的支持治疗：把致命项拉回安全区，其余按一阶惯性恢复。"""

        s = self.s
        k = min(1.0, dt / 900.0)   # 15 分钟量级的时间常数

        s.glucose += (5.5 - s.glucose) * k
        s.sao2 = clamp(s.sao2 + (98.0 - s.sao2) * k, 0.0, 100.0)
        s.map += (88.0 - s.map) * k
        s.temp += (36.8 - s.temp) * k
        s.pain = max(0.0, s.pain * (1.0 - k))
        s.paco2 += (40.0 - s.paco2) * k

        # 补液：血容量与渗透压一起拉回
        s.blood_vol += (5000.0 - s.blood_vol) * k
        s.osmol += (295.0 - s.osmol) * k
        s.na += (142.0 - s.na) * k
        s.body_water += (42.0 - s.body_water) * k
        s.na_mass += (6020.0 - s.na_mass) * k

        # 饱腹与精力：住院不吃不喝也要能撑住
        s.gastric_volume = max(s.gastric_volume, 220.0)
        s.hunger = min(s.hunger, 20.0)
        s.thirst = min(s.thirst, 20.0)
        s.energy = max(s.energy, 55.0)

        target = self.rescue_restore_health
        s.health += (target - s.health) * min(1.0, k * 3.0)

    # ------------------------------------------------------------------
    # 告警：只在跨阈值时发一次，避免刷屏
    # ------------------------------------------------------------------
    def _alerts(self):
        s = self.s
        def fire(key, cond, sev, text):
            if cond and not self._last_alert.get(key):
                self._last_alert[key] = True
                self.bus.emit(Event(s.t, "alert", sev, text, {}))
            elif not cond:
                self._last_alert[key] = False

        fire("bp_hi", s.map > 130.0, "warn", f"血压偏高 {s.sbp:.0f}/{s.dbp:.0f}")
        fire("bp_lo", s.map < 60.0, "alert", f"血压偏低 {s.sbp:.0f}/{s.dbp:.0f}")
        fire("hypo", s.glucose < 3.5, "alert", f"低血糖 {s.glucose:.1f} mmol/L")
        fire("hyper", s.glucose > 11.0, "warn", f"高血糖 {s.glucose:.1f} mmol/L")
        fire("hypoxia", s.sao2 < 92.0, "alert", f"血氧低 {s.sao2:.0f}%")
        fire("fever", s.temp > 38.5, "warn", f"发热 {s.temp:.1f} ℃")
        fire("dehydrate", s.osmol > 305.0, "warn", f"脱水 渗透压 {s.osmol:.0f}")
        # 酸碱：pH 与 pCO2/hco3 现在自洽，极端值可以报警了
        fire("acidosis", s.ph < 7.30, "warn", f"酸中毒 pH {s.ph:.2f} pCO2 {s.paco2:.0f}")
        fire("alkalosis", s.ph > 7.48, "warn", f"碱中毒 pH {s.ph:.2f} pCO2 {s.paco2:.0f}")
        # 排便：超过 3 天不排便算便秘（真人 3 次/天到 3 次/周都正常）
        days_since = (s.t - s.last_stool_t) / 86400.0
        fire("constipation", days_since > 3.0, "warn", f"便秘 {days_since:.1f} 天未排便")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def snapshot(self):
        s = self.s
        return {
            "t": s.t, "hour": round(s.hour, 2),
            "hr": s.hr, "sbp": s.sbp, "dbp": s.dbp, "map": s.map, "pp": s.pp, "co": s.co,
            "rr": s.rr, "sao2": s.sao2, "paco2": s.paco2,
            "glucose": s.glucose, "insulin": s.insulin, "atp": s.atp, "lactate": s.lactate,
            "gfr": s.gfr, "na": s.na, "osmol": s.osmol, "blood_vol": s.blood_vol,
            "renin": s.renin, "ang2": s.ang2, "cortisol": s.cortisol, "adrenaline": s.adrenaline,
            "temp": s.temp, "energy": s.energy, "hunger": s.hunger, "thirst": s.thirst,
            "stress": s.stress, "pain": s.pain, "thermal": s.thermal,
            "depression": s.depression,
            # 消化与嗜好品（对应 APK GastricVolume / AbsorbableType / BAC / Caffeine）
            "gastric_volume": s.gastric_volume, "chyme_carb": s.chyme_carb,
            "chyme_protein": s.chyme_protein, "chyme_fat": s.chyme_fat,
            "gut_glucose": s.gut_glucose, "gut_amino": s.gut_amino,
            "bac": s.bac, "caffeine": s.caffeine,
            "meals": s.meals, "drinks": s.drinks, "urinations": s.urinations,
            "stools": s.stools, "ph": s.ph, "hco3": s.hco3,
            "colon_residue": s.colon_residue,
            "last_meal_items": s.last_meal_items, "last_drink_items": s.last_drink_items,
            "alive": s.alive, "health": s.health,
            # 濒死与急救
            "critical": s.critical, "critical_since": s.critical_since,
            "hospital_until": s.hospital_until, "hospital_since": s.hospital_since,
            "hospital_visits": s.hospital_visits, "hospital_reason": s.hospital_reason,
        }