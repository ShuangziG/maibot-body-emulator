"""状态定义：一具身体的全部可观测变量。

字段原则（2026-11-05 修订）：
  只保留 APK 元数据里能找到对应物的生理量。此前的「作息/睡眠债/孤独/卫生/
  自信/性欲」等字段在 LIFE.apk 的 il2cpp 元数据里查无对应（monoscripts 中
  sleep 命中 0，circadian/rhythm/schedule 命中 0），属于自造剧情层，已移除。
  判据见 apk_report/字段对照表.md。
"""
from dataclasses import dataclass, field, asdict
import math


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


@dataclass
class BodyState:
    """身体状态：APK 可测指标 + 维持数值闭合所需的少量中间量。"""

    # ---- 时间（仿真时钟，非生理量）----
    t: float = 0.0            # 仿真秒
    hour: float = 0.0         # 当日小时 0-24
    circ: float = 0.5         # 昼夜相位 0..1（由 hour 导出，用于交感/皮质醇/体温的基线摆动）

    # 注：circ 不是 APK 词，但它只是 hour 的确定性函数，不构成额外建模。
    # APK 里与之对应的是 Melatonin / Alertness / Drowsiness 的时序参数。

    # ---- 心血管 ----
    hr: float = 70.0          # 心率 bpm            [APK: HR]
    sv: float = 82.0          # 每搏输出量 mL        [推导]
    co: float = 5.6           # 心输出量 L/min       [推导]
    svr: float = 1450.0       # 体循环阻力           [推导]
    map: float = 93.0         # 平均动脉压 mmHg      [APK: BP]
    sbp: float = 120.0        # 收缩压               [APK: BP]
    dbp: float = 80.0         # 舒张压               [APK: BP]
    pp: float = 40.0          # 脉压                 [推导]
    blood_vol: float = 5000.0 # 循环血量 mL          [APK: Blood Volume]

    # ---- 呼吸 ----
    rr: float = 14.0          # 呼吸频率             [APK: RR]
    tidal: float = 500.0      # 潮气量 mL            [APK: TV]
    sao2: float = 98.0        # 血氧饱和度 %         [APK: SaO2]
    paco2: float = 40.0       # 动脉二氧化碳 mmHg    [APK: pCO2]

    # ---- 代谢 ----
    glucose: float = 5.0      # 血糖 mmol/L          [APK: BS]
    insulin: float = 0.3      # 胰岛素相对单位       [APK: Insulin]
    glycogen: float = 400.0   # 肝糖原 g             [APK: GS]
    atp: float = 1.0          # 细胞能量 0..1        [APK: ATP]
    lactate: float = 1.0      # 血乳酸 mmol/L        [APK: Lactic]

    # ---- 肾脏 ----
    gfr: float = 125.0        # 肾小球滤过率 mL/min   [APK: GFR / FF]
    urine_total: float = 0.0  # 累计尿量 mL          [APK: UO]
    bladder: float = 0.0      # 膀胱充盈 mL          [APK: Urinary Bladder]
    na: float = 140.0         # 血钠 mmol/L          [APK: Sodium]
    k: float = 4.2            # 血钾                 [APK: Potassium]
    osmol: float = 290.0      # 血浆渗透压           [APK: Plasma Osmolality]
    ph: float = 7.40          # 血液 pH              [APK: pH]
    # 动脉碳酸氢根：pH 不能独立写死。旧版 ph 恒 7.40 而 paco2 恒 65，
    # 按 Henderson-Hasselbalch 那对应 pH 7.25（失代偿性呼吸性酸中毒），
    # 两个自相矛盾的数同时钉死 —— WebUI 上同时显示「pCO2 65」和「pH 7.40」。
    # 现由 hco3 与 paco2 共同决定 pH，肾脏再缓慢调 hco3 做代偿。
    hco3: float = 24.0        # 动脉碳酸氢根 mmol/L

    # ---- 体液守恒量（真正的状态量，其余由此导出）----
    body_water: float = 42.0  # 总体水 L（TBW）
    na_mass: float = 5880.0   # 可交换钠总量 mmol

    # ---- 肝脏 / 胃肠 ----
    gut_full: float = 0.0     # 胃内容物 0..1        [APK: Stomach]
    # 结肠残渣 g：不可消化部分（纤维、部分蛋白/脂肪）累积到阈值触发排便。
    # 旧版没有任何排便通路，摄入 1239 kcal 固体食物后残渣凭空消失，
    # 事件种类只有 drink/meal/urination 三种。
    colon_residue: float = 0.0   # 结肠内残渣 g
    last_stool_t: float = 0.0    # 上次排便的仿真时刻
    gut_empty_t: float = 0.0
    bile: float = 1.0         # 胆汁                 [APK: Bile / Gall]

    # ---- 胃肠内容物（对应 APK: GastricVolume / AddContent / WaterAbsorption）----
    # 原版的食物不是「扣饥饿值」，而是作为内容物进胃、变食糜、由幽门按
    # 十二指肠/空肠/回肠的吸收类型吸收。这里用体积 + 逐项成分近似该过程。
    gastric_volume: float = 0.0   # 胃内体积 mL          [APK: GastricVolume]
    chyme_carb: float = 0.0       # 胃内待排空碳水 g
    chyme_protein: float = 0.0    # 蛋白 g
    chyme_fat: float = 0.0        # 脂肪 g
    chyme_water: float = 0.0      # 可自由水 mL          [APK: WaterAbsorption]
    chyme_na: float = 0.0         # 钠 mmol
    chyme_alcohol: float = 0.0    # 乙醇 g
    chyme_caffeine: float = 0.0   # 咖啡因 mg
    # ---- 小肠分段（APK: Duodenum / DuodenumVolume / Jejunum / Ileum）----
    # 依据 apk_report/il2cpp_strings.txt：Duodenum(5165) DuodenumVolume(5168)
    # Jejunum(9394) DuodenumGland(5167) JejunumGland(9395) 三处都在。
    # 旧实现把幽门排出的食糜直接倒进"待吸收入血"池，等于没有小肠 ——
    # 十二指肠的渗透压/酸负荷感受、空肠的吸收、回肠的刹车（PYY/GLP-1）
    # 全部缺失，饱腹只能靠胃体积一个量硬撑。
    duodenum_volume: float = 0.0   # 十二指肠内容物 mL     [APK: DuodenumVolume]
    duodenum_carb: float = 0.0     # 十二指肠内碳水 g
    duodenum_protein: float = 0.0  # 十二指肠内蛋白 g
    duodenum_fat: float = 0.0      # 十二指肠内脂肪 g
    jejunum_volume: float = 0.0    # 空肠内容物 mL          [APK: Jejunum]
    jejunum_carb: float = 0.0      # 空肠内待吸收碳水 g
    jejunum_protein: float = 0.0   # 空肠内待吸收蛋白 g
    jejunum_fat: float = 0.0       # 空肠内待吸收脂肪 g
    ileum_residue: float = 0.0     # 回肠末端残余 g（触发 PYY/GLP-1 刹车）

    # 已排空、正由肠壁吸收入血（一阶速率）
    gut_glucose: float = 0.0      # 待入血葡萄糖 mmol
    gut_amino: float = 0.0        # 待入血氨基酸（按蛋白 g 记）

    # ---- 胃肠激素（APK 腺体族：Gastrin/Secretin/CCK/GIP/PYY/GLP-1/Ghrelin）----
    # 相对浓度，1.0 ≈ 餐后生理峰值的一半。饥饿不再是"时间 + 血糖"两个量
    # 的合成，而是 ghrelin（促食欲）与 PYY/GLP-1/CCK/胃扩张（抑食欲）
    # 相互对抗的结果 —— 与 APK 的 CNS_Appetite / GetAppetite 结构一致。
    gastrin: float = 0.15        # 胃泌素（G 细胞，蛋白刺激）  [APK: GastrinGland]
    secretin: float = 0.10       # 促胰液素（S 细胞，酸刺激）  [APK: SecretinGland]
    cck: float = 0.10            # 缩胆囊素（I 细胞，脂肪/蛋白）[APK: CholecystokininGland]
    gip: float = 0.10            # 抑胃肽（K 细胞，碳水，促胰岛素）[APK: GIPGLand]
    pyy: float = 0.10            # 酪酪肽（回肠，饱腹刹车）    [APK: PYYGland]
    glp1: float = 0.10           # 胰高血糖素样肽-1（回肠，饱腹）[推导，APK 无单独串]
    ghrelin: float = 0.30        # 胃饥饿素（胃底，空腹升高）  [APK: Ghrelin]

    fat_reserve: float = 0.0      # 刚吸收、待氧化的脂肪 g（中间量）
    na_intake_24h: float = 120.0  # 膳食钠吸收速率 EMA（mmol/天，中间量）

    # ---- 嗜好品通路（APK 物质枚举含 Ethanol / Caffeine，指标含 BAC）----
    bac: float = 0.0              # 血乙醇 g/L（‰）      [APK: BAC]
    caffeine: float = 0.0         # 血浆咖啡因 mg        [APK: Caffeine]

    # ---- 内分泌轴 ----
    renin: float = 1.0        #                      [APK: Renin]
    ang2: float = 1.0         # 血管紧张素 II        [APK: Angiotensin / AGT]
    aldosterone: float = 1.0  #                      [APK: Aldosterone]
    cortisol: float = 0.5     #                      [APK: Cortisol / ACTH]
    acth: float = 0.5         #                      [APK: ACTH]
    adrenaline: float = 0.20  # 肾上腺素             [APK: Epinephrine]
    tsh: float = 1.0          #                      [APK: TSH / TRH]
    t3: float = 1.0           # 甲状腺激素           [APK: T3]
    adh: float = 1.0          # 抗利尿激素           [APK: Vasopressin]

    # ---- 受体占据（0..1）----
    r_alpha1: float = 0.10    # 血管 α1              [APK: Adrenoceptor]
    r_beta1: float = 0.15     # 心脏 β1              [APK: Adrenoceptor]
    r_m2: float = 0.20        # 迷走 M2              [APK: M Receptor]
    r_insulin: float = 0.30   # 胰岛素受体           [APK: Insulin Receptor]

    # ---- 体温 ----
    temp: float = 36.8        #                      [APK: Thermometer]
    sweat: float = 0.0        #                      [APK: Sweat Gland]
    shiver: float = 0.0

    # ---- 体感层（0..100，APK 里以症状枚举存在）----
    energy: float = 80.0      # 由 ATP 导出          [推导]
    hunger: float = 20.0      #                      [APK: Hunger]
    thirst: float = 20.0      # 由渗透压导出         [推导]
    stress: float = 20.0      #                      [APK: Stress]
    pain: float = 0.0         #                      [APK: Pain]
    thermal: float = 50.0     # 50 = 舒适            [APK: Cold / Hot]
    depression: float = 2.0   # 累积应激导出         [APK: Depression]
    stress_load: float = 0.0  # 应激累积（抑郁的输入，中间量）

    # ---- 室外环境（2026-11-14 新增）----
    # 由插件定时从 Open-Meteo 抓取，注入引擎。引擎不知道天气怎么来的，
    # 只知道「外面多冷多热、她现在在不在室外」。
    env_temp_c: float = 22.0        # 室外气温
    env_humidity: float = 50.0      # 室外相对湿度 %
    env_feels_c: float = 22.0       # 体感温度
    env_load: float = 0.0           # 室外冷热压迫 0~1
    env_indoor: bool = True         # 是否在室内（室内时压迫大幅衰减）
    env_wind_ms: float = 0.0        # 室外风速 m/s（室内为 0）
    env_updated_at: float = 0.0     # 上次更新时间
    # 室内环境（2026-11-14 预留接口，当前未接入数据源）
    # 现在室内统一按"中性、无冷热压迫"处理。将来若接入室内温湿度
    # （智能家居、手动配置、或按季节推算），把值写进这两个字段即可自动生效，
    # 引擎的 _thermo 已经会读它们，不需要再改引擎代码。
    env_indoor_temp_c: float | None = None    # None = 未知，按中性处理
    env_indoor_humidity: float | None = None  # None = 未知，按中性处理

    # ---- 睡眠（2026-11-08 新增；2026-11-14 加入四态睡眠周期）----
    # 旧版只有 external link_sleep 一个注入点，而部署配置里没有 [link] 段，
    # 于是永不为 0 —— 这具身体从来没有睡过觉：夜间照常饮水、起夜、交感不降。
    # 现改为引擎自持：按昼夜窗口入睡，睡眠深度 0..1，可被外部 link 覆盖。
    sleep_depth: float = 0.0      # 睡眠深度 0=清醒 1=深睡（由阶段派生）
    sleeping: bool = False        # 是否处于睡眠（sleep_depth > 0.5）
    sleep_debt: float = 0.0       # 睡眠债（小时），长期熬夜累积
    last_sleep_t: float = 0.0     # 上次入睡的仿真时刻

    # ---- 睡眠周期（2026-11-14 新增，四态分期）----
    # 参考 Oura/PSG 的分期口径：一个 NREM-REM 周期约 90 分钟，
    # 整夜 4~6 个周期。旧实现只有单一连续深度，无法表达"入睡后先深睡、
    # 后半夜 REM 变多"这一真实结构，睡眠债也因此没有质量维度可扣。
    # 阶段取值：awake / light / deep / rem
    sleep_stage: str = "awake"    # 当前睡眠阶段
    stage_elapsed: float = 0.0    # 当前阶段已持续（仿真秒）
    stage_index: int = 0          # 本次睡眠内的第几个周期（0 起）
    cycle_pos: float = 0.0        # 当前周期内位置（0..1）
    # 整夜累计（入睡时清零，醒来结算）
    dur_light: float = 0.0        # 浅睡累计（仿真秒）
    dur_deep: float = 0.0         # 深睡累计
    dur_rem: float = 0.0          # REM 累计
    dur_awake: float = 0.0        # 睡眠中途清醒累计
    sleep_quality: float = 0.0    # 本次睡眠质量评分 0~100
    sleep_onset_t: float = 0.0    # 本次入睡时刻（用于总时长）
    last_sleep_hours: float = 0.0 # 最近一次完整睡眠的时长（小时），运动门槛用
    # ---- 强制睡眠 / 微睡眠（2026-11-14）----
    # 依据 [R6] Durmer & Dinges 2005: "Microsleeps, sleep attacks, and lapses
    # in cognition increase with sleep loss"。连续清醒过久会出现不可抗拒的
    # 睡眠发作，不是靠意志能扛住的。
    awake_hours: float = 0.0      # 距上次睡着的连续清醒时长
    forced_sleep: bool = False    # 是否处于强制睡眠（微睡眠）状态
    forced_sleep_at: float = 0.0  # 本次强制睡眠开始时刻
    microsleep_count: int = 0     # 累计微睡眠次数

    # ---- 夜间唤醒（2026-11-14 新增）----
    # 真实睡眠不是一整段连续的：夜里会因消息、口渴、憋尿而短暂醒来。
    # 唤醒源分开记，便于 WebUI 区分「被吵醒」和「生理性醒」。
    wake_count: int = 0           # 本次睡眠内清醒次数（所有来源合计）
    wake_by_message: int = 0      # 因消息被唤醒次数
    wake_by_thirst: int = 0       # 渴醒次数
    wake_by_bladder: int = 0      # 憋尿醒次数
    wake_pending: bool = False    # 处于「被唤醒后等待重新入睡」状态
    wake_since: float = 0.0       # 本次唤醒的仿真时刻
    last_wake_reason: str = ""    # 最近一次唤醒原因（展示用）

    # ---- 月经周期（2026-11-14 新增，双相位机）----
    # 依据 LIFE.apk 元数据：MenstrualPhase(Follicular/Ovulation/Luteal) 与
    # UterusPhase(Proliferative/Secretory) 是两套并列的相位枚举，
    # 另有 Menstruation / FollicleMaturation / LutealMaturation / Proliferation。
    # 故这里也保留两条独立相位，而不是合成"周期第 N 天"。
    menarche_done: bool = False   # 是否已初潮（新身体需要先跑一轮）
    menstrual_day: float = 0.0    # 距上次行经开始的仿真天数
    menstrual_phase: str = ""     # follicular / ovulation / luteal
    uterus_phase: str = ""        # proliferative / secretory / menstrual
    menstruating: bool = False    # 是否正在行经
    estrogen: float = 0.0         # 雌激素相对浓度 [APK: EstrogenGland / Estradiol]
    progesterone: float = 0.0     # 孕激素相对浓度 [APK: ProgesteroneGland]
    follicle_maturing: bool = False   # 卵泡成熟中 [APK: FollicleMaturation]
    luteal_maturing: bool = False     # 黄体成熟中 [APK: LutealMaturation]
    menstrual_flow: float = 0.0   # 经血量 0..1（用于疼痛与体感）
    cycle_length: float = 28.0    # 个人周期长度（天），可被随机化
    # 前列腺素 E2/F2α 相对浓度。APK 元数据含 ProstaglandinF2 / ProstaglandinD2 /
    # Prostacyclin，是经期疼痛与子宫收缩的直接介质，也引起恶心、腹泻、
    # 血管扩张（面色苍白、发冷）。
    prostaglandin: float = 0.0
    # 内源性阿片（β-内啡肽）相对浓度。持续疼痛会触发内源性镇痛，
    # 这是"痛久了反而没那么难受"的生理基础。APK 元数据含 Peptide Hormones 族。
    endorphin: float = 0.1
    # 经期综合征（PMS/PMDD）强度 0..1，黄体后期升高，影响情绪与体感
    pms_load: float = 0.0

    # ---- 行为状态（维持数值闭合的脚手架，非 APK 功能）----
    exercising: bool = False
    # 日程驱动的运动（2026-11-14）
    exercise_intent: str = ""       # 日程希望的运动强度 "" / mid / high
    exercise_until: float = 0.0     # 本次运动窗口结束时刻
    exercise_on_phase: float = 900.0   # 跑多久（秒），默认 15 分钟
    exercise_off_phase: float = 300.0  # 歇多久（秒），默认 5 分钟
    exercise_cycle_t: float = 0.0   # 当前间歇相位计时
    exercise_refused: str = ""      # 上次拒绝运动的原因（展示用）
    _next_phase_change: float = 0.0 # 下一次换相位（跑<->歇）的时刻
    last_exercise_t: float = -99999.0  # 最近一次运动结束/进行中的时刻（乳酸清除用）
    exercise_start_t: float = 0.0   # 本次运动开始时刻（疲劳计算用）

    # ---- 延迟性肌肉酸痛 DOMS（2026-11-14 新增）----
    # 重要：DOMS 不是乳酸引起的。乳酸几小时内就清除，而 DOMS 峰值在运动后
    # 48-72 小时。机制是肌纤维微损伤 + 局部炎症（Hotfiel 2018）。
    # 依据文献：
    #   [R3] Cheung K, Hume P, Maxwell L. Delayed onset muscle soreness:
    #        treatment strategies and performance factors. Sports Med.
    #        2003;33(2):145-164. doi:10.2165/00007256-200333020-00005
    #   [R4] Hotfiel T, et al. Advances in DOMS: Part I. Sportverletz Sportschaden.
    #        2018;32(4):243-250. doi:10.1055/a-0753-1884
    #        → "peak soreness at 48-72 h post-exercise"
    #   [R5] Doma K, et al. The Repeated Bout Effect... J Strength Cond Res.
    #        2023;37(12):2504-2515. doi:10.1519/JSC.0000000000004628
    #        → 重复运动效应：一次运动后 DOMS 显著低于首次（SMD 0.51-1.23）
    doms: float = 0.0             # 当前 DOMS 强度 0~100
    doms_peak_at: float = 0.0     # 出现峰值的时刻（运动后 48-72h）
    doms_decay_at: float = 0.0    # 开始衰减的时刻（峰值后）
    doms_from_exercise: float = 0.0  # 本次触发 DOMS 的运动量（用于算峰值）
    exercise_habit: float = 0.0   # 运动习惯 0~1，越高越不易 DOMS（重复运动效应）
    last_meal_t: float = -6 * 3600.0
    last_drink_t: float = -2 * 3600.0
    last_urine_t: float = -3 * 3600.0
    meals: int = 0
    drinks: int = 0
    urinations: int = 0
    stools: int = 0
    last_meal_items: str = ""     # 最近一餐吃了什么（显示用）
    last_drink_items: str = ""    # 最近一次喝了什么（显示用）

    # ---- 健康 ----
    alive: bool = True          # 恒为 True：本建模不设死亡
    health: float = 100.0

    # ---- 濒死与急救（自定字段，非 APK 原生，见 README 依据表）----
    critical: bool = False          # 健康跌破阈值后的濒死状态
    critical_since: float = 0.0     # 进入濒死的仿真时刻
    hospital_until: float = 0.0     # 住院到哪个仿真时刻；<= t 表示不在院
    hospital_since: float = 0.0
    hospital_visits: int = 0
    hospital_reason: str = ""

    def snapshot(self):
        return asdict(self)


DEFAULT_STATE = BodyState()