# -*- coding: utf-8 -*-
"""血液系统：血容量、红细胞生成、铁储备、血小板与止血（v1.4.0 新增）。

为什么单独成文件
----------------
v1.3.1 之前，"血容量"只是 _renal 里的一行：blood_vol = 5000 * TBW/42。
那不是一个状态，而是一个**函数** —— 失血、贫血、补液它一概表达不了，
红细胞与免疫细胞更是连字段都没有。LIFE.apk 元数据里只有
Plasma / Erythrocytes / ErythrocytesO2 / ErythrocytesCO2 / PlateletClot
这些**名字**，没有推进逻辑（判据见 apk_report/LIFE_APK_机制勘察.md：
名字有 ≠ 状态有 ≠ 系统有）。因此本模块是**自造系统**，不是移植；
定量依据见 REFERENCES.md [R36]~[R45]、[R62]。

与既有实现的分工
----------------
* 血浆量：由总体水导出（body_water 是守恒量），再乘 RAAS/ADH 的容量张力。
  不做独立积分 —— 独立的血浆池会与总体水重复记账，两边迟早对不上。
* 红细胞量：独立状态（mL），有生成、有衰老、有失血带走。
* 血容量 = 血浆量 + 红细胞量。旧公式里的 5000 mL 参考值改为 BV_REF_ML，
  各引用处按比值换算，基线行为逐位不变。

数值口径（与本模型 42 L 总体水对齐）
------------------------------------
文献值：成人血容量 60~70 mL/kg，女性偏低 [R37]；女性 Hct 36~46%，
红细胞寿命约 120 天 [R39]；血浆约占总体水 5.7%。
取 总体水 42 L <-> 血浆 2400 mL <-> 红细胞 1600 mL，
合血容量 4000 mL、Hct 40%、Hb 136 g/L（女性 120~150，[R46]）。
"""
from .state import clamp
from .dynamics import H2S, _tau, approach_tau

__all__ = [
    "BV_REF_ML", "PV_PER_TBW", "RBC_REF_ML", "HB_REF_G_L", "HB_PER_ML_RBC",
    "FE_PER_ML_BLOOD", "PLT_REF", "BV_FIELDS",
    "roll_cycle_total", "start_bleed", "derived", "anemia_grade",
    "step_plasma", "step_bleed", "step_rbc", "step_iron", "step_platelet",
    "registry_audit", "step",
]

# ---------------------------------------------------------------------------
# 常数（全部有出处）
# ---------------------------------------------------------------------------
BV_REF_ML = 4000.0           # 参考总血容量 mL（60 kg 女性 67 mL/kg，[R37]）
PV_PER_TBW = 2400.0 / 42.0   # 每升总体水对应的血浆 mL（基线占比，不是边际关系）
PV_REF_ML = 2400.0           # 参考血浆量 mL
# 组织间液 → 血管内的回填时间常数。真人失血后 0.5~1 L 的回填在数小时内完成，
# 取 3 h（τ）。太快会把急性失血抹平，太慢则血容量长期偏低。
PLASMA_TAU = 3.0 * 3600.0
RBC_REF_ML = 1600.0          # Hct 40% 时的红细胞容量 mL
HB_REF_G_L = 136.0           # 参考血红蛋白 g/L
HB_PER_ML_RBC = 0.34         # 每 mL 红细胞含血红蛋白 g（MCHC 340 g/L）
FE_PER_ML_BLOOD = 0.5        # 每 mL 全血含铁 mg（Hb 150 g/L × 0.335%）
PLT_REF = 250.0              # 血小板参考值 ×10^9/L（150~350，[R45]）

# 容量张力系数。幅度刻意做小：这些激素的主战场是肾脏排钠排水（_renal），
# 不该在这里二次计账，只需要能表达"失血早期 Hct 不立刻掉"。
TONE_ANG2 = 0.06
TONE_ADH = 0.05
TONE_ALDO = 0.04

