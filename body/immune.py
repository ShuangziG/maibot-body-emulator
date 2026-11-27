# -*- coding: utf-8 -*-
"""免疫系统：白细胞三分类、感染-炎症-发热闭环与生病行为（v1.4.0 新增）。

自造声明
--------
LIFE.apk 元数据里有 Infect / Inflame 两个**症状枚举**，但没有白细胞、
没有细胞因子、没有任何免疫推进逻辑（判据同 blood.py）。因此本模块是
**自造系统**，定量依据见 REFERENCES.md [R46]~[R52]、[R62]、[R63]。

建模范围（按需求确定：中性粒/淋巴/单核三类，不铺全谱系）
----------------------------------------------------------
1. 白细胞计数：三类各自有一阶动力学目标值，基线取成人参考区间中位
   （WBC 6.5、中性粒 3.6、淋巴 2.0、单核 0.45 ×10^9/L）[R46]。
2. 感染：一次自限性感染用"高斯型负荷曲线"表达（潜伏 → 峰值 →
   清除），峰值日与宽度按病毒/细菌分开；不是 logistic —— logistic
   清不干净，会留下一个永远接近 0 但非 0 的尾巴，30 天里越积越多。
3. 炎症：IL-6 一阶追随感染负荷，CRP 再一阶追随 IL-6（两级延迟，
   峰值滞后约 1 天，与内毒素试验的实测曲线一致 [R63]、[R47]）。
4. 发热：致热驱动由 IL-6 与感染负荷合成，交给 _thermo 叠加到调定点；
   发热的升/降由体温一阶惯性自然产生（升时寒战、降时出汗），
   不额外写"寒战"逻辑。
5. 生病行为：感染负荷直接压低精力、拉低心境、抑制食欲 ——
   这是 sickness behavior 的三条核心表现 [R49]。

易感性（谁更容易被感染）
------------------------
免疫张力 immune_tone 由四件事合成，全部有文献：
  * 睡眠不足     [R51] Irwin 2015 综述：睡眠缩短 → 炎症↑、抗病毒免疫↓
  * 慢性应激     [R50] 糖皮质激素抑制 NK/淋巴细胞功能
  * 规律运动     [R52] 中等强度运动提高免疫监视（过量则相反，故有上限）
  * 膳食质量     蛋白质/铁/维C 不足 → 免疫细胞增殖受限（由 nutrition 打分给）
"""
from .state import clamp
from .dynamics import H2S, _tau, approach_tau

__all__ = ["IMMUNE_FIELDS", "IMMUNE", "step_tone", "step_infection",
           "step_leukocyte", "step_cytokine", "step_fever", "registry_audit", "step"]

# ---------------------------------------------------------------------------
# 常数
# ---------------------------------------------------------------------------
# 白细胞基线（成人参考区间中位）[R46]
WBC_BASE = 6.5
NEUT_BASE = 3.6
LYMPH_BASE = 2.0
MONO_BASE = 0.45

# 感染曲线：峰值日与宽度（天）。病毒性上呼吸道感染症状峰值 2~3 天、
# 一周左右自限；细菌性感染峰值略晚、炎症指标更高。
VIRAL_PEAK_DAY = 2.2
VIRAL_WIDTH = 1.10
BACT_PEAK_DAY = 2.6
BACT_WIDTH = 1.30

# 每日新感染概率。成年人常见呼吸道感染约 2~4 次/年，取 0.012/天
# （≈4.4 次/年）作为基准，再由易感性系数放大/缩小。
INFECT_BASE_RATE = 0.012
IMMUNITY_DAYS = 30.0      # 感染后短期抵抗（同型再感染概率下降）

# 细胞因子动力学
IL6_TAU = 3.0 * H2S       # IL-6 半衰期数小时 [R47]
CRP_TAU = 19.0 * H2S      # CRP 半衰期约 19 小时，峰值滞后约 1 天 [R63]
CRP_PER_IL6 = 0.80        # CRP(mg/L) ≈ 0.8 × IL-6(pg/mL) 的稳态比例

