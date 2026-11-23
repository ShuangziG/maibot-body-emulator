"""甲状旁腺与钙磷轴：PTH、血钙、活性维生素 D 的靶点。

APK 依据：
  腺体  PTHGland
  激素  PTH / Calcitriol
  指标  物质枚举含 Calcium；症状枚举含 Hypercalcaemia / Hypocalcaemia
  受体  PTHR（骨、肾）、VDR（肠、骨、肾）

为什么要新增血钙字段 ca：PTH 的全部意义就是「把血钙拉回设定点」。
若只算 PTH 而没有钙，这条轴就是空转的 —— 一个没有靶量的激素，
和一条注释没区别。ca 是本包新增的唯一非激素生理量，它**不驱动任何既有体征**
（引擎里没有任何一处读 ca），所以对 v1.3.0 标定零影响。
"""
from ..base import hormone, receptor, register, seat
from ...state import clamp

ORGAN = ("parathyroid", "甲状旁腺 · 钙磷", "PTH 与血钙")

register("pth", 300.0)

# 血钙设定点 mmol/L。成人参考区间 2.15~2.55，取 2.35 为中心。
CA_SETPOINT = 2.35
CA_SLOPE = 0.35   # PTH 对血钙偏差的增益刻度（mmol/L）

HORMONES = {
    "pth": hormone("PTH", "PTHGland", "pth", 0.60,
                   "甲状旁腺激素，低钙时升高"),
}

RECEPTORS = {
    "r_pthr": receptor("PTHR", "pth", 0.60, n=2.0, expr="骨、肾小管",
                       note="PTHR 介导骨钙动员与钙重吸收"),
    "r_vdr": receptor("VDR", "calcitriol", 0.60, n=2.0, expr="肠、骨、肾",
                      note="VDR 是核受体，介导肠钙吸收"),
}


def step(s, dt):
    """推进钙磷轴。

    两条一阶方程首尾相接：低钙 -> PTH 升高 -> 动员骨钙与促活性维 D -> 血钙回升
    -> PTH 回落。增益刻意取小（每仿真分钟 0.055 mmol/L 量级），
    保证 dt=10 s 的显式欧拉不会振荡。
    """
    # PTH 由血钙偏差瞬时调节（半时约 5 分钟，是全身最快的内分泌反射之一）
    pth_t = clamp(0.60 + 2.40 * (CA_SETPOINT - s.ca) / CA_SLOPE, 0.05, 3.00)
    s.pth = seat(s.pth, pth_t, dt, "pth")

    # 血钙：PTH 动员 + 活性维 D 促吸收 - 降钙素抑吸收 - 骨缓冲回复力
    d_ca = (0.055 * max(0.0, s.pth - 0.60)
            + 0.040 * max(0.0, s.calcitriol - 0.60)
            - 0.045 * max(0.0, s.calcitonin - 0.20)
            - 0.020 * (s.ca - CA_SETPOINT))
    s.ca = clamp(s.ca + d_ca * dt / 60.0, 1.80, 3.00)