# 红细胞生成：Hb 每偏离参考 30 g/L，骨髓产出放大 3 倍；上限 6 倍
# （重度贫血时骨髓可提高 5~8 倍产出，[R40]）。
ERYTHRO_GAIN = 3.0
ERYTHRO_MAX = 6.0
ERYTHRO_MIN = 0.20
RBC_LIFESPAN_DAY = 120.0     # [R39]

# 铁：基础丢失约 1 mg/天，行经女性另加经血铁；吸收分数 5%~35%，
# 储备越低吸收越高 [R41][R44]。炎症经 hepcidin 抑制吸收 [R62]。
FE_BASAL_LOSS_MG_D = 0.80
FE_ABSORB_BASE = 0.12
FE_ABSORB_SPAN = 0.18
FE_STORE_REF = 400.0
FE_STORE_EMPTY = 50.0

PLT_LIFESPAN_DAY = 7.0       # [R45]
PLT_REACTIVE = 0.35          # 炎症性反应性升高上限

CLOT_TAU_ON = 180.0          # 凝血块形成（秒）
CLOT_TAU_OFF = 1800.0        # 纤溶/回吸收（秒）
# 单步失血上限。防止 dt 很大时一步把血抽干（数值保护，不是生理结论）。
# 失血分级口径见 [R38]：I 级 <15% 血容量不改变生命体征，II 级 15%~30% 才出现
# 心动过速；本模型把一次意外出血的上限留在 I 级（见 config.BloodConfig）。
BLEED_STEP_CAP = 0.02

_tau("blood_plt", PLT_LIFESPAN_DAY * 86400.0)

# 氧输送（DO2）缺口的阈值。静息 DO2 约 820 mL/min（CO 4.7 L/min × CaO2 17.4），
# 组织摄氧约 250~300 mL/min，摄氧率 ~30% —— 这个水平远不是缺氧。
# 真到"组织缺氧"要等 DO2 掉到临界值附近（文献口径约 8~10 mL/kg/min，
# 即 55 kg 约 440~550 mL/min）。因此这里**不用线性比值**：线性会让静息
# 基线就带 0.09 的缺口，健康值每小时被白扣 0.4 分，两天掉 20 分。
# 取 700 起算、380 满格，与"贫血到 Hb 60 才明显缺氧"的观感一致。
DO2_HYPO_START = 700.0
DO2_HYPO_FULL = 380.0

# 字段注册表：registry_audit 与 WebUI 共用，保证"登记了就有字段"。
BV_FIELDS = {
    "plasma_vol": dict(unit="mL", note="血浆量（状态量，向总体水目标缓慢重分配）"),
    "rbc_mass": dict(unit="mL", note="红细胞容量，独立状态"),
    "hb": dict(unit="g/L", note="血红蛋白，由红细胞量导出"),
    "hct": dict(unit="%", note="红细胞压积 = 红细胞/全血"),
    "cao2": dict(unit="mL/dL", note="动脉血氧含量 1.34×Hb×SaO2"),
    "do2": dict(unit="mL/min", note="氧输送 = CO×CaO2×10"),
    "o2_deficit": dict(unit="0-1", note="氧输送缺口（组织缺氧信号）"),
    "epo": dict(unit="相对", note="促红素驱动信号 [R40]"),
    "iron_store": dict(unit="mg", note="储存铁（铁蛋白类比）[R41]"),
    "fe_intake_24h": dict(unit="mg/天", note="膳食铁摄入 EMA"),
    "fe_absorb_frac": dict(unit="0-1", note="当前铁吸收分数"),
    "fe_bleed_pending": dict(unit="mg", note="待扣铁（失血带来的铁流失）"),
    "retic_pct": dict(unit="%", note="网织红细胞百分比（骨髓产出指标）"),
    "platelet": dict(unit="10^9/L", note="血小板 [R45]"),
    "clot": dict(unit="0-1", note="止血/凝血进程 [APK: PlateletClot]"),
    "bleed_rate": dict(unit="mL/min", note="当前出血速率"),
    "bleed_target_ml": dict(unit="mL", note="本次出血总量目标"),
    "bleed_done_ml": dict(unit="mL", note="本次出血已完成量"),
    "bleed_site": dict(unit="", note="出血部位（展示用）"),
    "menstrual_loss_ml": dict(unit="mL", note="本周期累计经血"),
    "menstrual_target_ml": dict(unit="mL", note="本周期经血总量目标"),
    "blood_loss_24h": dict(unit="mL/天", note="失血速率 EMA"),
    "anemia": dict(unit="", note="贫血分级 none/mild/moderate/severe"),
}