# 中性粒反应快、淋巴慢
NEUT_TAU = 2.0 * H2S
LYMPH_TAU = 6.0 * H2S
MONO_TAU = 4.0 * H2S

_tau("immune_neut", NEUT_TAU)
_tau("immune_lymph", LYMPH_TAU)
_tau("immune_mono", MONO_TAU)

# 字段注册表（registry_audit / WebUI 共用）
IMMUNE_FIELDS = {
    "wbc": dict(unit="10^9/L", note="白细胞总数"),
    "neut": dict(unit="10^9/L", note="中性粒细胞 2.0~7.5 [R46]"),
    "lymph": dict(unit="10^9/L", note="淋巴细胞 1.0~4.0 [R46]"),
    "mono": dict(unit="10^9/L", note="单核细胞 0.2~0.8 [R46]"),
    "il6": dict(unit="pg/mL", note="白细胞介素-6 [R47]"),
    "crp": dict(unit="mg/L", note="C 反应蛋白 [R63]"),
    "infection": dict(unit="0-1", note="感染负荷（自限曲线）"),
    "infection_peak": dict(unit="0-1", note="本次感染峰值"),
    "infection_day": dict(unit="天", note="本次感染已进展天数"),
    "infection_type": dict(unit="", note="viral / bacterial"),
    "infection_site": dict(unit="", note="感染部位描述"),
    "infection_count": dict(unit="次", note="累计感染次数"),
    "immunity_days": dict(unit="天", note="感染后短期抵抗剩余天数"),
    "immune_tone": dict(unit="0-1", note="免疫张力（易感性主控量）"),
    "fever_drive": dict(unit="℃", note="致热驱动，叠加到体温调定点"),
    "sickness": dict(unit="0-1", note="生病行为强度"),
    "infection_roll_day": dict(unit="天", note="上次掷易感骰子的日序"),
}

# 部位表：让"感染"有名字可念，不至于界面上一句"感染中"
_SITES_VIRAL = [
    ("上呼吸道", "喉咙发痒、鼻塞、低热"),
    ("咽部", "咽痛、吞咽不适"),
    ("鼻腔", "流涕、打喷嚏"),
    ("胃肠道", "恶心、腹泻、食欲差"),
]
_SITES_BACT = [
    ("扁桃体", "咽痛明显、高热、脓点"),
    ("泌尿道", "尿频尿痛、腰酸"),
    ("皮肤伤口", "局部红肿热痛"),
    ("鼻窦", "面部胀痛、脓性鼻涕"),
]


def _pick(seq, rng):
    """在给定序列里按 rng 取一项；rng 缺失时退化为第一项。"""
    if not seq:
        return None
    try:
        return seq[rng.randrange(len(seq))]
    except Exception:
        return seq[0]


def step_tone(s, dt, sensitivity: float = 1.0):
    """免疫张力：睡眠、慢性应激、运动习惯、膳食质量的合成。

    范围 0.40~1.35：1.0 是健康基线；低于 0.7 属于"这段时间特别易感"，
    高于 1.2 相当于状态极佳。系数是标定值，方向全部有文献 [R50][R51][R52]。
    """
    sleep_debt = max(0.0, float(getattr(s, "sleep_debt", 0.0) or 0.0))
    sleep_loss = clamp(sleep_debt / 6.0, 0.0, 1.0)        # 债 6 h 封顶
    cort = clamp(float(getattr(s, "cortisol", 0.5) or 0.5), 0.0, 3.0)
    stress_term = clamp((cort - 0.6) / 1.4, 0.0, 1.0)
    habit = clamp(float(getattr(s, "exercise_habit", 0.0) or 0.0), 0.0, 1.0)
    score = float(getattr(s, "diet_score", 50.0) or 50.0)
    nut_term = clamp((score - 50.0) / 50.0, -1.0, 1.0)
    tone = (1.0
            - 0.30 * sleep_loss
            - 0.25 * stress_term
            + 0.10 * habit
            + 0.12 * nut_term)
    # sensitivity=0 时张力恒为 1.0（易感性不受睡眠/应激/运动/膳食影响）
    tone = 1.0 + (tone - 1.0) * clamp(float(sensitivity), 0.0, 2.0)
    s.immune_tone = clamp(tone, 0.40, 1.35)


