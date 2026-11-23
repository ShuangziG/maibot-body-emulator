"""胰岛：胰岛素、胰高血糖素、淀粉素、生长抑素。

APK 依据：
  腺体  InsulinGland / GlucagonGland / SomatostatinGland
  激素  Insulin / Glucagon / Amylin / Somatostatin
  受体  Insulin Receptor / Glucagon Receptor / SS（somatostatin 受体）

注意 **胰岛素本身的分泌仍由 engine._metabolic 推进**（它是糖代谢闭环的一部分，
与肝糖原、糖异生同处计算才守恒）。本模块只补算与它共分泌/对抗的三种激素，
并第一次让 Insulin Receptor 真正被驱动 —— v1.3.0 里 r_insulin 恒为 0.30，
是个从未更新的常数，v1.2.5 因此把它从内脏页移除。现在它有配体了。
"""
from ..base import hormone, receptor, register, seat, touch
from ...state import clamp

ORGAN = ("pancreas", "胰岛", "糖代谢的内分泌侧")

register("glucagon", 300.0)
register("amylin", 300.0)
register("somatostatin", 900.0)

# 血糖参考值：与引擎其余部分共用 5.0 mmol/L 的基线口径
GLUCOSE_BASE = 5.0

HORMONES = {
    "insulin": hormone("Insulin", "InsulinGland", None, 0.30,
                       "胰岛素（分泌由 engine._metabolic 推进，本模块只登记）"),
    "glucagon": hormone("Glucagon", "GlucagonGland", "glucagon", 0.50,
                        "胰高血糖素，低血糖时由 α 细胞分泌"),
    "amylin": hormone("Amylin", "InsulinGland", "amylin", 0.30,
                      "淀粉素，与胰岛素共分泌，抑制胃排空"),
    "somatostatin": hormone("Somatostatin", "SomatostatinGland", "somatostatin", 0.20,
                            "生长抑素，δ 细胞旁分泌，抑制胰岛素与胰高血糖素"),
}

RECEPTORS = {
    "r_insulin": receptor("Insulin Receptor", "insulin", 0.30, n=2.0,
                          expr="肝、肌、脂肪",
                          note="n=2 对应胰岛素受体酪氨酸激酶的二聚结构"),
    "r_glucagon": receptor("Glucagon Receptor", "glucagon", 0.50, n=1.0,
                           expr="肝、肾",
                           note="GR 介糖原分解与糖异生"),
    "r_ss": receptor("SS", "somatostatin", 0.20, n=1.0,
                     expr="胰岛 α/β 细胞、垂体、胃肠",
                     note="SSTR 家族有 5 型，此处按单一等效受体建"),
}


def step(s, dt):
    """推进胰岛新增的三种激素。"""
    # 胰高血糖素：低血糖是主驱动（不是「胰岛素低」的直接反比 ——
    # 那是把β/α细胞当成同一个变量，会在餐后同时给出错误的高值与低值）
    glc_low = max(0.0, GLUCOSE_BASE - s.glucose) / 3.0
    s.glucagon = seat(s.glucagon, clamp(0.25 + 1.60 * glc_low, 0.05, 3.00),
                      dt, "glucagon")

    # 淀粉素与胰岛素共分泌（同一分泌颗粒），故跟随 insulin 而不是血糖
    s.amylin = touch(s.amylin, clamp(0.08 + 0.90 * clamp(s.insulin, 0.0, 2.0) / 2.0,
                                     0.02, 1.20), dt, 300.0)

    # 生长抑素：高血糖与高脂肪刺激 δ 细胞；作用是旁分泌抑制，
    # 这里只作为可观测的制动信号，不去回调胰岛素（会与引擎的糖闭环打架）
    ss_t = clamp(0.12 + 0.45 * max(0.0, s.glucose - 6.0) / 4.0
                 + 0.25 * clamp(s.fat_reserve / 40.0, 0.0, 1.0), 0.05, 1.00)
    s.somatostatin = seat(s.somatostatin, ss_t, dt, "somatostatin")