# ---------------------------------------------------------------------------
# 入口：月经总量、外部出血
# ---------------------------------------------------------------------------
def roll_cycle_total(s, rng=None, gain: float = 1.0) -> float:
    """为一轮月经抽经血总量（mL）。

    人群研究：经血中位数约 30~40 mL，第 90 百分位约 80 mL，
    >80 mL 定义为月经过多 [R42]；铁需求据此抬到 2 mg/天量级 [R44]。
    用对数正态（sigma=0.55）近似右偏分布，夹在 10~150 mL。
    """
    import math
    import random as _random
    r = rng or _random
    try:
        total = 35.0 * math.exp(r.gauss(0.0, 0.55))
    except Exception:
        total = 35.0
    total = clamp(total * max(0.0, float(gain)), 0.0, 150.0)
    s.menstrual_target_ml = float(total)
    s.menstrual_loss_ml = 0.0
    return float(total)


def start_bleed(s, rate_ml_per_min: float, total_ml: float, site: str = "") -> None:
    """登记一次偶发出血（外伤/擦伤/鼻出血等）。

    total_ml 是本次出血的**上限**：出血按 rate 推进，凝血块把它逐步刹住，
    但总量绝不超过 total_ml —— 这样"会不会失血过多"由目标体积决定，
    不由 dt 与浮点行为决定，30 天回归才可复现。
    """
    total = max(0.0, float(total_ml))
    if total <= 0.0:
        return
    s.bleed_rate = max(0.0, float(rate_ml_per_min))
    s.bleed_target_ml = max(float(getattr(s, "bleed_target_ml", 0.0) or 0.0), total)
    s.bleed_site = str(site or "")
    s.clot = 0.0


# ---------------------------------------------------------------------------
# 导出量
# ---------------------------------------------------------------------------
def derived(s) -> dict:
    """由状态导出血液学指标（纯函数，不改 state）。

    Hb 按"红细胞体积 × MCHC"算，不做独立积分：两个独立积分会在补液时
    给出"Hb 不降而 Hct 降"的自相矛盾组合。
    """
    plasma = max(1.0, float(getattr(s, "plasma_vol", 0.0) or 0.0))
    rbc = max(1.0, float(getattr(s, "rbc_mass", 0.0) or 0.0))
    bv = plasma + rbc
    hct = clamp(rbc / bv * 100.0, 5.0, 75.0)
    hb = clamp(rbc * HB_PER_ML_RBC / (bv / 1000.0), 20.0, 220.0)
    sao2 = clamp(float(getattr(s, "sao2", 98.0) or 98.0), 0.0, 100.0)
    cao2 = 1.34 * (hb / 10.0) * (sao2 / 100.0)      # mL O2 / dL
    co = max(0.5, float(getattr(s, "co", 5.6) or 5.6))
    do2 = co * 10.0 * cao2                          # mL/min
    return {"blood_vol": bv, "hct": hct, "hb": hb, "cao2": cao2, "do2": do2}


def anemia_grade(hb: float) -> str:
    """贫血分级（WHO 成年非孕女性：<120 贫血、<90 中度、<60 重度）[R65]。"""
    if hb >= 120.0:
        return "none"
    if hb >= 90.0:
        return "mild"
    if hb >= 60.0:
        return "moderate"
    return "severe"