def _spawn_infection(s, rng) -> None:
    """开始一次感染：定类型、定部位、定峰值。"""
    bacterial = False
    try:
        bacterial = rng.random() < 0.35
    except Exception:
        bacterial = False
    site, desc = _pick(_SITES_BACT if bacterial else _SITES_VIRAL, rng)
    peak = 0.55
    try:
        peak = 0.50 + 0.40 * rng.random()
    except Exception:
        peak = 0.70
    # 免疫张力低的人峰值更高（症状更重），高的人更轻
    peak *= clamp(1.25 - 0.35 * float(getattr(s, "immune_tone", 1.0) or 1.0), 0.6, 1.25)
    s.infection_type = "bacterial" if bacterial else "viral"
    s.infection_site = "%s（%s）" % (site, desc) if site else ""
    s.infection_peak = clamp(peak, 0.25, 0.95)
    s.infection_day = 0.0
    s.infection = 0.05
    s.infection_count = int(getattr(s, "infection_count", 0) or 0) + 1


def step_infection(s, dt, rng=None, per_day: float = None):
    """感染进程：掷骰子决定是否新发；已发作的按高斯曲线推进。"""
    day = float(getattr(s, "infection_day", -1.0) if getattr(s, "infection_day", -1.0) is not None else -1.0)
    peak = float(getattr(s, "infection_peak", 0.0) or 0.0)
    bact = str(getattr(s, "infection_type", "") or "") == "bacterial"
    tp = BACT_PEAK_DAY if bact else VIRAL_PEAK_DAY
    w = BACT_WIDTH if bact else VIRAL_WIDTH

    if day >= 0.0 and peak > 0.0:
        day += dt / 86400.0
        s.infection_day = day
        if day > tp * 2.0:
            # 自限结束：进入短期抵抗期
            s.infection = 0.0
            s.infection_day = -1.0
            s.infection_peak = 0.0
            s.infection_type = ""
            s.infection_site = ""
            s.immunity_days = IMMUNITY_DAYS
        else:
            z = (day - tp) / w
            load = peak * pow(2.718281828459045, -z * z)
            # 免疫张力高的人峰值被压一点、恢复更快
            s.infection = clamp(load, 0.0, 1.0)
    else:
        s.infection = 0.0

    # 抵抗期倒计时
    imm = float(getattr(s, "immunity_days", 0.0) or 0.0)
    if imm > 0.0:
        s.immunity_days = max(0.0, imm - dt / 86400.0)

    # ---- 每日掷一次易感骰子 ----
    sim_day = s.t / 86400.0
    last = float(getattr(s, "infection_roll_day", -1.0) or -1.0)
    if day < 0.0 and int(sim_day) != int(last):
        s.infection_roll_day = sim_day
        tone = clamp(float(getattr(s, "immune_tone", 1.0) or 1.0), 0.3, 1.4)
        base = INFECT_BASE_RATE if per_day is None else max(0.0, float(per_day))
        hazard = base * clamp(1.55 - tone, 0.45, 2.20)
        if float(getattr(s, "immunity_days", 0.0) or 0.0) > 0.0:
            hazard *= 0.35
        got = False
        try:
            got = rng.random() < hazard if rng is not None else False
        except Exception:
            got = False
        if got:
            _spawn_infection(s, rng)
            s.infection_roll_day = sim_day


