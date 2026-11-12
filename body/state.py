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

    # ---- 睡眠（2026-11-08 新增）----
    # 旧版只有 external link_sleep 一个注入点，而部署配置里没有 [link] 段，
    # 于是永不为 0 —— 这具身体从来没有睡过觉：夜间照常饮水、起夜、交感不降。
    # 现改为引擎自持：按昼夜窗口入睡，睡眠深度 0..1，可被外部 link 覆盖。
    sleep_depth: float = 0.0      # 睡眠深度 0=清醒 1=深睡
    sleeping: bool = False        # 是否处于睡眠（sleep_depth > 0.5）
    sleep_debt: float = 0.0       # 睡眠债（小时），长期熬夜累积
    last_sleep_t: float = 0.0     # 上次入睡的仿真时刻

    # ---- 行为状态（维持数值闭合的脚手架，非 APK 功能）----
    exercising: bool = False
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