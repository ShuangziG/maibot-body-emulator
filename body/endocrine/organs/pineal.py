"""松果体：褪黑素。

APK 依据：
  腺体  MelatoninGland；激素 Melatonin；受体 Melatonin Receptor
  指标  症状枚举含 Drowsiness / Alertness，二者与褪黑素时序同源

驱动取「黑暗」而不是「睡眠」：褪黑素的分泌由视网膜-视交叉上核通路控制，
人闭眼躺着不睡它照样升，开灯睡着它反而被压。故用 (1 - circ) 为主，
睡眠深度只作小幅加成。
"""
from ..base import hormone, receptor, register, seat
from ...state import clamp

ORGAN = ("pineal", "松果体", "褪黑素与昼夜")

register("melatonin", 900.0)

HORMONES = {
    "melatonin": hormone("Melatonin", "MelatoninGland", "melatonin", 0.20,
                         "褪黑素，黑暗驱动"),
}

RECEPTORS = {
    "r_melatonin": receptor("Melatonin Receptor", "melatonin", 0.20, n=1.0,
                            expr="视交叉上核、垂体",
                            note="MT1/MT2 合并为单一等效受体"),
}


def step(s, dt):
    """circ 在 04:00 为 0、16:00 为 1，故 (1-circ) 是「夜」的相位。"""
    dark = 1.0 - clamp(float(s.circ), 0.0, 1.0)
    sleep_boost = 0.25 * clamp(float(getattr(s, "sleep_depth", 0.0)), 0.0, 1.0)
    s.melatonin = seat(s.melatonin, clamp(0.04 + 0.90 * dark + sleep_boost, 0.02, 1.30),
                       dt, "melatonin")
