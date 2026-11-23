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
from . import endocrine

# 数值底座已抽到 dynamics.py（2026-11-18 结构规整）。
# 这里 re-export 同名符号，保证 engine 内部 20 余处 _tau()/approach() 调用
# 以及外部可能存在的 engine.TAU 引用都不受影响。
from .dynamics import H2S, TAU, _tau, approach, approach_tau  # noqa: E402

# 时间常数统一登记，便于审阅与调参
_tau("hr", 12.0)   # 仅用于非运动场景的平滑；运动的上升/恢复改用下面两条独立常数
_tau("bp", 20.0)
_tau("rr", 30.0)
_tau("o2", 8.0)
_tau("glucose", 240.0)
_tau("atp", 60.0)
_tau("renal", 60.0)
# 内分泌轴的时间常数不在此处 —— 已迁到 body/endocrine/organs/ 各器官模块，
# 与各自的激素注册表放在一起（单一来源）。此处若再登记会覆盖同样的值，
# 看似无害实则制造两处真相。
_tau("temp", 300.0)
# 发热阈值（2026-11-22）：体温对**其它系统**的影响只在这个温度以上生效。
# 正常核心体温在 36.3~37.2 之间随昼夜与睡眠摆动，那是体温自己的事；
# 若拿这个摆动去驱动心率/CO2/不显性失水，改一处体温标定就会污染全身 ——
# 而且这些通路都很隐蔽（37.0 的失水阈值曾让 1e-5 的水量差值一路积到尿量）。
# 37.5℃ = 临床「低热」起点（37.5~38.3，CDC 把 38.0 以上定义为 fever）。
# 之前取 37.3 是自己拍的，低于文献区间；改成 37.5 后，正常昼夜峰（37.05）
# 依然不受影响，而 37.6 的轻度发热会开始带动心率 —— 与临床一致。
FEVER_C = 37.5
# 睡眠期「整夜缓降」的时间常数（2026-11-22）。核心热惯性是数十分钟尺度，
# 不能拿 5 分钟的体温时间常数去跟睡眠分期 —— 见 _thermo 里的说明。
_tau("temp_sleep", 1800.0)
_tau("affect", 900.0)

# ---- 情绪神经内分泌（2026-11-18 新增）----
# GCR 的 Hill 参数（GCR_KD / GCR_N）已迁到 body/endocrine/organs/adrenal.py，
# 与受体注册表同处（单一来源）。
# 瞬时情绪场的自衰减时间常数（秒）。2026-11-18 由 900 改 360：
# 实测 900 s 时连续同向事件会线性叠加到上限（"+30 晚安"反而比
# "+60 带好吃的"更兴奋），因为上一句还没消退下一句又加上去。
# 360 s（6 min）下，一句话的影响仍能覆盖几轮对话，但不会无限累积。
# 2026-11-22 由 360 改 1200：6 分钟太短，一句"你真好"的影响还没轮到下一轮
# 对话就没了，界面上表现为"只有那一刻有个尖，其余是一条 0 线"。
# 20 分钟仍能覆盖几轮对话，又不至于让连续同向事件一直叠加（360 那次修正要防的
# 就是叠加到上限）。
MOOD_TAU = 1200.0
# 心境底色的时间常数（秒），4 小时。
# 为什么要单独一层：瞬时情绪（mood_valence）是为"这一句话"服务的，用完就该散；
# 而人平时不是一张白纸 —— 睡得好、不痛、精力足时底色偏正，连轴转、痛经、
# 睡债高时底色偏负。此前没有这一层，于是两次对话之间情绪恒为 0。
# 4 小时是按"半天的心境"取的：慢到不会被一句话推动，快到一天内能跟着作息走。
MOOD_TONE_TAU = 14400.0
# 欣快/烦躁的自身时间常数（秒）。2026-11-18 与 MOOD_TAU 分开：
# 实测共用一个 τ=900 时，一句"你真好"只让 euphoria 爬到 7.2，
# 而分档要到 30 才叫"心情不错" —— 等于接上 NLP 也看不出反应。
# 归一化：当场感受该在 3 分钟内基本到位，而不是 15 分钟。
EUPH_TAU = 180.0
# 烦躁的自身时间常数（秒，15 分钟），比欣快慢 5 倍。
# 依据：差异情绪习惯化研究（Berkeley, 2024，"Letting Go of the Negative,
# Holding on to the Positive"）—— 正性情绪消退更快、负性情绪更持久。
# 工程上体现为：同一场不愉快过去后，烦躁还在，欣快已经没了。
DYSPH_TAU = 900.0

# 糖原储存容量上限（g）。2026-11-16 由 500 提到 800：
#   文献：骨骼肌糖原约 400~900 g（随训练与饮食大幅波动）+ 肝糖原 100~120 g，
#   合计在糖负荷后可远超 500 g。旧上限 500 在连续高碳水饮食下会顶格，
#   储存通道关闭后血糖失去主要去路 —— 实测两顿米饭就能冲到 15.8。
GLYCOGEN_MAX = 800.0

# ---- 运动的心率/体温动力学（2026-11-17）----
# 旧实现的问题（实测）：
#   1) 心率用 approach()，它是「线性比例 dt/τ」而不是指数，τ=12 s 意味着
#      12 秒就走到目标 —— 一开跑心率从 70 直接跳到 119，停止后同样 12 秒回落。
#   2) 运动强度完全不分级：中强度与高强度都是 +40 bpm、+0.8 ℃。
#   3) 出汗只扣水，不参与散热，体温没有负反馈 —— 环境热 + 运动 + 黄体期叠加时
#      目标温度可以顶到 38 ℃ 附近，被判成「低烧」。
#
# 文献口径（见 REFERENCES [R19]）：
#   - 运动开始的心率上升以迷走撤退为主，很快（数十秒量级），随后交感激活把
#     稳态推到与强度匹配的水平；
#   - 运动后的心率恢复（HRR）靠迷走再激活，健康成人 1 分钟回落 15~25 bpm，
#     完全恢复需数分钟 —— 与上升**不对称**，这是旧实现完全没有体现的；
#   - 中等强度运动核心温度上升约 0.5~1.0 ℃，剧烈运动可达 38.3~39.4 ℃；
#   - 散热靠出汗蒸发，随核心温度升高而增强，形成负反馈。
HR_TAU_RISE = 20.0        # 运动开始的心率上升时间常数（s）
HR_TAU_FALL = 95.0        # 运动结束的心率恢复时间常数（s）→ HRR1 ≈ 17~20 bpm
HR_MAX_INTERCEPT = 208.0  # Tanaka 最大心率 = 208 − 0.7×年龄
HR_MAX_SLOPE = 0.7
HR_EX_FRAC_MID = 0.60     # 慢跑 = 60% HRmax（Zone 2 下限）
HR_EX_FRAC_HIGH = 0.72    # 快跑 = 72% HRmax
EX_RR_MID = 8.0           # 中强度额外呼吸频率（次/分）
EX_RR_HIGH = 14.0
EX_VCO2_MID = 0.35        # 中强度额外 CO2 产生量（L/min）
EX_VCO2_HIGH = 0.60
EX_TEMP_MID = 0.55        # 中强度产热（℃）
EX_TEMP_HIGH = 1.10       # 高强度产热（℃）
SWEAT_COOL = 0.35         # 出汗蒸发的散热权重（℃，sweat=1 时）

# 葡萄糖有效性（glucose effectiveness，S_G）：不依赖胰岛素的被动摄取。
# 2026-11-16 补（issue #6）：原处置项 0.19*G*(0.55+0.45*I) 在胰岛素饱和后
# 只剩对 G 的一次项，斜率吸不掉肠道的持续灌入。三日高碳水（糖原顶格、
# 储存通道关闭）时血糖与吸收会在 12.9~15.9 上达成平衡，高于健康人实测
# 峰值（约 9~10）。真实机制里还有一条不靠胰岛素的去路 —— 血糖自身的
# 质量作用：组织（肝、脑、肠、红细胞等）按浓度差被动摄取，最小模型
# （Bergman）把它单列为 S_G ≈ 0.01~0.03 /min。
# 这里以「相对静息基线的超出量」计，静息段与低血糖段恒为 0，
# 因此不会像把 0.19 提到 0.24 那样连静息下限一起拉低（那样做实测
# 静息 4.78 -> 4.46，已否决）。
# 系数为工程取值：在既有标定基础上补斜率，不是文献绝对值代入。
GE_SLOPE_DEFAULT = 0.020      # /min，等效 S_G
GE_FLOOR_DEFAULT = 5.0        # mmol/L，低于此值不启用

# 肠道葡萄糖吸收速率上限（mmol/min）。2026-11-16 补（issue #6 方向 3）：
# 吸收是载体介导的（SGLT1/GLUT2），真实人体葡萄糖吸收约 1~1.5 g/min
# （1 g 葡萄糖 = 5.56 mmol）。模型用一级动力学对肠糖池，负荷越大入血越快，
# 无上限时双倍大份能到 1.86 g/min，超出载体能力。
# 常规三餐实测吸收峰值 1.06 g/min，不触限；只有极端大份才会被削。
ABSORB_CAP_MMOL_MIN = 8.33    # = 1.5 g/min