# ---------------------------------------------------------------------------
# 各子系统
# ---------------------------------------------------------------------------
def step_plasma(s, dt):
    """血浆量：状态量，向"总体水决定的目标"缓慢重分配（组织间液 ↔ 血管内）。

    为什么不能直接 plasma = 总体水 × 5.7%（初版就是这么写的，被压力测试打回）：
    那样"失血 1 L 血浆"只会让血浆量掉 57 mL —— 因为 5.7% 是**基线占比**，
    不是**边际关系**。实测 1800 mL 急性失血，血容量只掉 510 mL，等于把
    大失血抹平了。改成状态量之后：
      * 失血：血浆量与总体水各按 1:1 扣（见 step_bleed）；
      * 回填：按组织间液 → 血管内的方向缓慢逼近目标（真实机制即
        autotransfusion，失血后数小时内可回填约 0.5~1 L）；
      * 目标仍由总体水与 RAAS/ADH 张力决定，故脱水/补水依旧驱动血浆量。
    总水量不变时血浆量变化，等价于水在血管内外之间移动 —— 不破坏守恒。
    """
    ang2 = float(getattr(s, "ang2", 1.0) or 1.0)
    adh = float(getattr(s, "adh", 1.0) or 1.0)
    aldo = float(getattr(s, "aldosterone", 1.0) or 1.0)
    tone = clamp(1.0 + TONE_ANG2 * (ang2 - 1.0) + TONE_ADH * (adh - 1.0)
                 + TONE_ALDO * (aldo - 1.0), 0.80, 1.12)
    water = clamp(float(getattr(s, "body_water", 42.0) or 42.0), 20.0, 60.0)
    target = clamp(water * PV_PER_TBW * tone, 800.0, 3800.0)
    cur = float(getattr(s, "plasma_vol", PV_REF_ML) or PV_REF_ML)
    s.plasma_vol = clamp(cur + (target - cur) * min(1.0, dt / PLASMA_TAU), 600.0, 3600.0)


def step_rbc(s, dt):
    """红细胞生成与衰老。

        dRBC/dt = 产出 - 破坏
        破坏：RBC/120 天（随机衰老的一阶近似）
        产出：RBC_REF/120 天 × k，k 由 Hb 相对参考值决定
    平衡点满足 RBC = RBC_REF×k，k=1 时正是参考值 —— 稳态自洽。
    缺铁时骨髓受限：k 再乘 (0.3 + 0.7×储备/阈值)。
    """
    rbc = max(1.0, float(getattr(s, "rbc_mass", RBC_REF_ML) or RBC_REF_ML))
    d = derived(s)
    hb = d["hb"]
    s.epo = clamp(1.0 + (HB_REF_G_L - hb) / 20.0, 0.05, 5.0)
    k = clamp(1.0 + ERYTHRO_GAIN * (HB_REF_G_L - hb) / 30.0, ERYTHRO_MIN, ERYTHRO_MAX)
    store = float(getattr(s, "iron_store", 0.0) or 0.0)
    if store < FE_STORE_EMPTY:
        k *= 0.30 + 0.70 * clamp(store / FE_STORE_EMPTY, 0.0, 1.0)
    prod = RBC_REF_ML / RBC_LIFESPAN_DAY * k
    destroy = rbc / RBC_LIFESPAN_DAY
    rbc += (prod - destroy) * dt / 86400.0
    s.rbc_mass = clamp(rbc, 200.0, 3200.0)
    ratio = prod / (RBC_REF_ML / RBC_LIFESPAN_DAY)
    s.retic_pct = clamp(0.5 + 1.2 * (ratio - 1.0), 0.2, 8.0)


