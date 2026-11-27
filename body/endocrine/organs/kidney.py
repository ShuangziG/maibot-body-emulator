"""肾脏：肾素-血管紧张素-醛固酮、活性维生素 D、抗利尿激素的靶点。

APK 依据：
  腺体  ReninGland / Kidney
  激素  Renin / Angiotensinogen / Angiotensin I / Angiotensin II / Calcitriol
  受体  AGT Receptor（AT1）/ Vasopressin 2 R / ANPReceptor

**旧实现逐字保留**：step_raas 是 v1.3.0 的实现，搬运不改公式。
新增 Angiotensin I 与 Calcitriol。
"""
from ..base import hormone, receptor, register, seat
from ...dynamics import approach
from ...state import clamp
from ...blood import BV_REF_ML

ORGAN = ("kidney", "肾脏", "RAAS 与活性维生素 D")

register("raas", 90.0)
register("ang2", 60.0)
register("ang1", 60.0)
register("calcitriol", 3600.0)

HORMONES = {
    "renin": hormone("Renin", "ReninGland", "raas", 1.00, "RAAS 起始酶"),
    "ang2": hormone("Angiotensin II", "AngiotensinogenGland", "ang2", 1.00,
                    "血管紧张素 II（旧实现由肾素直接推，见 step_raas 注释）"),
    "ang1": hormone("Angiotensin I", "AngiotensinogenGland", "ang1", 0.80,
                    "血管紧张素 I，ACE 的底物"),
    "calcitriol": hormone("Calcitriol", "Kidney", "calcitriol", 0.60,
                          "活性维生素 D（1,25-(OH)2-D），肾 1α-羟化酶产物"),
}

RECEPTORS = {
    "r_at1": receptor("AGT Receptor", "ang2", 1.00, n=1.0,
                      expr="血管平滑肌、肾上腺、肾",
                      note="AT1 介血管收缩、醛固酮分泌与钠重吸收"),
    "r_avpr2": receptor("Vasopressin 2 R", "adh", 1.00, n=1.0,
                        expr="肾集合管",
                        note="V2 介水通道插入（AQP2），是 ADH 抗利尿的唯一通路"),
    "r_anp": receptor("ANPReceptor", "anp", 0.25, n=1.0,
                      expr="肾小球、集合管、血管",
                      note="ANP 受体介利钠利尿与血管舒张（对抗 RAAS）"),
}


def step_raas(s, dt):
    """肾素-血管紧张素-醛固酮系统（v1.3.0 原实现，逐字保留）。

    驱动：血容量不足、平均动脉压偏低。醛固酮的保钠动作不在这里 ——
    它必须与 engine 的排钠同处计算才能守恒。
    """
    renin_t = clamp(1.0 + (1.0 - s.blood_vol / BV_REF_ML) * 3.0
                    + (90.0 - s.map) / 40.0, 0.2, 4.0)
    s.renin = approach(s.renin, renin_t, dt, "raas")
    s.ang2 = approach(s.ang2, clamp(s.renin * 1.2, 0.2, 3.0), dt, "ang2")
    s.aldosterone = approach(s.aldosterone, clamp(s.ang2 * 0.8, 0.0, 3.0),
                             dt, "aldo")


def step(s, dt):
    """推进 Angiotensin I 与 Calcitriol。

    Ang I 与旧的 ang2 公式**并行**而不是串成一条链：ang2 的历史实现是
    renin*1.2 直通（不经过 Ang I），若把 ang2 改成从 ang1 来，等于换掉
    一条已标定的血管收缩轴。这里两个分支并存，Ang I 作为可观的中间产物，
    并在注释里明写这个缺口 —— 不假装链条是完整的。
    """
    s.ang1 = seat(s.ang1, clamp(0.80 * s.renin, 0.10, 3.00), dt, "ang1")

    # 活性维生素 D：肾 1α-羟化酶由 PTH 促进、高血钙与高磷抑制。
    # 时间常数取 1 h：这是日内慢变量，不该跟着分钟级波动跑。
    ct_t = clamp(0.25 + 0.70 * s.pth - 0.25 * max(0.0, s.ca - 2.55) / 0.35, 0.05, 1.60)
    s.calcitriol = seat(s.calcitriol, ct_t, dt, "calcitriol")