def step_leukocyte(s, dt):
    """三类白细胞的目标值与一阶追随。

    病毒性：淋巴升、单核轻度升、中性粒基本不变；
    细菌性：中性粒显著升（可到 10 以上）、淋巴相对降。
    方向与临床血常规一致 [R46]。
    """
    inf = clamp(float(getattr(s, "infection", 0.0) or 0.0), 0.0, 1.0)
    bact = str(getattr(s, "infection_type", "") or "") == "bacterial"
    if bact:
        neut_t = NEUT_BASE + 7.0 * inf
        lymph_t = LYMPH_BASE * (1.0 - 0.25 * inf)
        mono_t = MONO_BASE + 0.40 * inf
    else:
        neut_t = NEUT_BASE * (1.0 + 0.15 * inf)
        lymph_t = LYMPH_BASE + 3.0 * inf
        mono_t = MONO_BASE + 0.50 * inf
    # 昼夜节律：白细胞计数在健康人身上也有明确的钟点波动（下午至傍晚偏高、
    # 清晨最低，幅度约 ±15%）。没有这一项时"白细胞"会是一个 30 天纹丝不动
    # 的常数 —— 那不是生理量，只是一个初始值。
    _circ = clamp(float(getattr(s, "circ", 0.5) or 0.5), 0.0, 1.0)
    _diurnal = 0.88 + 0.24 * _circ
    neut_t *= _diurnal
    lymph_t *= _diurnal
    mono_t *= _diurnal
    s.neut = clamp(approach_tau(float(getattr(s, "neut", NEUT_BASE) or NEUT_BASE),
                                neut_t, dt, NEUT_TAU), 0.3, 25.0)
    s.lymph = clamp(approach_tau(float(getattr(s, "lymph", LYMPH_BASE) or LYMPH_BASE),
                                 lymph_t, dt, LYMPH_TAU), 0.2, 12.0)
    s.mono = clamp(approach_tau(float(getattr(s, "mono", MONO_BASE) or MONO_BASE),
                                mono_t, dt, MONO_TAU), 0.05, 4.0)
    s.wbc = clamp(s.neut + s.lymph + s.mono, 0.6, 40.0)


def step_cytokine(s, dt):
    """IL-6 与 CRP 的两级延迟。

    没有延迟的话 CRP 会跟着感染曲线同步起落，而临床上 CRP 的峰值
    总在症状之后 1 天左右 [R63] —— 这个滞后本身就是"炎症还在发展"的信号。
    """
    inf = clamp(float(getattr(s, "infection", 0.0) or 0.0), 0.0, 1.0)
    il6_t = 1.5 + 48.0 * inf
    s.il6 = clamp(approach_tau(float(getattr(s, "il6", 1.5) or 0.0), il6_t, dt, IL6_TAU),
                  0.2, 500.0)
    crp_t = clamp(0.80 + CRP_PER_IL6 * s.il6, 0.5, 300.0)
    s.crp = clamp(approach_tau(float(getattr(s, "crp", 1.0) or 1.0), crp_t, dt, CRP_TAU),
                  0.2, 400.0)


def step_fever(s, dt, gain: float = 1.0):
    """致热驱动：交给 _thermo 叠加到调定点。

    幅度：一般感染 0.8~1.6 ℃，与本模型"发烧 38~39 ℃"的观感对齐
    （真人常见感染发热 38~39 ℃，[R48]）。
    """
    inf = clamp(float(getattr(s, "infection", 0.0) or 0.0), 0.0, 1.0)
    il6 = float(getattr(s, "il6", 1.5) or 1.5)
    drive = 1.45 * inf + 0.012 * max(0.0, il6 - 1.5)
    s.fever_drive = clamp(drive * clamp(float(gain), 0.0, 2.0), 0.0, 2.2)
    s.sickness = clamp(inf * 1.05, 0.0, 1.0)


def step(s, dt, rng=None, per_day: float = None,
         fever_gain: float = 1.0, sensitivity: float = 1.0):
    """推进一整个免疫步。顺序：张力 -> 感染 -> 白细胞 -> 细胞因子 -> 发热。"""
    step_tone(s, dt, sensitivity)
    step_infection(s, dt, rng, per_day)
    step_leukocyte(s, dt)
    step_cytokine(s, dt)
    step_fever(s, dt, fever_gain)


def registry_audit():
    """自检：注册表里的字段是否都在 BodyState 上。"""
    import dataclasses
    from .state import BodyState
    have = {f.name for f in dataclasses.fields(BodyState)}
    return ["IMMUNE_FIELDS." + n + " 不在 BodyState 字段里" for n in IMMUNE_FIELDS if n not in have]