# ---- 糖原满位后的溢出：脂肪合成（de novo lipogenesis, DNL）----
# 2026-11-16 补（issue #6 极限测试的结论）：糖原顶到 800 g 后储存通道关闭，
# 若碳水仍在灌入，模型只能把血糖推到更高来"匹配"吸收速率（实测双倍大份
# 稳态 15.0、改动前 20）。真实人体有第三条出口：糖原满了以后，多余碳水
# 一部分被氧化，一部分经 DNL 转成脂肪 —— 这正是 Acheson 1988 测到的。
#
# 文献（REFERENCES [R18]，Acheson KJ 等, Am J Clin Nutr 1988;48:240-247）：
#   - 糖原可容纳约 500 g 的增益，之后净脂肪合成才开始增加体脂；
#   - 大量碳水过载时，约 475 g 碳水/天 -> 150 g 脂肪/天（比值 3.17:1）；
#   - 该实验中碳水利用率升到 1010±37 g/天，而循环血糖并未异常升高。
#
# 本模型没有脂肪代谢，产出的脂肪按需求直接丢弃（只做转换、不记账），
# 因此这里只关心"从血糖里扣掉多少"，比值仅用于日志与自证。
# 触发条件用「糖原 85% 满位 + 血糖 > 7.0」，两条同时满足才生效 ——
# 正常饮食（糖原 400~650）与全部既有场景因此恒为 0，一字不动。
DNL_CAP_MMOL_MIN = 3.5          # 葡萄糖去路速率上限 ≈ 38 g/h
DNL_GAIN_PER_MMOL = 0.85        # 每高 1 mmol/L 血糖增加的 DNL 速率（mmol/min）
DNL_FLOOR_MMOL = 7.0            # 血糖低于此值不合成脂肪
DNL_TRIGGER_FRAC = 0.85         # 糖原达到 85% 上限时开始
DNL_GLUCOSE_G_PER_FAT_G = 3.17  # Acheson 475 g CHO : 150 g 脂肪


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
        # 年龄：默认 18 岁，由插件按配置写入（见 plugin 的 age 赋值）。
        # 只影响最大心率与运动目标心率；静息基线随年龄轻微上移。
        self.age = 18
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
        # 2026-11-22：日程插件给出「这觉几点结束」时填这里（0..24）。
        # -1 = 没给，按内置窗口醒。只在已经睡着时生效 —— 白天不该被它改窗口。
        self.link_wake_hour = -1.0
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
        # 窗口两端都带当晚的自然变动（见 _night_window），不再是固定的 23:00–07:00
        a, b = self._night_window()
        # 外部（麦织/麦生活）给了"这觉预计几点结束"，且人已经睡着时，以它为准。
        # 只覆盖起床端：入睡仍按窗口，避免白天的临时小憩把整夜窗口改掉。
        _wake = float(getattr(self, "link_wake_hour", -1.0))
        if _wake >= 0.0 and self.s.sleeping:
            b = _wake
        if a > b:                      # 跨零点窗口（默认 23 -> 7）
            return hour >= a or hour < b
        return a <= hour < b

    # 一个 NREM-REM 周期约 90 分钟。整夜前半段深睡占比高、后半段 REM 变多，
    # 这是健康成人睡眠结构的普遍规律（PSG 实测）。
    SLEEP_CYCLE_MIN = 90.0

    # ---- 睡眠节律的自然变动（2026-11-22 新增）----
    # 用户反馈：「晚上睡眠节律是不是有点太规律了」。核对后确认三处都是死值：
    #   · 窗口固定 23:00–07:00 → 每晚入睡/起床分秒不差；
    #   · 周期固定 90 分钟；
    #   · 分期配比只随周期序号线性变化，夜与夜之间完全相同。
    # 于是七夜的分期图像是同一张图复印的 —— 真实成人不是这样：入睡时刻与起床
    # 时刻每晚都有几十分钟的分散，NREM-REM 周期在 70~110 分钟之间浮动，
    # 深睡比例也一夜一个样。
    # 【诚实标注】下面的幅度是**量级估计**，没有找到能直接给出「夜间入睡/起床
    # 分散的 SD」的原始文献（检索到的都是"作息不规律对健康的影响"这类）。
    # 取 ±26min(SD) / ±22min(SD) / 周期 ±12min，是为了让七夜看得出差别，
    # 又不至于把"作息紊乱"当成常态。需要更保守或更散时改这四个系数即可。
    #
    # 实现纪律：**不消耗 self.rng**。那会改变食物选择等既有随机流的抽样顺序，
    # 把一次「睡眠形状」的调整变成全身轨迹重排，回归也就没法解释了。
    # 这里用「夜序号 + 盐」的确定性哈希做伪随机：同一夜永远同一样子，
    # 可复现、可回归、不影响任何其它子系统。
    SLEEP_ONSET_JITTER_MIN = 36.0    # 入睡时刻 ±36 分钟
    SLEEP_WAKE_JITTER_MIN = 40.0     # 起床时刻 ±40 分钟（量级估计，未取得原始文献）
    SLEEP_CYCLE_JITTER_MIN = 12.0    # 周期 90±12 → 78~102 分钟
    SLEEP_LATE_NIGHT_PROB = 0.18     # 约 1/5 的夜晚熬夜（周末效应）
    SLEEP_LATE_NIGHT_MIN = 60.0      # 熬夜最多 60 分钟

    # ---- 影响睡眠结构的三个「外来输入」（2026-11-22 新增）----
    # 咖啡因 [R44]：随机交叉试验（23 人，7 条件）—— 100 mg 即使睡前 4 h 也无显著影响；
    #   400 mg 在睡前 12 h 内就显著推迟入睡，且越近越糟。故取 150 mg 起效阈值、
    #   400 mg 对应 +20 分钟，超额部分线性。
    CAFFEINE_THRESHOLD_MG = 150.0
    CAFFEINE_FULL_MG = 400.0
    CAFFEINE_MAX_DELAY_MIN = 20.0
    # 酒精 [R45]：PSG 研究一致显示「前半夜 REM 被抑制、后半夜反跳」。
    #   【诚实标注】原文没给出可直接取用的抑制百分比，下面 25%/10% 是量级估计。
    ALCOHOL_FULL_BAC = 0.5
    ALCOHOL_REM_SUPPRESS = 0.25
    ALCOHOL_REM_REBOUND = 0.10
    # 运动 [R46]：急性运动小幅加深慢波（一项研究 +33%，meta 分析为"小效应"；
    #   睡前 4~8 h 效果最好，过近反而缩短 SWS）。取 +20%（4~8 h）/ +10%（8~12 h）。
    EXERCISE_DEEP_GAIN = 0.20
    EXERCISE_DEEP_GAIN_LATE = 0.10
    EXERCISE_ONSET_ADV_MIN = 8.0

    @staticmethod
    def _hash01(a: float, b: float) -> float:
        """确定性伪随机 0..1：同一 (a, b) 永远同一值，且不占用 rng 流。"""
        x = math.sin(float(a) * 12.9898 + float(b) * 78.233) * 43758.5453123
        return x - math.floor(x)

    def _night_index(self) -> int:
        """当前是第几夜（按仿真钟的午夜分界）。

        用 (t/3600 + clock_offset) // 24，而不是 t//86400 —— 与
        _body_sim_day / 事件天数同一口径（相位非零时两者不等）。
        """
        total_h = self.s.t / H2S + float(getattr(self, "clock_offset", 0.0) or 0.0)
        return int(total_h // 24.0)

    def _night_window(self) -> tuple:
        """当晚的（入睡, 起床）时刻——在配置窗口上叠加夜间变动。"""
        n = self._night_index()
        on = (float(self.sleep_start_hour)
              + (self._hash01(n, 11.0) - 0.5) * 2.0 * self.SLEEP_ONSET_JITTER_MIN / 60.0)
        # 约 1/5 的夜晚熬夜（周末/追剧那种）：只往后推，不提前
        if self._hash01(n, 71.0) < self.SLEEP_LATE_NIGHT_PROB:
            on += self.SLEEP_LATE_NIGHT_MIN * self._hash01(n, 73.0) / 60.0
        off = (float(self.sleep_end_hour)
               + (self._hash01(n, 23.0) - 0.5) * 2.0 * self.SLEEP_WAKE_JITTER_MIN / 60.0)

        s = self.s
        # ---- 咖啡因：推迟入睡（阈值 + 线性，见 CAFFEINE_* 注释）----
        caf = float(getattr(s, "caffeine", 0.0) or 0.0)
        if caf > self.CAFFEINE_THRESHOLD_MG:
            frac = clamp((caf - self.CAFFEINE_THRESHOLD_MG)
                         / max(1.0, self.CAFFEINE_FULL_MG - self.CAFFEINE_THRESHOLD_MG),
                         0.0, 1.5)
            on += self.CAFFEINE_MAX_DELAY_MIN * frac / 60.0
        # ---- 当天运动：入睡略快（4~12 h 内，越近越明显）----
        since_ex_h = (s.t - float(getattr(s, "last_exercise_t", -1e9))) / H2S
        if 0.0 <= since_ex_h <= 12.0:
            on -= self.EXERCISE_ONSET_ADV_MIN * (0.5 if since_ex_h > 8.0 else 1.0) / 60.0
        return (on % 24.0, off % 24.0)

    # ---- 连续睡眠区间（sleep bout）口径 ----
    # 2026-11-16 新增。只有连续睡眠达到 min_sleep_bout_hours（默认 4 小时）
    # 才被记为「一夜合格睡眠」，计入睡眠债结算与近 7 日质量统计。
    #
    # 【诚实标注】这个 4 小时门槛是**工程取舍，不是文献结论**。
    # 文献检索确认：目前没有「连续 4 小时不间断睡眠」这一健康阈值的
    # 权威出处（见 docs/血糖与睡眠文献依据.md 未核实清单第 3 条）。
    # 它的作用是——把午睡/打盹挡在「昨晚睡了多久」的统计之外，
    # 避免一次 25 分钟的小睡被记成一夜睡眠。取 4 小时是因为它明显低于
    # 成人最低正常夜间睡眠、又明显高于任何午睡时长，能稳定区分两者。
    # 需要不同口径时改这个数即可；设为 0 则关闭门槛。
    #
    # 为什么按「连续」而不是「累计」：睡眠碎片化（fragmentation）本身
    # 独立于总时长地损害恢复效果。但注意本模型的「连续」是带容忍间隙的
    # （见 sleep_bout_gap_tolerance_hours）—— 夜里短暂醒来不会把一夜切断，
    # 否则一个 6 小时 + 1 小时的夜晚会被错误地拆成 6h 和「不够 4h 的午睡」。
    # 设为 0 可关闭门槛（任何长度的 bout 都计入）。
    min_sleep_bout_hours: float = 4.0
    # 判定「两段睡眠是否连续」的容忍间隙：中途醒来不足这么久视为同一 bout
    # 里的一次短暂觉醒（会记入 wake，但不把 bout 切断）。
    sleep_bout_gap_tolerance_hours: float = 0.5
    # 近 7 日统计窗口
    sleep_week_window_days: float = 7.0

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

        # ---- 延迟结算当前 bout（2026-11-16）----
        # 清醒超过容忍间隙 → 这一段连续睡眠真的结束了，结算并结算质量。
        # 间隙之内 → 视为同一次睡眠的短暂觉醒，继续挂账等着睡回去。
        self._flush_sleep_bout()

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
                s.sleep_bout_awake_sec = float(getattr(s, "sleep_bout_awake_sec", 0.0)) + dt
                return

        # ---- 入睡 / 醒来：带一段过渡（不抖动）----
        # 入睡时间常数受核心体温调制：真实机制是核心体温必须下降约 0.3℃ 才
        # 容易入睡，黄体期基础体温偏高会推迟入睡（0.3~0.5℃ 约推迟 20~40 分钟）。
        # 基准 300 秒（约 5 分钟），体温每高 0.1℃ 增加 60 秒，上限 1200 秒。
        # 2026-11-22：同上，只认发热。正常核心体温的昼夜/睡眠摆动不该改变入睡速度，
        # 否则睡眠时长会被体温标定牵着走（一个间接耦合，极难排查）。
        onset_tau = 300.0 + max(0.0, (s.temp - FEVER_C)) * 600.0
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
                # 2026-11-16 修：旧实现在「重新入睡」（resumed=True）时不清零
                # dur_*，于是「睡 6 小时 → 醒 3 分钟 → 再睡」会接着同一份账本
                # 继续累加；跨夜的账本也从不归零，实测三晚 8 小时累计成 23.9 h，
                # 而 WebUI 上「睡了一整夜」却显示总时长接近 0。
                #
                # 新口径：账本按「连续睡眠区间（bout）」记账，而不是按「入睡事件」。
                #   距上一段 bout 结束 < sleep_bout_gap_tolerance_hours（默认 0.5h）
                #   → 视为同一次睡眠中的短暂觉醒，账本延续、只累计清醒时长；
                #   否则 → 新的一段 bout，账本清零重记。
                prev_end = float(getattr(s, "sleep_bout_end_t", 0.0) or 0.0)
                tol = float(self.sleep_bout_gap_tolerance_hours) * H2S
                same_bout = (s.sleep_bout_start_t > 0.0 and prev_end > 0.0
                             and (s.t - prev_end) <= tol)
                if same_bout:
                    # 同一 bout 内的短暂觉醒后重新入睡：只累计清醒，不清账
                    s.sleep_bout_wakes = int(getattr(s, "sleep_bout_wakes", 0)) + 1
                else:
                    s.dur_light = s.dur_deep = s.dur_rem = s.dur_awake = 0.0
                    s.wake_count = 0
                    s.wake_by_message = s.wake_by_thirst = s.wake_by_bladder = 0
                    s.sleep_bout_start_t = s.t
                    s.sleep_bout_wakes = 0
                    s.sleep_bout_light_sec = 0.0
                    s.sleep_bout_deep_sec = 0.0
                    s.sleep_bout_rem_sec = 0.0
                    s.sleep_bout_awake_sec = 0.0
                    s.sleep_bout_stage_sec = 0.0
                if not resumed:
                    s.sleep_onset_t = s.t
                # 2026-11-16 修真实 bug：原文是 s.last_sleep_t = s.last_sleep_t or s.t，
                # `or` 短路导致它只在第一次赋值时生效，之后永远是旧值 ——
                # 服务器存档实测该字段停在「第一次入睡」的时刻，比当时晚了
                # 38.7 小时。字段注释写的是「上次入睡的仿真时刻」，语义不符。
                # 现改为每次真正入睡都更新。
                s.last_sleep_t = s.t
                # 记下「夜间时段」，供结算时判定午睡 vs 夜间片段。
                # 结算常发生在窗口开关已关闭之后，不能依赖当时的开关状态。
                #
                # 2026-11-16 修：不能无条件记录当次入睡的窗口 —— 午睡时
                # sleep_start/end 会被设成 13:00–13:30，照记的话午睡窗口
                # 自己就成了"夜间窗口"，13:07 落在里面被当成夜间睡眠
                # （实测回归：午睡被计入夜数）。只有当这个窗口确实跨越
                # 深夜（覆盖 0 点附近，或时长 >= 4h）时才认定为夜间窗口。
                _a = float(self.sleep_start_hour)
                _b = float(self.sleep_end_hour)
                _span = (_b - _a) % 24.0
                if _span <= 0.0:
                    _span = 24.0
                _crosses_midnight = (_a > _b) or (_a <= 4.0) or (_b >= 24.0)
                if (_span >= 4.0) and _crosses_midnight:
                    self.sleep_night_window = (_a, _b)
                self.bus.emit(Event(s.t, "sleep_start", "info",
                                    "重新入睡" if resumed else "睡着",
                                    {"stage": "light"}))
        elif want_sleep <= 0.5 and was_sleeping:
            # 醒来过程
            s.sleep_depth = approach_tau(s.sleep_depth, 0.0, dt, 300.0)
            if s.sleep_depth <= 0.5:
                s.sleeping = False
                s.sleep_stage = "awake"
                # 2026-11-16：醒来不再立即结算，只标记 bout 的「最后清醒时刻」。
                # 原因：夜里短暂觉醒（被消息吵醒、起夜）如果立刻结算，会把
                # 一个完整夜晚切成 6h + 1h 两段，后一段因不足 4h 被当成午睡
                # 丢弃 —— 用户看到的正是「睡了 6 小时只算了半小时」。
                # 改为延迟结算：清醒超过 sleep_bout_gap_tolerance_hours
                # 才认定这一段 bout 真的结束了（见 _flush_sleep_bout）。
                if float(getattr(s, "sleep_bout_start_t", 0.0)) > 0.0:
                    s.sleep_bout_end_t = s.t
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
        # 2026-11-16 修：删除「窗口内清醒」的即时累积。
        # 服务器存档实测暴露了它的后果 —— 8 天里实际睡了 62.77 h，
        # 对 8 晚 × 7.5 h = 60 h 的需求几乎不缺，但 sleep_debt 报到 13.12。
        # 原因是同一段清醒时间被**重复计债三次**：
        #   ① 这里（窗口内清醒，按秒累加）
        #   ② 上面的 awake_hours > 16 h（按秒累加）
        #   ③ _settle_sleep 里按「每晚缺口」结算
        # 三条通路口径不同却同时生效，债自然越滚越大，且与「到底睡够没有」
        # 脱钩。现统一到单一账本：**只由 _settle_sleep 在每段合格 bout
        # 结束时按缺口结算**，清醒期不再单独加债。
        # awake_hours > 16 h 那条保留，因为它捕捉的是「一直不睡」这种
        # 极端情形 —— 那种情况下根本不会有 bout 结束，没有它债就是 0。

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
        # 2026-11-17：一律按**慢跑**处理。日程只能回答「要不要跑」，给不出配速/
        # 距离/间歇结构，据此切强度档只是假装知道。强度分档的代码保留，
        # 等有了可靠的烈度来源再接。见 docs/issues/issue_运动烈度无法控制.md
        level = "mid"
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

        # 周期长度与分期配比都按「夜 + 周期序号」做确定性浮动（见 SLEEP_*_JITTER）
        _n = self._night_index()
        cycle_min = (self.SLEEP_CYCLE_MIN
                     + (self._hash01(_n * 7.0 + s.stage_index, 37.0) - 0.5)
                     * 2.0 * self.SLEEP_CYCLE_JITTER_MIN)
        cycle_sec = cycle_min * 60.0
        light_frac, deep_frac, rem_frac = self.cycle_profile(s.stage_index)
        # 夜与夜之间的配比差异：深睡、REM 各 ±25%，再归一化
        deep_frac *= 0.75 + 0.50 * self._hash01(_n * 13.0 + 5.0, 53.0)   # ±25%
        rem_frac *= 0.75 + 0.50 * self._hash01(_n * 17.0 + 3.0, 61.0)
        # ---- 酒精：前半夜压 REM、后半夜反跳（[R45]）----
        _bac = clamp(float(getattr(s, "bac", 0.0) or 0.0) / self.ALCOHOL_FULL_BAC, 0.0, 1.5)
        if _bac > 0.0:
            if s.stage_index <= 2:
                rem_frac *= max(0.05, 1.0 - self.ALCOHOL_REM_SUPPRESS * _bac)
            else:
                rem_frac *= 1.0 + self.ALCOHOL_REM_REBOUND * _bac
        # ---- 当天运动：加深慢波（[R46]，睡前 4~8 h 最佳）----
        _since_ex = (s.t - float(getattr(s, "last_exercise_t", -1e9))) / H2S
        if 0.0 <= _since_ex <= 8.0:
            deep_frac *= 1.0 + self.EXERCISE_DEEP_GAIN
        elif 8.0 < _since_ex <= 12.0:
            deep_frac *= 1.0 + self.EXERCISE_DEEP_GAIN_LATE

        _tot = light_frac + deep_frac + rem_frac
        if _tot > 0.0:
            light_frac, deep_frac, rem_frac = (light_frac / _tot,
                                               deep_frac / _tot,
                                               rem_frac / _tot)

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

        # 累计时长（整夜账本 + 当前 bout 账本，2026-11-16 起两者分开记）
        if new_stage == "light":
            s.dur_light += dt
            s.sleep_bout_light_sec = float(getattr(s, "sleep_bout_light_sec", 0.0)) + dt
        elif new_stage == "deep":
            s.dur_deep += dt
            s.sleep_bout_deep_sec = float(getattr(s, "sleep_bout_deep_sec", 0.0)) + dt
        else:
            s.dur_rem += dt
            s.sleep_bout_rem_sec = float(getattr(s, "sleep_bout_rem_sec", 0.0)) + dt
        s.sleep_bout_stage_sec = (float(getattr(s, "sleep_bout_stage_sec", 0.0)) + dt)

        if new_stage != s.sleep_stage:
            # 2026-11-21（v1.3.1）：原先每次分期切换都发一条 debug 事件 ——
            # 一夜 4~6 个周期、每周期 3~4 次切换，于是时间线里整夜里全是
            # "睡眠分期"行，把真正要看的事件（进食、排尿、醒来）刷掉了。
            # 分期本身是**连续量**，该画成图（见 WebUI 的夜间分期条），
            # 不该当离散事件记录。故此处只推进状态、不发事件。
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

    def _flush_sleep_bout(self):
        """检查当前 bout 是否已经真正结束，是则结算。

        判定条件：不在睡眠中，且距离 bout 内最后一次由睡转醒已经超过了
        sleep_bout_gap_tolerance_hours（默认 0.5 小时）。
        这样做的好处：短暂的夜间觉醒（被叫醒、起夜）不会把一个整夜
        切成两段，而真正起床后又会被及时结算，不会出现「睡完了才发现
        总时长还是 0」。
        """
        s = self.s
        if s.sleeping:
            return
        if float(getattr(s, "sleep_bout_start_t", 0.0)) <= 0.0:
            return
        last = float(getattr(s, "sleep_bout_end_t", 0.0) or 0.0)
        if last <= 0.0:
            return
        tol = float(getattr(self, "sleep_bout_gap_tolerance_hours", 0.5)) * H2S
        if (s.t - last) < tol:
            return
        self._settle_sleep()

    def _settle_sleep(self):
        """醒来结算：按「连续睡眠区间（bout）」算质量、还睡眠债。

        2026-11-16 重写。旧实现直接拿 dur_light/dur_deep/dur_rem 的整体累计
        当作「这一觉」的时长，由此产生三个互相叠加的错误：

          1. dur_* 只在首次入睡时清零，跨夜不归零 —— 实测连续三晚 8 小时
             后 total_h 变成 23.93（三晚串成一觉）。
          2. 「重新入睡」分支（resumed=True）同样不清零，中途醒一次再睡
             会接着同一份账本继续加。
          3. 只有「醒来」才结算，而正常作息下她睡到窗口末尾才醒；于是
             WebUI 在整个夜里拿到的都是上一次（或初始的 0）质量值 ——
             用户看到「睡了 6 小时只算了半小时」，根因在这里。

        新口径：
          - 时长只认「连续睡眠区间」，bout 内被唤醒的清醒时间不计入睡眠
            时长，但计入睡眠效率（PSG 的 sleep efficiency 定义）。
          - 短于 min_sleep_bout_hours（默认 4h）的 bout —— 午睡、打盹 ——
            不记为合格一夜：不参与睡眠债结算、不进入近 7 日质量统计，
            只发一个 nap_end 事件。这与「午睡不该影响夜间睡眠质量」一致。
          - 合格的 bout 追加进 sleep_bouts，并重算近 7 日汇总（PSQI 口径）。
        """
        s = self.s
        light_s = float(getattr(s, "sleep_bout_light_sec", 0.0) or 0.0)
        deep_s = float(getattr(s, "sleep_bout_deep_sec", 0.0) or 0.0)
        rem_s = float(getattr(s, "sleep_bout_rem_sec", 0.0) or 0.0)
        awake_s = float(getattr(s, "sleep_bout_awake_sec", 0.0) or 0.0)

        slept_s = light_s + deep_s + rem_s
        if slept_s <= 0.0:
            return

        # bout 总时长 = 实际睡着 + 中途清醒（在床时间，time in bed）
        bout_s = slept_s + awake_s
        total_h = slept_s / H2S
        bout_h = bout_s / H2S
        efficiency = (slept_s / bout_s) if bout_s > 0.0 else 0.0

        s.sleep_bout_end_t = s.t

        min_bout = float(getattr(self, "min_sleep_bout_hours", 4.0) or 0.0)

        # ---- 午睡 vs 夜间碎片：按「时段」区分，不只看时长 ----
        # 2026-11-16 修：旧逻辑用「总时长 < 门槛」一刀切丢弃，结果是
        # 「23:00 睡到 02:00（3h）→ 因故醒 1h → 再睡到 07:00」这种被
        # 打断的夜晚，前半段 3h 因不足 4h 被整体丢掉，后半段又从零起算，
        # 最后整夜什么都没记住（实测 bouts=0、last_sleep_hours=0）。
        # 正确口径：
        #   · 落在**夜间睡眠窗口**（或紧邻窗口）的片段 —— 即使被中途醒来
        #     打断、单段不足 4h —— 仍属于这一夜，应当累计并计入统计；
        #   · 落在**日间**的短睡才是午睡，按门槛过滤。
        # 判据用 bout 起点所在的钟点是否在该夜的睡眠窗口内（含前后 2h 余量）。
        bout_start_hour = 0.0
        if float(getattr(s, "sleep_bout_start_t", 0.0) or 0.0) > 0.0:
            bout_start_hour = ((float(s.sleep_bout_start_t) / H2S)
                               + float(getattr(self, "clock_offset", 0.0))) % 24.0

        # 2026-11-16 修：旧版这里直接调 self._in_sleep_window()，但结算往往
        # 发生在窗口**已经关闭之后**（插件把 sleep_start/end 设成 0,0 表示
        # "现在不该睡"），于是 _in_sleep_window 恒为 False，任何夜间片段
        # 都被误判成"日间午睡"而丢弃 —— 实测「睡 3h → 醒 1h → 再睡 4h」
        # 的第二段 4h 因此整段丢失。
        # 现在改用**固化的夜间时段**判断：优先取实例上记录的
        # sleep_night_window（由引擎在真正入睡时记下），否则回落到
        # 常规的 23-7 常识窗口。绝不依赖调用瞬间的开关状态。
        win = getattr(self, "sleep_night_window", None)
        if not (isinstance(win, (tuple, list)) and len(win) == 2):
            win = (23.0, 7.0)
        a_h, b_h = float(win[0]) % 24.0, float(win[1]) % 24.0
        if a_h > b_h:
            in_night_window = (bout_start_hour >= a_h) or (bout_start_hour < b_h)
        else:
            in_night_window = (a_h <= bout_start_hour < b_h)
        # 窗口外但离窗口端点很近（刚醒又睡回去）也算夜间片段
        if not in_night_window:
            for edge in (a_h, b_h):
                d = abs(bout_start_hour - edge)
                d = min(d, 24.0 - d)
                if d <= 2.0:
                    in_night_window = True
                    break

        is_nap = (total_h < min_bout) and (not in_night_window)
        if is_nap:
            s.sleep_week_skipped = int(getattr(s, "sleep_week_skipped", 0) or 0) + 1
            self.bus.emit(Event(s.t, "nap_end", "debug", "小睡结束", {
                "slept_h": round(total_h, 2),
                "counts_as_night": False,
                "reason": f"日间短睡，短于 {min_bout:.1f} 小时门槛",
            }))
            s.sleep_bout_start_t = 0.0
            return

        deep_r = deep_s / slept_s
        rem_r = rem_s / slept_s

        # ---- 质量评分（0~100），口径对齐 PSQI 成分 ----
        #   PSQI 的七个成分里，与单夜睡眠结构直接相关的是：
        #   主观睡眠质量、入睡潜伏期、睡眠时长、睡眠效率、睡眠障碍。
        #   这里取可量化的四项：
        #   时长分（40）—— 7~9 h 满分，6 h 明显扣分（AASM 成人推荐 7~9 h）
        #   深睡分（25）—— 健康成人 N3 占 TST 约 13~23%，取 18% 为中心
        #   REM 分（20）—— 健康成人 REM 占 TST 约 20~25%，取 22.5% 为中心
        #   效率分（15）—— 睡眠效率 = TST / 在床时间，85% 以上视为正常
        if total_h >= 7.0:
            dur_score = 40.0
        elif total_h >= 6.0:
            dur_score = 40.0 * (total_h - 5.0) / 2.0
        else:
            dur_score = max(0.0, 40.0 * (total_h - min_bout) / max(1e-6, 6.0 - min_bout))

        deep_score = 25.0 * max(0.0, 1.0 - abs(deep_r - 0.18) / 0.18)
        rem_score = 20.0 * max(0.0, 1.0 - abs(rem_r - 0.225) / 0.225)
        # 效率分：85% 及以上满分，往下线性衰减到 60% 归零
        eff_score = 15.0 * clamp((efficiency - 0.60) / 0.25, 0.0, 1.0)

        s.sleep_quality = clamp(dur_score + deep_score + rem_score + eff_score,
                                0.0, 100.0)
        s.last_sleep_hours = total_h   # 供运动门槛判断"昨晚睡够没有"

        # ---- 睡眠债结算 ----
        # 缺口按「每晚所需时长」算；基准默认 7.5h。
        need = max(4.0, float(getattr(self, "sleep_need_hours", 7.5)))
        shortfall = max(0.0, need - total_h)
        if shortfall > 0.0:
            s.sleep_debt = clamp(s.sleep_debt + shortfall, 0.0, 48.0)

        # 还债按「等效睡眠小时」：质量差的一小时顶不满一小时。
        q_factor = 0.4 + 0.6 * clamp(s.sleep_quality / 100.0, 0.0, 1.0)
        effective_h = total_h * q_factor
        # 前 1 小时视为入睡过渡，不计入还债。
        s.sleep_debt = clamp(s.sleep_debt - max(0.0, effective_h - 1.0) * 0.9,
                             0.0, 48.0)

        # ---- 写入 bout 历史，供近 7 日统计 ----
        bouts = list(getattr(s, "sleep_bouts", None) or [])
        bouts.append({
            "end_t": float(s.t),
            "start_t": float(s.sleep_bout_start_t or (s.t - bout_s)),
            "hours": round(total_h, 2),
            "bout_h": round(bout_h, 2),
            "quality": round(s.sleep_quality, 1),
            "efficiency": round(efficiency, 3),
            "light_h": round(light_s / H2S, 2),
            "deep_h": round(deep_s / H2S, 2),
            "rem_h": round(rem_s / H2S, 2),
            "wakes": int(getattr(s, "sleep_bout_wakes", 0) or 0),
        })
        s.sleep_bouts = bouts
        self._recompute_sleep_week()

        s.sleep_bout_start_t = 0.0

        self.bus.emit(Event(s.t, "sleep_end", "info", "醒来", {
            "slept_h": round(total_h, 2),
            "bout_h": round(bout_h, 2),
            "efficiency": round(efficiency, 3),
            "light_h": round(light_s / H2S, 2),
            "deep_h": round(deep_s / H2S, 2),
            "rem_h": round(rem_s / H2S, 2),
            "quality": round(s.sleep_quality, 1),
            "cycles": s.stage_index + 1,
            "counts_as_night": True,
        }))

    # 近 7 日窗口内保留多少段 bout 作为上限（防止无限增长）
    SLEEP_BOUTS_MAX = 32

    def _recompute_sleep_week(self):
        """重算近 7 日睡眠汇总（7 日滑动聚合，**不是 PSQI**）。

        【重要 · 命名纪律】不要把这个汇总称作 PSQI。
        PSQI（Buysse 1989）的官方回顾窗口是**1 个月**，且明确不建议
        改用其它时间框架；把它说成「近 7 日 PSQI」是公式性错误。
        这里做的是「PSQI 成分的 7 日类比」：沿用 PSQI 里可量化的成分
        （睡眠时长 TST、睡眠效率 SE、睡眠障碍/觉醒次数），但窗口取 7 天。
        为什么取 7 天：单夜波动太大，不足以说明睡眠好坏；7 天是临床上
        描述「近期睡眠」的常用滑动窗口，也正是用户要求的「看近一周」。
        文献依据见 docs/血糖与睡眠文献依据.md B 组。

        统计量：
          nights        —— 窗口内合格 bout 数
          hours         —— 合格 bout 的睡眠小时合计（TST 之和）
          quality       —— 按小时加权的平均质量评分
          efficiency    —— 按小时加权的平均睡眠效率（SE）
        """
        s = self.s
        window_s = max(1.0, float(getattr(self, "sleep_week_window_days", 7.0))) * 24.0 * H2S
        now = float(s.t)
        bouts = [b for b in (getattr(s, "sleep_bouts", None) or [])
                 if isinstance(b, dict) and (now - float(b.get("end_t", 0.0))) <= window_s]
        # 只保留窗口内的，并按数量上限裁剪（丢最老的）
        bouts = bouts[-int(self.SLEEP_BOUTS_MAX):]
        s.sleep_bouts = bouts

        if not bouts:
            s.sleep_week_hours = 0.0
            s.sleep_week_quality = 0.0
            s.sleep_week_efficiency = 0.0
            s.sleep_week_nights = 0
            s.sleep_week_updated_t = now
            return

        total_h = sum(float(b.get("hours", 0.0)) for b in bouts)
        if total_h <= 0.0:
            s.sleep_week_hours = 0.0
            s.sleep_week_quality = 0.0
            s.sleep_week_efficiency = 0.0
            s.sleep_week_nights = len(bouts)
            s.sleep_week_updated_t = now
            return

        # 按小时加权：睡得长的那一夜对总体评价的贡献更大
        wq = sum(float(b.get("quality", 0.0)) * float(b.get("hours", 0.0)) for b in bouts)
        we = sum(float(b.get("efficiency", 0.0)) * float(b.get("hours", 0.0)) for b in bouts)
        s.sleep_week_hours = round(total_h, 2)
        s.sleep_week_quality = round(wq / total_h, 1)
        s.sleep_week_efficiency = round(we / total_h, 3)
        s.sleep_week_nights = len(bouts)
        s.sleep_week_updated_t = now


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

    def feed(self, kind: str = "snack", items: list = None, note: str = "",
             portion: float = 1.0, ratios: dict = None) -> dict:
        """外部喂食：把一次真实发生的吃喝灌进身体。

        与 _eat 的区别：_eat 从固定食谱池里随机挑，feed 接受指定食物，
        用于"我在聊天里说我点了外卖"这类需要落到具体食物的场景。
        kind 支持 meal(按当前钟点选餐段) / snack / drink。
        返回实际吃进去的组合，供上层回述。
        """
        s = self.s
        if s.hospital_until > s.t:
            return {"ok": False, "reason": "住院中，由医护供能"}

        # 2026-11-17：入口校验（探索测试发现）。旧实现把未知食物与非法 kind 当
        # 成功返回 ok=True，但血糖、胃内容物、「最近一餐」全无变化 —— 调用方会
        # 拿到一个假成功。现在非法输入一律 ok=False 并说明原因。
        kind = str(kind or "").strip().lower()
        if kind not in {"meal", "snack", "drink"}:
            return {"ok": False,
                    "reason": f"kind 无效：{kind!r}，只支持 meal / snack / drink"}
        keys_clean: list = []
        unknown: list = []
        for it in (list(items) if items else []):
            k = it.strip() if isinstance(it, str) else str(it).strip()
            if not k:
                continue
            if k in food.FOODS:
                if k not in keys_clean:
                    keys_clean.append(k)
            else:
                unknown.append(k)
        if unknown:
            return {"ok": False,
                    "reason": "食物库里没有：" + "、".join(unknown[:3]),
                    "unknown": unknown}

        if kind == "drink":
            # 2026-11-15：留空不再随机配饮品。旧实现走 food.pick_drink() 从
            # DRINK_POOL 里抽，模型报「我喝了半杯白酒」而解析不出具体饮品时，
            # 会落成随机果汁 —— 说了喝、身体没喝到点子上，比不记还误导。
            # 现在认不出就按白水记（零热量、零酒精、零咖啡因），并把
            # fallback 标记回传给上层，由它提示模型补具体饮品。
            fallback = not keys_clean
            keys = keys_clean or [food.DEFAULT_DRINK_KEY]
            s.last_drink_t = s.t
            s.drinks += 1
            tot = self._scale_tot(food.combo_totals(keys), portion)
            self._load_stomach(keys, tot, water_already=True)
            name = food.combo_summary(keys)
            # 旧实现只加 drinks 计数，从不写 last_drink_items —— 记了账界面上
            # 也看不到「最近一次喝了什么」，issue #3 正是拿这个字段判"没记"。
            s.last_drink_items = name
            self.bus.emit(Event(s.t, "drink", "info", f"饮水：{name}",
                                {"items": keys, "note": note, "fallback": fallback}))
            return {"ok": True, "kind": "drink", "items": keys, "fallback": fallback,
                    "portion": float(portion),
                    "volume_ml": round(float(tot.get("volume_ml") or 0.0), 1),
                    "kcal": round(float(tot.get("kcal") or 0.0), 1)}

        slot = "snack" if kind == "snack" else self._meal_slot(s.hour)
        keys = keys_clean or (
            food.pick_snack(self.rng) if slot == "snack" else food.pick_meal(slot, self.rng)
        )
        actual = self._eat_keys(slot, keys, note=note, ratios=ratios)
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

    @staticmethod
    def _combo_totals_ratio(keys: list, ratios: dict) -> dict:
        """逐条按比例累加成分总量，供「不同的菜各多少克」这种混合记账。

        先对每个 key 单独求和再乘比例，最后累加 —— 不能先求和再统一乘，
        否则「180 g 番茄 + 50 g 面」会被拉成同一个比例。
        """

        total: dict = {}
        for key in keys:
            one = food.combo_totals([key])
            r = 1.0
            try:
                r = max(0.0, min(20.0, float(ratios.get(key, 1.0))))
            except (TypeError, ValueError):
                r = 1.0
            for name, value in one.items():
                if isinstance(value, (int, float)):
                    total[name] = total.get(name, 0.0) + float(value) * r
        return total

    @staticmethod
    def _scale_tot(tot: dict, portion: float) -> dict:
        """按份量比例缩放一次吃喝的成分总量（portion=0.5 即半份）。

        2026-11-15：给跨插件 API log_drink 用 —— 「她自己喝了半杯」不该按整杯
        记进血液。夹紧在 0.05~3.0，异常值一律按整份，不影响既有调用。
        """

        try:
            p = float(portion)
        except (TypeError, ValueError):
            return tot
        if not (0.05 <= p <= 3.0) or abs(p - 1.0) < 1e-6:
            return tot
        return {k: (float(v) * p if isinstance(v, (int, float)) else v) for k, v in tot.items()}

    def _load_stomach(self, keys, tot, water_already=False):
        """把一组食物按成分灌进胃里（对应 APK 的 AddContent / GastricVolume）。"""
        s = self.s
        s.gastric_volume = min(1500.0, s.gastric_volume + tot["volume_ml"])
        s.chyme_carb += tot["carb_g"]
        # GI 分账（2026-11-16）：把这次的碳水按升糖快慢记到两本账上。
        # 总量仍记在 chyme_carb，两本账只影响后续「吸收进入血液的速率」。
        try:
            slow_g, fast_g = food.carb_gi_split(keys)
        except Exception:
            slow_g, fast_g = 0.0, 0.0
        if slow_g > 0.0 or fast_g > 0.0:
            s.chyme_carb_slow = float(getattr(s, "chyme_carb_slow", 0.0)) + slow_g
            s.chyme_carb_fast = float(getattr(s, "chyme_carb_fast", 0.0)) + fast_g
        # 按碳水克数累积加权 GI，供「入血比例」按 GL 折算
        try:
            w_gi, w_mass = food.carb_gi_weighted(keys)
        except Exception:
            w_gi, w_mass = 0.0, 0.0
        if w_mass > 0.0:
            prev_m = float(getattr(s, "gut_carb_gi_mass", 0.0) or 0.0)
            prev_acc = prev_m * float(getattr(s, "gut_carb_gi_weighted", 0.0) or 0.0)
            new_m = prev_m + w_mass
            s.gut_carb_gi_mass = new_m
            s.gut_carb_gi_weighted = (prev_acc + w_gi * w_mass) / new_m if new_m > 0 else w_gi
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

    def _eat_keys(self, slot, keys, note="", ratios: dict = None):
        """按指定食物组合进餐：胃内容物 -> 逐项成分 -> 排空吸收。

        2026-11-06 改造：原实现是 glucose += 2.2 / na_mass += 40 的常量注入，
        与 APK 的 AddContent -> Chyle -> 分段吸收 结构不符（FoodType 23 项无法体现）。
        现改为胃内容物 + 逐项成分 + 排空吸收，见 _metabolic。
        """
        s = self.s
        # 正餐补到七分饱：日程/工具点名了具体食物时，那几句话往往只描述了
        # 一道主菜（"点了碗米粉"），按原样落地胃内容物只有两三百毫升。
        # 这里对所有正餐统一补足到 MEAL_TARGET_ML；零食（snack）不补。
        # 按克数记账（ratios 非空）时不补足：厨房报的是实际重量，
        # 再补到七分饱会让「半碗」凭空变大。
        if ratios is None and slot in ("breakfast", "lunch", "dinner"):
            keys = food.ensure_meal_size(keys)
        keys = list(keys)
        tot = self._combo_totals_ratio(keys, ratios) if ratios else food.combo_totals(keys)
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
        # 受体占据已迁到 body/endocrine/（2026-11-18 拆出，2026-11-21 按器官分科）。
        # 注意传的是局部 symp/para 而非 s.symp —— 后者是动态属性、不进存档。
        endocrine.step_autonomic_receptors(s, symp, para)

    # ------------------------------------------------------------------
    # 心血管：心率 -> 每搏量 -> 心输出量 -> MAP -> SBP/DBP
    # ------------------------------------------------------------------
    def _cardio(self, dt):
        s = self.s

        # 2026-11-08 重标定：旧系数(62/45/22)把日间静息 HR 拉到 82(beta)
        # 与真实 70-75 偏高，同时夜间因 r_m2 项过重掉到 44。现按
        # 日间 72、夜间 58 两个实测靶点反解：beta1 与 m2 的摆动幅度都收窄。
        # 静息基线随年龄轻微上移：18 岁 60、60 岁 68.4（成人静息 60~100 的偏年轻段）。
        _age = float(getattr(self, "age", 18) or 18)
        hr_rest_base = 60.0 + 0.20 * (_age - 18.0)
        # 2026-11-22：体温项只保留**发热段**。原先写的是 3.0*(temp-36.8)，
        # 于是昼夜 0.3℃ 的正常摆动、睡眠期 0.4℃ 的降幅都会被算进心率 ——
        # 结果是「改睡眠体温标定，血压心率跟着变」，一处标定污染整条链。
        # 常温段（36.3~37.3）体温对静息心率的真实影响不足 1 bpm，不值得承担这个耦合；
        # 发热时的心动过速才是必须保留的部分（37.3℃ 以上每度 +3 bpm 的量级）。
        _fever = max(0.0, s.temp - FEVER_C)
        hr_t = (hr_rest_base + 32.0 * s.r_beta1 - 16.0 * s.r_m2
                + 3.0 * _fever + 8.0 * (s.t3 - 1.0))
        if s.exercising:
            # 运动目标按**最大心率百分比**定，不再是固定加数：
            # 慢跑（Zone 2 下限）60% HRmax、快跑 72%。年龄越大 HRmax 越低，
            # 同样的相对强度对应更低的绝对心率 —— 这是固定 +40 做不到的。
            _hr_max = HR_MAX_INTERCEPT - HR_MAX_SLOPE * _age
            _frac = (HR_EX_FRAC_HIGH
                     if str(getattr(s, "exercise_intent", "") or "mid") == "high"
                     else HR_EX_FRAC_MID)
            hr_t = max(hr_t, _hr_max * _frac)
        # 睡眠时心率随笔迷走张力下降，深睡可降到 50 上下；不用运动那套 +40 的档
        # 睡眠对心率的效应已经全部通过 r_beta1 / r_m2 传进来了（交感下调 ->
        # beta1 降、迷走相对占优 -> M2 升）。这里不再另减，否则重复计账：
        # 实测叠加 -14 时夜间 HR 掉到 44、SBP 掉到 86，低于真人夜间下限。
        if self.link_activity > 0.0:
            hr_t += 10.0 * min(1.0, self.link_activity)
        # 深睡期心率进一步下移：旧实现只有 r_m2 一项，18 岁深睡实测约 64，
        # 而文献健康年轻人睡眠 45~70、常见 40~60。这里再压 6 bpm（不会像
        # 早先试过的 -14 那样掉到 44）。
        hr_t -= float(getattr(self, "hr_sleep_drop", 4.0)) * clamp(
            float(getattr(s, "sleep_depth", 0.0) or 0.0), 0.0, 1.0)
        hr_t = clamp(hr_t, 35.0, 185.0)
        # 方向性时间常数 + 指数逼近（见 EX_HR_* / HR_TAU_* 注释）：
        # 上升快、下降慢，停止后 1 分钟回落约 15~25 bpm，而不是 12 秒归位。
        _tau_hr = HR_TAU_RISE if hr_t >= s.hr else HR_TAU_FALL
        _alpha_hr = min(1.0, 1.0 - math.exp(-dt / _tau_hr))
        s.hr = clamp(s.hr + (hr_t - s.hr) * _alpha_hr, 35.0, 185.0)

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
        _lvl_hi = str(getattr(s, "exercise_intent", "") or "mid") == "high"
        rr_t = 14.0 + 6.0 * (s.stress / 100.0) + 0.8 * max(0.0, s.paco2 - 40.0) / 5.0
        if s.exercising:
            # 强度分级：慢跑 +8、快跑 +14（旧实现一律 +8）。
            rr_t += EX_RR_HIGH if _lvl_hi else EX_RR_MID
        s.rr = approach(s.rr, clamp(rr_t, 6.0, 40.0), dt, "rr")

        # ---- 肺泡通气量 -> 潮气量（2026-11-16）----
        # 旧实现 tidal 恒为 500（BodyState 里的常量，全引擎无人调节）：运动时
        # VCO2 从 0.255 涨到 0.605（+137%），而通气只靠 rr 涨 57%，补偿严重不足
        # —— 实测 pCO2 冲到 59、pH 跌到 7.235，误报「酸中毒」。
        #
        # 被控量必须是**肺泡通气量 VA**，不是总分钟通气量 vent。
        # 曾误用 vent：固定 vent 后 VA = vent - 0.15*rr 会随 rr 升高而下降，
        # 与生理方向相反，并通过 rr 的 pCO2 反馈形成正反馈。A/B 实测（应激 100）：
        #   基线  VA 7.00 / pCO2 31.4     误用版  VA 4.55 / pCO2 48.4
        # 方向完全颠倒，故改为直接控制 VA。
        #
        # 目标 pCO2 定法：
        #   - 静息 40
        #   - 运动 38.5（真实运动存在轻度过度通气，pCO2 略低于静息）
        #   - 应激时按 40 - 0.09*stress 下降：焦虑紧张会换气过度 → 呼吸性碱中毒。
        #     该系数由基线行为标定（基线 stress=100 时 pCO2 约 31），不是新发明。
        # 反解：VA = 863*VCO2/pCO2，再由 VA 反解 tidal = VA*1000/rr + 150
        # （+150 补回解剖死腔）。于是 rr 变化时 tidal 自动补偿，VA 始终保持在
        # 目标值 —— pCO2 不再因 rr 波动漂移；运动相位切换时 VCO2 阶跃、tidal
        # 同步阶跃，VA 瞬时到位，不会出现「VCO2 已降而 VA 未降」的碱中毒过冲。
        stress = max(0.0, float(getattr(s, "stress", 0.0) or 0.0))
        vco2_t = 0.255 + ((EX_VCO2_HIGH if _lvl_hi else EX_VCO2_MID) if s.exercising else 0.0)
        vco2_t += max(0.0, s.temp - FEVER_C) * 0.06
        if s.exercising:
            pco2_set = 38.5
        else:
            # 静息目标必须复现基线的平衡解，而不是拍一条直线。
            # 基线里 stress 只经 rr 影响通气：
            #   rr = 14 + 0.06*stress + 0.16*max(0, pCO2-40)
            #   VA = 0.35*rr          （tidal 恒 500 时的等价式）
            #   pCO2 = 220.1/VA
            # 解这个自洽方程即得基线平衡 pCO2（stress=0 时 43.3、28 时 40.1、
            # 100 时 31.4），与实测逐点吻合。用线性近似会在中低应激段偏低
            # —— 曾用 40-0.09*stress，实测静息 pCO2 从 40.05 掉到 37.46。
            _rr0 = 14.0 + 0.06 * stress
            _p_hyper = 628.9 / max(_rr0, 1.0)      # 220.1/(0.35*rr)
            if _p_hyper > 40.0:
                # 代回 max(0, pCO2-40) 项后的二次方程正根
                _b = 7.6 + 0.06 * stress
                pco2_set = (-_b + math.sqrt(_b * _b + 402.5)) / 0.32
            else:
                pco2_set = _p_hyper
            pco2_set = clamp(pco2_set, 25.0, 45.0)
        va_t = clamp(863.0 * vco2_t / max(pco2_set, 20.0), 2.0, 60.0)
        s.tidal = clamp(va_t * 1000.0 / max(s.rr, 1.0) + 150.0, 350.0, 2200.0)

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
        vco2 += max(0.0, s.temp - FEVER_C) * 0.06
        paco2_t = clamp(863.0 * vco2 / va, 25.0, 70.0)
        s.paco2 = approach_tau(s.paco2, paco2_t, dt, 30.0)

    # ------------------------------------------------------------------
    # 酸碱平衡：pH 由碳酸氢根与 pCO2 共同决定
    # ------------------------------------------------------------------
    # ---- 酸碱模型常数（Van Slyke 方程，Siggaard-Andersen 1977）----
    # 标准碱剩余用「血红蛋白 5 g/dL」的细胞外液口径：该口径把血浆与红细胞
    # 之间的缓冲分摊折算成细胞外液的等效值，是临床血气仪的通用做法。
    #   SBE = (1 - 0.023*Hb) * ((HCO3 - 24.4) + (2.3*Hb + 7.7)*(pH - 7.4))
    _STD_HB = 5.0
    _K_HB = 1.0 - 0.023 * _STD_HB          # 0.885
    # 非碳酸氢盐缓冲当量 m（mmol/L per pH）：
    #   标准 SBE 取 Hb=5 g/dL -> m = 2.3*5 + 7.7 = 19.2，口径只覆盖
    #   细胞外液（血浆 + 红细胞），比体内实际缓冲弱。
    #   真实人体约 60% 的缓冲发生在细胞内（蛋白、肽、磷酸盐），
    #   这部分不体现在标准 SBE 里。本模型取 in-vivo 有效值 40.0，
    #   使运动性酸中毒幅度与实测吻合：
    #     乳酸 7.7（本模型运动峰值）-> pH 7.33（实测重运动 7.30~7.35）
    #     乳酸 10                    -> pH 7.29（实测 7.30）
    #   该值属**经验标定**，非文献直接给出的常数，见 REFERENCES.md [R14]。
    _M_HB = 40.0
    _CO2_SOL = 0.0307                       # CO2 溶解度 mmol/L/mmHg
    _BE_HCO3 = 24.4                         # Van Slyke 方程的参考碳酸氢根

    def _solve_ph_from_be(self, be: float, paco2: float) -> float:
        """由碱剩余与 pCO2 反解 pH（Van Slyke 方程，二分法）。

        为什么不直接 HCO3 -> pH：碳酸氢盐只是缓冲体系的一部分。
        血红蛋白（全血约 30% 缓冲容量）、血浆蛋白、磷酸盐都会吸收 H+，
        使同样一份酸负荷下 HCO3- 少掉一部分、pH 少掉一部分。
        只按 HCO3 算 pH 会把酸中毒放大近一倍 —— 实测乳酸 15 时模型给
        pH 7.019，真实血液约 7.25（见 REFERENCES.md [R14]）。
        碱剩余把「所有缓冲对吸收完酸之后还剩多少碱」压缩成一个标量，
        再用它反解 pH，非碳酸氢盐缓冲就自动被计入了。
        """

        a = self._CO2_SOL * max(paco2, 5.0)
        k, m = self._K_HB, self._M_HB

        def f(ph: float) -> float:
            hco3 = a * (10.0 ** (ph - 6.1))
            return k * ((hco3 - self._BE_HCO3) + m * (ph - 7.4)) - be

        lo, hi = 6.50, 7.85
        flo, fhi = f(lo), f(hi)
        # 极端值落在外，直接钳到边界，保证不抛异常
        if flo > 0.0:
            return lo
        if fhi < 0.0:
            return hi
        for _ in range(24):          # 24 次二分 -> 区间 1.35 上精度约 8e-8
            mid = 0.5 * (lo + hi)
            if f(mid) > 0.0:
                hi = mid
            else:
                lo = mid
        return 0.5 * (lo + hi)

    def _acidbase(self, dt):
        """酸碱平衡：碱剩余（代谢分量） + pCO2（呼吸分量） -> pH。

        2026-11-16 重写。三处关键改动：
          1. 补上代谢性酸中毒（乳酸），此前 pH 只受 pCO2 影响；
          2. 改用 Van Slyke 方程反解 pH，**非碳酸氢盐缓冲对（血红蛋白、
             血浆蛋白、磷酸盐）被自动计入**，不再让 HCO3 独自承担全部酸；
          3. HCO3 由 pH 反算，不再作为独立状态量平滑 —— 它本就是
             pH 与 pCO2 的派生量，独立平滑会与 pH 自相矛盾。
        """

        s = self.s

        # ---- 1) 肾脏代偿（慢，小时级）----
        # pCO2 每高于 40 mmHg 10，HCO3- 升 3.5 mmol/L。真实肾脏代偿需
        # 数小时到数天，这里取 6 h（快端，避免长期偏酸）。
        # 肾代偿是「获得碱」，在碱剩余上体现为正值。
        chronic = max(0.0, s.paco2 - 40.0)
        comp_t = chronic / 10.0 * 3.5
        s.hco3_renal = approach_tau(s.hco3_renal, comp_t, dt, 6.0 * H2S)

        # ---- 2) 代谢分量：乳酸带走碱剩余 ----
        # 乳酸是强离子酸，每 1 mmol/L 使强离子差 SID 降 1 mEq/L，
        # 等价于碱剩余降 1 mmol/L。静息基线 1.0，只对超出部分计账。
        # 缓冲产生的额外 CO2 由通气排出（等碳酸缓冲段），不重复计入。
        acid_load = max(0.0, s.lactate - 1.0)
        be = s.hco3_renal - acid_load

        # ---- 3) 由碱剩余 + pCO2 反解 pH ----
        # 生理可达范围：碱中毒上限约 7.65（极端），酸中毒下限约 6.80
        s.ph = clamp(self._solve_ph_from_be(be, s.paco2), 6.80, 7.65)

        # ---- 4) HCO3 由 pH 与 pCO2 反算（保持自洽）----
        s.hco3 = clamp(self._CO2_SOL * max(s.paco2, 5.0) * (10.0 ** (s.ph - 6.1)),
                       2.0, 55.0)

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
        # 2026-11-16 改：上限 1.60 -> 2.20。
        # 旧上限使胰岛素在血糖约 11 mmol/L 时即饱和，此后处置能力不再随血糖
        # 上升 —— 实测两顿米饭叠加时血糖一路冲到 14.9 且仍在爬。
        # 文献：健康人餐后胰岛素可达空腹的 5~10 倍（空腹按本模型 0.30 计，
        # 对应上限 1.5~3.0）。取 2.20（约 7 倍空腹）落在区间内。
        s.insulin = clamp(0.30 + 0.50 * (s.glucose - 5.0) / 5.0
                          + incretin + amino_drive, 0.10, 2.20)

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
        # 2026-11-16 说明：曾把这里改成 Hill 方程（EC50 29 μU/mL，Rizza 1981
        # PMID 7018254），思路是该文献确实证明「肝糖输出半最大抑制 29 μU/mL、
        # 完全抑制约 60 μU/mL，显著低于外周利用的 55 μU/mL」。但直接套用后
        # 实测白米饭峰值从 8.11 升到 10.87（回归），原因是本模型的 insulin
        # 是 0~1.6 的相对量纲而非 μU/mL，两者不能直接换算代入。
        # 已回退为原线性式；若要真正落地该文献，需要先把胰岛素量纲整体
        # 换算到 μU/mL，属独立改动，不在本次范围。文献依据保留在
        # docs/血糖与睡眠文献依据.md A5 备查。
        hgp_suppress = clamp(1.0 - max(0.0, s.glucose - 5.0) * 0.30
                             - max(0.0, s.insulin - 0.30) * 0.80, 0.0, 1.0)
        # 门静脉葡萄糖负荷的直接抑制：肠道正在灌糖时，肝输出应当进一步下调。
        # 依据 Dimitriadis 2021（PMID 33419065）：餐后肝脏优先处置摄入葡萄糖
        # 的 50% 以上，从而限制血糖与胰岛素的急性升高 —— 也就是说餐后肝脏
        # 从「输出者」切换成「净摄取者」，这是独立于胰岛素的一条通路。
        # 门静脉葡萄糖负荷的直接抑制：肠道正在灌糖时肝输出应进一步下调，
        # 但幅度必须温和 —— 一试 1 - load/28 就过冲了：低 GI 食物入血慢，
        # 门静脉负荷长时间处于中位，肝输出被持续压掉大半，结果苹果只涨到
        # 5.2、牛奶 5.1，几乎不升糖（正常应是缓升到 6 上下）。
        # 真实机制里这条通路是「辅助」，主力仍是胰岛素（Rizza 1981 已给出
        # 明确的 EC50），故这里把系数收到 0.35，只做小幅修形。
        portal_load = float(getattr(s, "gut_glucose", 0.0) or 0.0) + \
            float(getattr(s, "gut_glucose_slow", 0.0) or 0.0)
        portal_suppress = clamp(1.0 - portal_load / 28.0 * 0.35, 0.0, 1.0)
        hgp_suppress = clamp(hgp_suppress * portal_suppress, 0.0, 1.0)
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
        # 2026-11-16：曾试着把这里改成 Hill 式（EC50 55 μU/mL，Rizza 1981），
        # 想与肝输出配对，但实测与既有线性式不匹配 —— 高 GI 峰值冲到 10.87，
        # 属回归，已回退。文献给的 EC50 是**方向性证据**（肝脏比外周敏感），
        # 而不是可以直接替换本模型相对单位系数的绝对值，因为 insulin 字段
        # 是 0~1.6 的相对量纲，不是 μU/mL。要做这件事必须先把胰岛素量纲
        # 整体换算到文献单位，属独立改动，不在本次范围。
        tissue_mmol_min = 0.19 * s.glucose * (0.55 + 0.45 * s.insulin)
        if s.exercising:
            tissue_mmol_min *= 1.8
        net_mmol_min = hepatic_mmol_min - tissue_mmol_min
        s.glucose = clamp(s.glucose + net_mmol_min / GLUCOSE_V * (dt / 60.0), 2.0, 25.0)

        # ---- 葡萄糖有效性（质量作用）：不依赖胰岛素的被动摄取 ----
        # 2026-11-16 补，闭合 issue #6「长时间连续高血糖」的收尾项。
        # 只作用于「高出静息基线的部分」，所以：
        #   静息 5.2、运动后 4.8、低血糖段 —— 本项为 0，既有标定一字不动；
        #   只有高糖段获得额外斜率，这是把 12.9 压回 10 量级需要的那个去路。
        # 单位：S_G[/min] × 超出量[mmol/L] × 分布容积[L] = mmol/min。
        ge_slope = float(getattr(self, "ge_slope", GE_SLOPE_DEFAULT))
        ge_floor = float(getattr(self, "ge_floor", GE_FLOOR_DEFAULT))
        ge_mmol_min = ge_slope * max(0.0, s.glucose - ge_floor) * GLUCOSE_V
        if ge_mmol_min > 0.0:
            s.glucose = clamp(s.glucose - ge_mmol_min / GLUCOSE_V * (dt / 60.0),
                              2.0, 25.0)

        # ---- 肾糖阈：血糖超过约 10 mmol/L 时经尿排糖（2026-11-16 补）----
        # 真实人体肾糖阈约 10 mmol/L（180 mg/dL）；超过后滤过的葡萄糖超出
        # 肾小管最大重吸收能力，多余部分随尿排出，是糖尿病/高血糖时的一条
        # 重要去路。旧实现只把它算进尿量（glu_urine_day_ml）用于渗透利尿，
        # **没有从血糖里扣除** —— 高血糖因此完全没有上限约束。
        # 系数 0.20 mmol/min per mmol/L 超出量：血糖 15.9 时约 1.2 mmol/min，
        # 与肾血浆流量约 7.5 L/h、最大重吸收约 2.08 mmol/min 的量级相符。
        # 该通路在 10 mmol/L 以下恒为 0，正常血糖段不受影响。
        renal_glu_mmol_min = max(0.0, s.glucose - 10.0) * 0.20
        if renal_glu_mmol_min > 0.0:
            s.glucose = clamp(s.glucose - renal_glu_mmol_min / GLUCOSE_V * (dt / 60.0),
                              2.0, 25.0)
        # 糖原账本（单位：g）。GLUCOSE_G_PER_MMOL = 180 mg = 0.18 g，
        # 旧实现漏了这个换算，把 mmol/min 直接当 g/min 扣，糖原消耗快 5.6 倍
        # （实测 400 g 在 10 h 内耗尽，真实需 24-48 h）。
        GLUCOSE_G_PER_MMOL = 0.18
        s.glycogen = clamp(s.glycogen
                           - hepatic_mmol_min * GLUCOSE_G_PER_MMOL * (dt / 60.0),
                           0.0, GLYCOGEN_MAX)
        # ---- 糖原储存：必须是血糖的真实去路（2026-11-16 修）----
        # 旧实现只在糖原账本上加，**从不从血糖扣除** —— 质量不守恒：
        # 糖原以约 224 g/h 冲到 500 上限，血糖却一点没降。两处后果叠加
        # （胰岛素饱和 + 糖原顶格），血糖失去全部去路，实测冲到 14.9。
        #
        # 文献依据（见 REFERENCES.md [R16]）：
        #   肝糖原约 100~120 g、肌糖原约 400 g，合计上限约 500 g —— 保留下限 500。
        #   餐后约 30% 葡萄糖由肝脏处置、15~20% 进入肌糖原；糖原合成是
        #   餐后葡萄糖处置的主要去路之一，不能只记账不扣糖。
        #
        # 速率上限取 2.6 mmol/min：高胰岛素钳夹实测全body最大处置约
        # 10~12 mg/kg/min（70 kg ≈ 3.9~4.7 mmol/min），其中外周摄取已由
        # tissue_mmol_min 承担，储存份额再取 2.6 会使总处置接近生理上限。
        # 趋近 500 g 上限时用 room 线性收敛，避免瞬间顶格。
        if s.glucose > 5.0 and s.glycogen < GLYCOGEN_MAX:
            room = clamp((GLYCOGEN_MAX - s.glycogen) / 60.0, 0.0, 1.0)
            store_mmol_min = min(2.6, max(0.0, s.glucose - 5.0) * s.insulin * 0.40) * room
            d_store = store_mmol_min * (dt / 60.0)      # mmol（本步）
            s.glucose = clamp(s.glucose - d_store / GLUCOSE_V, 2.0, 25.0)
            s.glycogen = clamp(s.glycogen + d_store * GLUCOSE_G_PER_MMOL, 0.0, GLYCOGEN_MAX)

        # ---- 糖原满位后的溢出：脂肪合成（DNL，见 DNL_* 常量与 [R18]）----
        # 糖原还有空位时储存优先，本项为 0；顶格后多余葡萄糖从这里出清，
        # 血糖因此不再靠"爬高"来匹配吸收速率。产出的脂肪不入任何账本（无脂肪模型）。
        _dnl_frac = clamp((s.glycogen - DNL_TRIGGER_FRAC * GLYCOGEN_MAX)
                          / ((1.0 - DNL_TRIGGER_FRAC) * GLYCOGEN_MAX), 0.0, 1.0)
        if _dnl_frac > 0.0 and s.glucose > DNL_FLOOR_MMOL:
            _dnl_mmol_min = min(DNL_CAP_MMOL_MIN,
                                DNL_GAIN_PER_MMOL * (s.glucose - DNL_FLOOR_MMOL)) * _dnl_frac
            if _dnl_mmol_min > 0.0:
                _dnl_g = _dnl_mmol_min * (dt / 60.0) * GLUCOSE_G_PER_MMOL
                s.glucose = clamp(s.glucose - _dnl_mmol_min / GLUCOSE_V * (dt / 60.0),
                                  2.0, 25.0)
                # 只为自证与排查保留的内存计数（不入存档、不上报）：
                # 按 3.17:1 折算成"本可合成的脂肪克数"，随后丢弃。
                self._dnl_glucose_g = float(getattr(self, "_dnl_glucose_g", 0.0)) + _dnl_g
                self._dnl_fat_g = float(getattr(self, "_dnl_fat_g", 0.0)) + _dnl_g / DNL_GLUCOSE_G_PER_FAT_G

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

    # ---- GI 对「入血比例」的调制（2026-11-16 新增）----
    # 文献依据（docs/血糖与睡眠文献依据.md A3，Vlachos 2020 PMID 32471238）：
    #   GL（血糖负荷）= GI × 碳水克数，共同决定餐后反应；
    #   低 GI 食物消化吸收更慢，餐后血糖与胰岛素反应更平缓。
    # 也就是说 GI 同时影响两件事：**入血有多快**（速率）和 **入血有多少**
    # （幅度）。本模型此前只做了速率侧（快慢双池），幅度侧完全没做 ——
    # 结果低 GI 食物只是被"拖长"，总负荷一分不少，峰值仍然偏高。
    #
    # 这里按 GI 把「有多少碳水以葡萄糖形式进入体循环」做线性缩放：
    #   GI 100（纯葡萄糖）→ 满额 0.70
    #   GI  50（中低）    → 约 0.55
    #   GI  30（很低）    → 约 0.45
    # 底部保留 0.45 的下限：碳水终究要被吸收，低 GI 不等于"不吸收"，
    # 剩下的差额按既有逻辑进肝糖原账本（见 _metabolic 里的 glycogen 项）。
    GI_RANGE_LOW = 30.0
    GI_RANGE_HIGH = 100.0
    CARB_TO_BLOOD_GI_FLOOR = 0.65   # GI 处于最低端时保留的比例（相对 CARB_TO_BLOOD）

    def _carb_to_blood_for_gi(self, gi: float) -> float:
        """按升糖指数折算「进入体循环的比例」。

        gi <= 0（非碳水食物）返回默认值；调用方应自行跳过这类条目。
        """
        try:
            gi = float(gi)
        except (TypeError, ValueError):
            return self.CARB_TO_BLOOD
        if gi <= 0.0:
            return self.CARB_TO_BLOOD
        t = clamp((gi - self.GI_RANGE_LOW) / (self.GI_RANGE_HIGH - self.GI_RANGE_LOW), 0.0, 1.0)
        factor = self.CARB_TO_BLOOD_GI_FLOOR + (1.0 - self.CARB_TO_BLOOD_GI_FLOOR) * t
        return self.CARB_TO_BLOOD * factor

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
            #
            # 2026-11-16 校准：旧值使餐后血糖峰值出现在 95~100 min，
            # 而文献实测健康人混合餐峰值在 46~50 min（Freckmann 2007,
            # PMID 19885137：24 名健康志愿者，餐后峰值时点平均 46-50 min）。
            # 峰值推迟一小时的直接原因是这条通路是四级串联
            # （胃 -> 十二指肠 -> 空肠 -> 肠糖池 -> 血），每级各有一个
            # 时间常数，旧参数把它们叠成了两倍生理延迟。
            # 这里按文献把「混合固体餐」的半排空从 90 min 收到 55 min
            # （真实混合膳食的胃半排空常见报道就在 60-90 min 区间下限，
            # 而碳水主导的一餐更快）。峰值时点因此前移，量级对上文献。
            liquid_bias = clamp(s.chyme_water / max(s.gastric_volume, 1.0), 0.0, 1.0)
            half_min = 40.0 - 22.0 * liquid_bias
            # 固形物按质量判定：蛋白 + 脂肪 + 碳水都算固形物。
            solid_g = s.chyme_protein + s.chyme_fat + s.chyme_carb * 0.25
            if solid_g < 1.0:
                half_min = 20.0                      # 纯液体饮
            frac = 1.0 - math.exp(-math.log(2.0) / (half_min * 60.0) * dt)
            frac = clamp(frac, 0.0, 0.25)

            s.gastric_volume = max(0.0, s.gastric_volume * (1.0 - frac))

            # 各成分按比例离胃；脂肪排空最慢（0.6 倍），蛋白 0.9 倍。
            #
            # 2026-11-16 GI 分账：高 GI 碳水在胃内也排空更快（淀粉已糊化、
            # 结构松散，机械与化学消化都更快），低 GI 碳水（膳食纤维多、
            # 结构致密）滞留在胃里的时间更长。这里让快账按 frac、慢账按
            # frac * 0.65 离胃，于是「同样 56 g 碳水，白米饭比苹果更早进
            # 小肠」，这正是文献里低 GI 食物血糖曲线更平缓的第一个原因。
            carb_out = s.chyme_carb * frac
            fast_pool = float(getattr(s, "chyme_carb_fast", 0.0) or 0.0)
            slow_pool = float(getattr(s, "chyme_carb_slow", 0.0) or 0.0)
            if fast_pool > 0.0 or slow_pool > 0.0:
                fast_out = fast_pool * frac
                slow_out = slow_pool * frac * 0.65
                s.chyme_carb_fast = max(0.0, fast_pool - fast_out)
                s.chyme_carb_slow = max(0.0, slow_pool - slow_out)
                # 两本账的离胃总量按比例缩放到与实际 carb_out 一致，
                # 保证总量守恒（chyme_carb 才是权威值）。
                pool_out = fast_out + slow_out
                if pool_out > 1e-9:
                    scale = carb_out / pool_out
                    fast_out *= scale
                    slow_out *= scale
                fast_frac_of_carb = fast_out / carb_out if carb_out > 1e-9 else 0.5
            else:
                fast_frac_of_carb = 0.5
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
            # GI 配比随食糜往下游传递：用碳水加权的滑动平均，
            # 避免一顿饭里先吃高 GI 后吃低 GI 时配比被最后一次覆盖。
            if carb_out > 1e-9:
                prev = float(getattr(s, "duodenum_carb_fast_frac", 0.5) or 0.5)
                prev_w = max(0.0, s.duodenum_carb - carb_out)
                tot_w = prev_w + carb_out
                s.duodenum_carb_fast_frac = (
                    (prev * prev_w + fast_frac_of_carb * carb_out) / tot_w
                    if tot_w > 1e-9 else fast_frac_of_carb
                )
            s.duodenum_protein += prot_out
            s.duodenum_fat += fat_out

        # ---- 十二指肠：酸中和 + 渗透感受，激发 secretin / CCK ----
        if s.duodenum_volume > 0.05:
            # 十二指肠转运很快（半时约 12 min），主要作用是让食糜接触
            # 胰液/胆汁并把信号传给腺体，不是主要吸收场所。
            # 2026-11-16 校准：12 -> 6 min。十二指肠主要是酸中和与渗透压感受，
            # 转运本身很快，不应成为糖吸收的限速环节。
            k_duo = 1.0 - math.exp(-math.log(2.0) / (6.0 * 60.0) * dt)
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
            s.glycogen = min(GLYCOGEN_MAX, s.glycogen + duo_carb * 0.15 * (1.0 - self.CARB_TO_BLOOD) * 0.8)
            s.gut_amino += duo_prot * 0.10
            s.fat_reserve = min(400.0, s.fat_reserve + duo_fat * 0.05)
            # 剩余进空肠
            s.jejunum_volume = min(900.0, s.jejunum_volume + max(0.0, s.duodenum_volume) * duo_frac)
            s.jejunum_carb += duo_carb * 0.85
            # 十二指肠 -> 空肠：GI 配比同样按碳水加权传递
            if duo_carb > 1e-9:
                d_frac = clamp(float(getattr(s, "duodenum_carb_fast_frac", 0.5) or 0.5), 0.0, 1.0)
                prev_j = clamp(float(getattr(s, "jejunum_carb_fast_frac", 0.5) or 0.5), 0.0, 1.0)
                prev_w = max(0.0, s.jejunum_carb - duo_carb * 0.85)
                tot_w = prev_w + duo_carb * 0.85
                s.jejunum_carb_fast_frac = (
                    (prev_j * prev_w + d_frac * duo_carb * 0.85) / tot_w
                    if tot_w > 1e-9 else d_frac
                )
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
            # 2026-11-16 校准：14 -> 8 min。空肠绒毛表面积巨大、刷状缘酶与
            # SGLT1/GLUT2 转运体密集，葡萄糖吸收本就是全消化道最快的一段。
            # 旧值 14 min 与胃排空、十二指肠串联后把肠糖池峰值推到 60 min，
            # 导致血糖峰值被迫推迟到 95-100 min（文献实测 46-50 min）。
            k_jej = 1.0 - math.exp(-math.log(2.0) / (8.0 * 60.0) * dt)
            jej_frac = clamp(k_jej, 0.0, 0.30)
            j_carb = s.jejunum_carb * jej_frac
            j_prot = s.jejunum_protein * jej_frac
            j_fat = s.jejunum_fat * jej_frac
            s.jejunum_carb -= j_carb
            s.jejunum_protein -= j_prot
            s.jejunum_fat -= j_fat
            s.jejunum_volume = max(0.0, s.jejunum_volume * (1.0 - jej_frac))
            # 空肠吸收：碳水 92%、蛋白 78%、脂肪 55%（脂肪主在回肠/淋巴）
            # GI 分速度入血（2026-11-16）：
            # 吸收进来的葡萄糖进入两个池子 —— 快池按短半时入血（高 GI，
            # 血糖快速上冲），慢池按长半时入血（低 GI，缓升缓降）。
            # 两者总量守恒，只是「入血的时间分布」不同，这正是 GI 的
            # 生理含义：同样的碳水总量，不同的血糖曲线形状。
            _fast_r = clamp(float(getattr(s, "jejunum_carb_fast_frac", 0.5) or 0.5), 0.0, 1.0)
            # GL 口径的幅度调制（2026-11-16）：按这顿的加权 GI 折算
            # 「有多少碳水以葡萄糖进入体循环」，其余按既有逻辑回补肝糖原。
            # 这一步补的是「幅度」，与上面的「速率」分离 —— 文献 A3 明确
            # 要求二者分开：GI 决定吸收快慢，GL(=GI×克数) 决定负荷大小。
            _gi_w = float(getattr(s, "gut_carb_gi_weighted", 0.0) or 0.0)
            _ctb = self._carb_to_blood_for_gi(_gi_w) if _gi_w > 0.0 else self.CARB_TO_BLOOD
            _absorbed_mmol = j_carb * 0.92 * 1000.0 / 180.0 * _ctb
            s.gut_glucose += _absorbed_mmol * _fast_r
            s.gut_glucose_slow = (float(getattr(s, "gut_glucose_slow", 0.0) or 0.0)
                                  + _absorbed_mmol * (1.0 - _fast_r))
            s.glycogen = min(GLYCOGEN_MAX, s.glycogen + j_carb * 0.92 * (1.0 - _ctb) * 0.8)
            s.gut_amino += j_prot * 0.78
            s.fat_reserve = min(400.0, s.fat_reserve + j_fat * 0.55)
            # 未吸收部分入回肠
            s.ileum_residue = min(300.0, s.ileum_residue
                                  + j_carb * 0.08 + j_prot * 0.22 + j_fat * 0.45)

        # ---- 回肠：PYY / GLP-1 刹车（"回肠刹车"）----
        # 回肠残渣按原有速率进入结肠，两处使用相同的转运量；
        # 结肠蓄满（400 g）时，尚未通过的残渣留在回肠。
        #
        # 【PR #13 · 作者 lingion】旧实现是两笔互不相干的账：回肠每小时减 4 g，
        # 结肠却另按 0.5 g/h 往上加 —— 两边不等，残渣既"凭空消失"又"重复累积"。
        # 缺陷是双向的：小份餐时残渣消失（实测高纤维三餐 4 天内 0 次排便），
        # 大份餐时结肠虚高（6~8 次/天）。现改为同一转运量，守恒。
        transit = min(s.ileum_residue, dt / H2S * 4.0,
                      max(0.0, 400.0 - s.colon_residue))
        s.ileum_residue -= transit
        s.colon_residue += transit
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
        # 2026-11-16：拆成快慢双池。旧实现只有一个池、半时 8 min，于是
        # 所有碳水同速入血 —— 苹果（GI 36）与白米饭（GI 73）的餐后曲线
        # 几乎重合（实测两者峰值都在 95 分钟左右）。GI 的本质就是
        # 「同一份碳水，入血的时间分布不同」，故这里按 GI 分两个半时：
        #   快池（高 GI） 半时 8 min  —— 葡萄糖/白面包/米饭，快速上冲
        #   慢池（低 GI） 半时 42 min —— 水果/豆类/全谷，缓升缓降
        # 吸收速率上限（见 ABSORB_CAP_MMOL_MIN）：快慢两池共享一份预算，
        # 超出载体能力的部分留在肠池里下一分钟继续吸，总量守恒不变。
        _abs_budget = float(getattr(self, "absorb_cap_mmol_min", ABSORB_CAP_MMOL_MIN)) * (dt / 60.0)
        if s.gut_glucose > 1e-9:
            # 半时 18 -> 8 min：门静脉到体循环的转运本身只需几分钟。
            k_abs = math.log(2.0) / (8.0 * 60.0)
            absorbed = min(s.gut_glucose * (1.0 - math.exp(-k_abs * dt)), _abs_budget)
            s.gut_glucose -= absorbed
            _abs_budget = max(0.0, _abs_budget - absorbed)
            s.glucose = clamp(s.glucose + absorbed / 14.0, 2.0, 25.0)
        if float(getattr(s, "gut_glucose_slow", 0.0) or 0.0) > 1e-9:
            # 慢池半时 42 min。低 GI 食物真正的降糖机制有一半在「延缓吸收」，
            # 另一半在「吸收后仍以较慢速度进入循环」，这里体现后半段。
            k_slow = math.log(2.0) / (42.0 * 60.0)
            absorbed_slow = min(s.gut_glucose_slow * (1.0 - math.exp(-k_slow * dt)),
                                _abs_budget)
            s.gut_glucose_slow -= absorbed_slow
            s.glucose = clamp(s.glucose + absorbed_slow / 14.0, 2.0, 25.0)

        # ---- 氨基酸池：蛋白 -> 糖原（糖异生）+ 缓慢供能 ----
        if s.gut_amino > 1e-9:
            k_aa = math.log(2.0) / (60.0 * 60.0)
            aa_out = s.gut_amino * (1.0 - math.exp(-k_aa * dt))
            s.gut_amino -= aa_out
            s.glycogen = min(GLYCOGEN_MAX, s.glycogen + aa_out * 0.35)

        # ---- 不可消化残渣入结肠：已并入上面的 transit（PR #13）----
        # 旧实现这里还有一笔"按 0.5 g/h 往结肠累加"的账，与回肠侧的 4.0 g/h
        # 不匹配，是重复累积的根源。已删除；位置保留说明，避免以后有人
        # "顺手补回来"。
        # 注：这里刻意不写出旧公式原文 —— digestion_check.py 的静态规则按文本
        # 匹配，写出来会被当成仍在使用的写法。

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
        if s.temp > FEVER_C:                                    # 发热额外蒸发
            insensible_l_day += (s.temp - FEVER_C) * 0.35
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
        """内分泌总入口（2026-11-18 拆分后只剩委托）。

        各条轴已迁到 body/endocrine/（按器官分科），本方法只负责保证：
          1) engine.step() 的调用点与顺序不变；
          2) 每条轴变成可独立测试的纯函数；
          3) 激素/受体的元数据集中在 endocrine.HORMONES / RECEPTORS。
        """
        endocrine.step(self.s, dt)

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
            # 强度分级产热（旧实现一律 +0.8）。
            base += (EX_TEMP_HIGH if str(getattr(s, "exercise_intent", "") or "mid") == "high"
                     else EX_TEMP_MID)
        # 散热负反馈：出汗蒸发带走热量，随 sweat（本身由核心温度驱动）增强。
        # 旧实现里 sweat 只扣体水、完全不参与体温 —— 于是运动+环境热+黄体期
        # 可以一路叠加到 38 ℃ 被判「低烧」，且没有把它拉回来的机制。
        base -= SWEAT_COOL * clamp(float(getattr(s, "sweat", 0.0) or 0.0), 0.0, 1.0)
        # ---- 睡眠期体温：拆成「整夜缓降」与「阶段微幅」两项（2026-11-22 修正）----
        # 旧实现是 base -= 0.30 * min(1, sleep_depth)。问题在于这个 0.30 是按
        # 「入睡后整夜下移 0.3~0.5℃」定的，输入却是逐阶段跳变的方波
        # （STAGE_DEPTH：深睡 1.00 / 浅睡 0.55 / REM 0.35），而体温时间常数只有
        # 5 分钟 —— 于是每进一次深睡就掉 0.10~0.15℃，一夜 22 次分期切换就是
        # 22 个锯齿。实测（2 日 23:04→3 日 07:00）：深睡段 36.65→36.50 等 5 段
        # 落差 0.10~0.15℃，整夜极差 0.51℃ 却全由切换造成。
        #
        # 真实生理是两件事、两个尺度：
        #   (a) 整夜 0.3~0.5℃ 的**单调缓降**（热惯性数十分钟级）；
        #   (b) 周期内 NREM/REM 的核心温差只有 0.02~0.05℃。
        # 故：慢项只由 sleeping（0/1，经 30 分钟一阶）驱动；阶段项限幅在 ±0.03℃。
        s.sleep_drive = approach(s.sleep_drive, 1.0 if s.sleeping else 0.0,
                                 dt, "temp_sleep")
        base -= 0.35 * s.sleep_drive
        if s.sleeping and s.sleep_depth > 0.0:
            base -= clamp(0.05 * (min(1.0, s.sleep_depth) - 0.55), -0.03, 0.03)
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
        # 战栗阈值（2026-11-22，按文献校核）：原为 36.5℃，那是「皮肤温低、觉得冷」
        # 的体感阈值，不是产热阈值。文献：失温定义为核心 <35.0℃，35.0~36.0 称
        # "low body temperature"，寒战是 <35℃ 的伴随症状。故取 35.5℃ 作起点
        # （留 0.5℃ 余量，不把 35.9 的正常偏冷当成寒战）。
        s.shiver = clamp((35.5 - s.temp) * 3.0 + 0.60 * cold_env, 0.0, 1.0)
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

        # ---- 瞬时情绪场自然回落（2026-11-18 新增）----
        # 外部每写一次 mood_valence 就抬一次，这里负责让它过去。
        # 指数衰减 + 死区归零：低于 0.05 直接清零，否则浮点尾数会永远挂着
        # 一个非零小值，让 WebUI 上"情绪"那一栏永远不归位。
        if s.mood_valence:
            s.mood_valence *= math.exp(-dt / MOOD_TAU)
            if abs(s.mood_valence) < 0.05:
                s.mood_valence = 0.0

        # ---- 心境底色（2026-11-22 新增）----
        # 只看身体状态，不看对话：睡够、精力足、不痛 → 正；睡债、疼痛、经前、
        # 极度口渴/饥饿、长期低落 → 负。-6 是"中性偏移"：一具健康的身体大体
        # 落在 0~+10，而不是把"没出事"当成"很高兴"。
        # 不把它接回 stress：stress 已经在用它自己的应力累积，再接一层
        # stress→底色→stress 的回路会让压力出现无依据的自振。
        tone_t = (10.0 * clamp((s.energy - 65.0) / 30.0, -1.0, 1.0)
                  + 6.0 * clamp((s.thermal - 50.0) / 25.0, -1.0, 1.0)
                  + 8.0 * clamp((float(getattr(s, "last_sleep_hours", 0.0)) - 6.5) / 2.5, -1.0, 1.0)
                  - 9.0 * clamp(float(s.sleep_debt) / 6.0, 0.0, 1.5)
                  - 0.45 * clamp(float(s.pain), 0.0, 100.0)
                  - 0.25 * clamp(float(getattr(s, "stress_load", 0.0)), 0.0, 40.0)
                  - 9.0 * clamp(float(s.pms_load), 0.0, 1.0)
                  - 0.15 * max(0.0, float(s.thirst) - 60.0)
                  - 0.20 * max(0.0, float(s.hunger) - 75.0)
                  - 0.08 * clamp(float(getattr(s, "depression", 0.0)), 0.0, 100.0)
                  - 6.0)
        s.mood_tone = approach_tau(s.mood_tone, clamp(tone_t, -60.0, 40.0),
                                   dt, MOOD_TONE_TAU)

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
                         + s.pms_load * 18.0
                         # 瞬时情绪：正面降压、负面升压。系数 0.55 使得
                         # valence 满值 ±100 时对应 ∓55 分压力，不至于一句话
                         # 就把她推到"濒临崩溃"（那是 90 分以上）。
                         # mood_valence 默认 0，故此式在未接入外部输入时
                         # 与改动前逐位一致。
                         - s.mood_valence * 0.55, 0.0, 100.0)
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

        # ---- 欣快 / 心境恶劣双向轴（2026-11-18 新增）----
        # 依据 LIFE.apk 症状枚举：Euphoria 与 Dysphoria 并列存在。
        # 本插件此前只有 depression 单向，做不出"开心到飞起"，只能做"低落"。
        #
        # 口径（工程映射，非文献直接给系数）：
        #   正向驱动 = 瞬时情绪场 + 内啡肽（愉悦/镇痛的共同介质）+ 低压力余量
        #   负向驱动 = 瞬时情绪场 + 高压段 + 疼痛 + PMS 负荷
        # 两者同时计算、互不排斥，但结构上互补：stress 低时正的占优，
        # stress 高时负的占优。不强行做成"和为零"，因为真实情绪可以
        # 既亢奋又焦躁（混合状态）。
        # 系数 0.8 -> 1.8：实测 0.8 时+45 只到 euphoria 7.2，接上 NLP 也看不见。
        # 1.8 时同样输入可到 80 档附近（上限 100），落在"很兴奋"区间。
        # 2026-11-22：加入心境底色（mood_tone）。没有它时，两次对话之间的
        # 正负驱动只剩"内啡肽 + 低压力余量"这一小截，实测 euphoria 贴着 3、
        # dysphoria 恒为 0 —— 界面就是两条直线。底色给的是"这段时间过得怎么样"。
        # 2026-11-22 二次校准：只加底色还不够 —— 底色 +5 时 euphoria 只有 6~9，
        # 画在固定的 0~100 轴上仍是一条贴地线（用户看到的"0 直线"有一半是显示问题）。
        # 给健康身体一个 15~25 的"正常带"（相当于"心情平稳偏好"），
        # 事件把正性推到 60~80，睡眠债/疼痛把负性推到 20~40 —— 这才在轴上看得出来。
        # 这一项随底色浮动（tone 每 +1 抬 0.5，上限 15），不写死常数：
        # 状态很差时它应该降到 4 以下，而不是让"很抑郁但还有 10 分欣快"并存。
        pos_drive = (max(0.0, s.mood_valence) * 1.8
                     + max(0.0, s.mood_tone) * 1.2
                     + clamp(10.0 + s.mood_tone * 0.5, 0.0, 15.0)
                     + clamp(s.endorphin, 0.0, 1.0) * 25.0
                     + max(0.0, 30.0 - s.stress) * 0.25)
        neg_drive = (max(0.0, -s.mood_valence) * 1.8
                     + max(0.0, -s.mood_tone) * 1.2
                     + max(0.0, s.stress - 50.0) * 0.6
                     + max(0.0, s.pain) * 0.4
                     + clamp(s.pms_load, 0.0, 1.0) * 20.0)
        # 用 EUPH_TAU 而不是 "affect"：当场感受与长期心境分开演进。
        s.euphoria = approach_tau(s.euphoria, clamp(pos_drive, 0.0, 100.0), dt, EUPH_TAU)
        # 负性比正性持久（见 DYSPH_TAU 的注释）：用各自的时间常数，而不是共用
        s.dysphoria = approach_tau(s.dysphoria, clamp(neg_drive, 0.0, 100.0), dt, DYSPH_TAU)

        # 低落（APK 症状枚举 Depression 的直接对应物）。
        # 口径：持续的应激负荷累积 + 当前压力水平，无社会/睡眠输入 —— 那些量
        # APK 里查无依据。stress_load 是应激对情绪的累积积分，τ=6h 缓慢消退。
        #
        # 2026-11-19 两处修正（v1.2.4 → v1.2.5）：
        #   1) 门槛 50 → 40。实测正常对话下压力峰值只有 34.9~43.6，加上疼痛/
        #      PMS 后才越过 40；原门槛 50 永远够不着，导致 depression 恒为 2.0
        #      （面板永远显示"情绪平稳"），长期心境这一维等于死的。
        #   2) 原式 `* 6.0` 与 `/ (6.0 * H2S)` 相互抵消，实际 τ=1 小时，
        #      与注释声明的 6 小时差 6 倍 —— "长期"心境只记一小时的事，等于中途失忆。
        #      现把增长项与消退项同除 6：稳态仍为 (stress - 门槛)，只改快慢，不动幅度。
        s.stress_load += (max(0.0, s.stress - 40.0)
                          - s.stress_load) * dt / (6.0 * H2S)
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
        # 酸碱：阈值必须随运动状态变化，不能用一个绝对数。
        # 真实重运动时 pH 会生理性降到 7.1~7.2（乳酸被 HCO3- 缓冲），
        # 这是正常代偿而非病理；运动后乳酸需 30~60 分钟清除，恢复期同理。
        # 用一个固定 7.30 会把每一次正常运动都报成「酸中毒」。
        #   - 运动中    ：只报真正危险的重度酸中毒（< 7.10）
        #   - 运动后 30 min 内：乳酸尚未清除，放到 7.20
        #   - 静息      ：常规阈值 7.30
        _last_ex = float(getattr(s, "last_exercise_t", 0.0) or 0.0)
        _recent_ex = _last_ex > 0.0 and (s.t - _last_ex) < 1800.0
        if s.exercising:
            acid_th = 7.10
        elif _recent_ex:
            acid_th = 7.20
        else:
            acid_th = 7.30
        fire("acidosis", s.ph < acid_th, "warn",
             f"酸中毒 pH {s.ph:.2f} pCO2 {s.paco2:.0f} 乳酸 {s.lactate:.1f}")
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