def step_bleed(s, dt, bus=None, cycle_enabled=True):
    """出血与止血：把失血写进守恒量（总体水、钠、红细胞、铁）。

    守恒：出血 V mL 全血
      * 血浆 V×(1-Hct) 从总体水扣，钠按 140 mmol/L 同步扣
      * 红细胞 V×Hct 从 rbc_mass 扣
      * 铁按 0.5 mg/mL 全血扣
    任何一项漏记，都会出现"失血了但体液没变"的假失血。
    """
    d = derived(s)
    hct = d["hct"] / 100.0
    bv = d["blood_vol"]
    cap = bv * BLEED_STEP_CAP

    # ---- 月经：按周期总量摊到 5 天，形状取 menstrual_flow（均值 0.5）----
    mvol = 0.0
    if bool(cycle_enabled) and bool(getattr(s, "menstruating", False)):
        target = float(getattr(s, "menstrual_target_ml", 0.0) or 0.0) or 35.0
        done = float(getattr(s, "menstrual_loss_ml", 0.0) or 0.0)
        if done < target:
            shape = max(clamp(float(getattr(s, "menstrual_flow", 0.0) or 0.0), 0.0, 1.0), 0.15)
            per_hour = target / (5.0 * 24.0 * 0.5)
            mvol = min(per_hour * shape * dt / H2S, target - done, cap)

    # ---- 偶发出血：有明确目标体积，凝血块负责把它刹住 ----
    evol = 0.0
    rate = max(0.0, float(getattr(s, "bleed_rate", 0.0) or 0.0))
    target_e = float(getattr(s, "bleed_target_ml", 0.0) or 0.0)
    done_e = float(getattr(s, "bleed_done_ml", 0.0) or 0.0)
    if rate > 0.0 and done_e < target_e:
        plt_frac = clamp(float(getattr(s, "platelet", PLT_REF) or PLT_REF) / 150.0, 0.0, 1.3)
        # 凝血块的形成速度与出血量有关：小伤口几分钟就止住，大出血止不住。
        # v1.4.0 压力测试抓到的缺陷：旧写法里凝血块饱和后 coag 归零，于是
        # 1800 mL 的急性失血在 ~300 mL 处"自己止住"，血容量只掉 311 mL ——
        # 既不符合生理，也让"大失血"这一档失去意义。
        # 现在：凝血时间随失血量线性拉长（<400 mL 用基准 τ），且 coag 留 0.25 的
        # 地板，保证出血一定朝目标推进，直到达到目标体积才停。
        _sev = clamp(target_e / 400.0, 1.0, 6.0)
        s.clot = approach_tau(s.clot, 1.0, dt, CLOT_TAU_ON * _sev)
        coag = max(0.25, 1.0 - float(getattr(s, "clot", 0.0))
                   * clamp(0.55 + 0.45 * plt_frac, 0.0, 1.0))
        evol = min(rate * coag * dt / 60.0, target_e - done_e, cap)
        done_e += evol
        s.bleed_done_ml = done_e
        if done_e >= target_e - 1e-9:
            s.bleed_rate = 0.0
            if bus is not None and target_e > 0.0:
                bus.emit_event("bleed_stop", "info",
                               "出血已止：累计 %.0f mL" % target_e, {})
    else:
        s.clot = approach_tau(float(getattr(s, "clot", 0.0) or 0.0), 0.0, dt, CLOT_TAU_OFF)

    vol = mvol + evol
    if vol <= 0.0:
        return
    if mvol > 0.0:
        s.menstrual_loss_ml = float(getattr(s, "menstrual_loss_ml", 0.0) or 0.0) + mvol
    plasma_ml = vol * (1.0 - hct)
    # 血浆量与总体水必须**各按 1:1 扣**：血浆是含在总体水里的一个隔室，
    # 出血失去的血浆既是"血管里的水"也是"身体的水"。只扣总体水（血浆由
    # 占比反推）会让 1 L 血浆损失只体现为 57 mL，等于把失血抹平。
    s.plasma_vol = max(400.0, float(getattr(s, "plasma_vol", PV_REF_ML) or PV_REF_ML) - plasma_ml)
    s.body_water = max(20.0, s.body_water - plasma_ml / 1000.0)
    s.na_mass = max(3000.0, s.na_mass - plasma_ml / 1000.0 * 140.0)
    s.rbc_mass = max(200.0, s.rbc_mass - vol * hct)
    s.fe_bleed_pending = float(getattr(s, "fe_bleed_pending", 0.0) or 0.0) \
        + vol * FE_PER_ML_BLOOD
    s.platelet = clamp(float(getattr(s, "platelet", PLT_REF) or PLT_REF) - vol * 0.02,
                       10.0, 900.0)
    # 失血速率 EMA（mL/天），供告警与界面展示
    rate_day = vol / max(dt, 1e-6) * 86400.0
    prev = float(getattr(s, "blood_loss_24h", 0.0) or 0.0)
    s.blood_loss_24h = prev + (rate_day - prev) * min(1.0, dt / (24.0 * H2S))


