"""甲状腺：T4 分泌、T3（旧公式保留）、降钙素。

APK 依据：
  腺体  T4Gland / T3Gland / CalcitoninGland
  激素  T4 / T3 / Thyroid Hormone / Calcitonin
  受体  TSHR（甲状腺自身）、TR（全身核受体）、TRHR（垂体，登记在 central）

一个诚实的说明：T3 的公式是 v1.1 就有的「昼夜小幅摆动」（1.0 + 0.15*circ），
它**不是**由 T4 经 5'-脱碘酶转化来的。要改成真正的 T4→T3 转化，等于换掉
一条已标定的轴（心率、体温、代谢率都挂在 t3 上），风险不成比例。
故此处：T3 原样保留、T4 新增并跟随 TSH；两者之间的脱碘耦联标注为未接线，
等有单独评估再动。宁可留一条明写的缺口，不留一个看起来对、其实没接的假链条。
"""
from ..base import hormone, receptor, register, seat, touch
from ...state import clamp

ORGAN = ("thyroid", "甲状腺", "T4/T3 与降钙素")

register("thyroid", 1800.0)   # v1.3.0 原值：TSH/T3 共用
register("t4", 1800.0)
register("calcitonin", 600.0)

HORMONES = {
    "t3": hormone("T3", "T3Gland/T4Gland", "thyroid", 1.00,
                  "三碘甲状腺原氨酸（旧公式保留，见模块头）"),
    "t4": hormone("T4", "T4Gland", "t4", 1.00,
                  "甲状腺素，主要分泌产物，外周转 T3"),
    "tsh": hormone("TSH", "TSHGland", "thyroid", 1.00,
                   "促甲状腺激素（旧公式保留）"),
    "calcitonin": hormone("Calcitonin", "CalcitoninGland", "calcitonin", 0.20,
                          "降钙素，高血钙时由甲状腺 C 细胞分泌"),
}

RECEPTORS = {
    "r_tshr": receptor("TSHR", "tsh", 1.00, n=1.0, expr="甲状腺滤泡细胞",
                       note="TSHR 受 TSH 刺激分泌 T4"),
    "r_tr": receptor("TR", "t3", 1.00, n=2.0, expr="全身核受体（心、肝、肌、脑）",
                     note="TR 是核受体二聚体，介导代谢率与产热"),
}


def step_thyroid(s, dt):
    """甲状腺轴 v1.3.0 原实现，**逐字保留**（改动会让心率/体温基线漂移）。

    与下面的 step() 分开是必要的：如果把新旧两段写进同一个 step，
    新轴的调用循环会让 tsh/t3 在一步内被推进两次，数值立刻偏离标定。
    """
    s.tsh = seat(s.tsh, 1.0, dt, "thyroid")
    s.t3 = seat(s.t3, clamp(1.0 + 0.15 * s.circ, 0.7, 1.4), dt, "thyroid")


def step(s, dt):
    """推进本版新增的 T4 与降钙素（不碰 tsh/t3）。"""
    # T4：由 TSH 驱动分泌，TRH 时代已由 TSH 承载，不再叠加。
    t4_t = clamp(0.65 + 0.40 * s.tsh + 0.05 * s.circ, 0.30, 1.80)
    s.t4 = seat(s.t4, t4_t, dt, "t4")

    # 降钙素：C 细胞对**血钙升高**敏感（不是甲状腺激素轴），故不与 TSH 耦联。
    # 高钙 -> 分泌增加 -> 抑骨吸收（在 parathyroid.step 里生效）。
    cal_t = clamp(0.08 + 1.20 * max(0.0, s.ca - 2.40) / 0.35, 0.02, 1.50)
    s.calcitonin = touch(s.calcitonin, cal_t, dt, 600.0)
