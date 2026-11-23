"""皮肤：维生素 D 前体的光照生成（与肾、肝、甲状旁腺共同构成钙代谢）。

APK 依据：
  激素  Calcitriol（活性形式在 kidney 里算）
  器官枚举含 Skin / SweatGland；物质枚举含 Calcium 与 Vitamin? 无 —— 见下

诚实的边界：APK 的 HormoneType 枚举里**没有** 25-OH-D（骨化二醇）这一项，
它是 Calcitriol 的生理前体。本字段按「Calcitriol 链的上游」补入，
使钙代谢的三个环节（皮肤生成 / 肝 25-羟化 / 肾 1α-羟化）都有可观测量，
而不是只留一个凭空出现的 calcitriol。若不承认这一点，请把本字段当推导量看。
"""
from ..base import hormone, register, seat
from ...state import clamp

ORGAN = ("skin", "皮肤", "维生素 D 前体的光照生成")

register("vitd25", 86400.0)   # 一天：这是日内最慢的变量

HORMONES = {
    "vitd25": hormone("Calcitriol(上游)", "Skin", "vitd25", 0.45,
                      "25-羟基维生素 D，皮肤经 UVB 合成的维生素 D3 在肝内羟化产物"),
}

RECEPTORS = {}


def step(s, dt):
    """UVB 暴露 = 昼间相位 × 是否在室外。室内按 1/4 有效光照折算（玻璃挡 UVB）。"""
    indoors = bool(getattr(s, "env_indoor", True))
    uv = clamp(float(s.circ), 0.0, 1.0) * (0.25 if indoors else 1.0)
    s.vitd25 = seat(s.vitd25, clamp(0.30 + 0.70 * uv, 0.20, 1.10), dt, "vitd25")