def step_iron(s, dt):
    """铁代谢：膳食吸收、基础丢失、造血与失血消耗。

    吸收分数随储备下降而升高（真实机制是肠上皮 hepcidin 门控），
    并受炎症抑制 —— 这就是"炎症性贫血"：炎症 → hepcidin ↑ →
    铁被锁在储备里、吸收也停 [R62]。

    月经过多与铁缺乏的因果链见 [R43]（经血铁损失 → 储备下降 → 缺铁性贫血），
    行经女性的铁需求抬到约 2 mg/天吸收量见 [R44]。
    """
    store = float(getattr(s, "iron_store", FE_STORE_REF) or 0.0)
    intake = max(0.0, float(getattr(s, "fe_intake_24h", 0.0) or 0.0))
    frac = FE_ABSORB_BASE + FE_ABSORB_SPAN * clamp(1.0 - store / FE_STORE_REF, 0.0, 1.0)
    if store > 900.0:
        frac *= 0.35
    inf = clamp(float(getattr(s, "infection", 0.0) or 0.0), 0.0, 1.0)
    frac *= clamp(1.0 - 0.60 * inf, 0.30, 1.0)
    frac = clamp(frac, 0.03, 0.40)
    s.fe_absorb_frac = frac
    lost_mg = float(getattr(s, "fe_bleed_pending", 0.0) or 0.0)
    s.fe_bleed_pending = 0.0
    loss = FE_BASAL_LOSS_MG_D * dt / 86400.0 + lost_mg
    s.iron_store = clamp(store + intake * frac * dt / 86400.0 - loss, 0.0, 1500.0)


def step_platelet(s, dt):
    """血小板：一阶回复 + 炎症性升高 + 生理性波动。

    为什么要加波动：目标值恒为 250、初值也是 250，于是"不流血也不感染"时
    血小板 30 天一动不动 —— 那不是生理量，只是一个初始值（30 天回归的
    死字段检查抓到了它）。健康人的血小板有昼夜波动与个体内变异，
    幅度约 ±10%，这里取 ±4%（保守）。方向与量级属估算，不影响任何判据。
    """
    plt = float(getattr(s, "platelet", PLT_REF) or PLT_REF)
    inf = clamp(float(getattr(s, "infection", 0.0) or 0.0), 0.0, 1.0)
    _circ = clamp(float(getattr(s, "circ", 0.5) or 0.5), 0.0, 1.0)
    target = PLT_REF * (1.0 + PLT_REACTIVE * inf) * (0.96 + 0.08 * _circ)
    s.platelet = clamp(approach_tau(plt, target, dt, PLT_LIFESPAN_DAY * 86400.0),
                       10.0, 900.0)


def step(s, dt, bus=None, cycle_enabled=True):
    """推进一整个血液步：血浆 -> 出血 -> 红细胞 -> 铁 -> 血小板 -> 导出量。"""
    step_plasma(s, dt)
    step_bleed(s, dt, bus, cycle_enabled)
    step_rbc(s, dt)
    step_iron(s, dt)
    step_platelet(s, dt)

    d = derived(s)
    s.blood_vol = d["blood_vol"]
    s.hct = d["hct"]
    s.hb = d["hb"]
    s.cao2 = d["cao2"]
    s.do2 = d["do2"]
    # 氧输送缺口：低于 700 起算、380 满格（见 DO2_HYPO_* 的说明）
    span = max(1.0, DO2_HYPO_START - DO2_HYPO_FULL)
    s.o2_deficit = clamp((DO2_HYPO_START - d["do2"]) / span, 0.0, 1.0)
    s.anemia = anemia_grade(d["hb"])


def registry_audit():
    """自检：注册表里的字段是否都在 BodyState 上。"""
    import dataclasses
    from .state import BodyState
    have = {f.name for f in dataclasses.fields(BodyState)}
    return ["BV_FIELDS." + name + " 不在 BodyState 字段里" for name in BV_FIELDS if name not in have]
