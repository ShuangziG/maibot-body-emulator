"""肝脏：血管紧张素原（RAAS 的真正起点）。

APK 依据：
  腺体  AngiotensinogenGland；器官枚举含 Liver
  激素  Angiotensinogen（排在 HormoneType 肽类第一位）

值得单独建模的理由：血管紧张素原不是「恒定底物」。糖皮质激素、雌激素、
炎症都会上调它的合成，这是 RAAS 活性的一条**上游调节**——临床上 ACEI 降压
效果的个体差异部分就来自这里。旧实现把它当成不存在的常数。
"""
from ..base import hormone, register, seat
from ...state import clamp

ORGAN = ("liver", "肝脏", "血管紧张素原")

register("angiotensinogen", 7200.0)

HORMONES = {
    "angiotensinogen": hormone("Angiotensinogen", "AngiotensinogenGland", "angiotensinogen",
                               1.00, "血管紧张素原，RAAS 的底物蛋白"),
}

RECEPTORS = {}


def step(s, dt):
    """糖皮质激素与雌激素上调、昼夜节律叠加。时间常数取 2 h（蛋白合成是慢变量）。"""
    agt_t = clamp(0.70 + 0.45 * clamp(s.cortisol, 0.0, 3.0)
                  + 0.20 * clamp(float(s.estrogen), 0.0, 1.0)
                  + 0.10 * s.circ, 0.30, 2.20)
    s.angiotensinogen = seat(s.angiotensinogen, agt_t, dt, "angiotensinogen")
