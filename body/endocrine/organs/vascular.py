"""血管内皮与血小板：前列环素、血栓素、缓激肽、半胱氨酰白三烯。

APK 依据：
  腺体  PGI2Gland（前列环素）
  物质  Prostacyclin(PGI2) / ThromboxaneA2 / ThromboxaneB2 / Bradykinin /
        LeukotrieneC4 / LeukotrieneD4 / LeukotrieneE4 / ProstaglandinD2 / Eicosanoid
  受体  IP / TP / B1 / B2 / CYSLTR1 / CYSLTR2 / EP1-3 / H1

这一族的定位：它们是**局部介质**，半时以秒到分钟计，既不是腺体激素也不是
经典神经递质，但 APK 把它们和激素放在同一张表里。按器官归类，它们的产地是
内皮（PGI2 舒张、抗聚集）与血小板（TXA2 收缩、促聚集）—— 一对拮抗的轴。

代理量的坦白：本引擎没有独立的「炎症强度」标量（APK 的症状枚举有 Inflame，
但插件从未建模）。故缓激肽与白三烯分别用**酸中毒/运动**与**应激**作代理，
这是近似而不是测量，注释里写明，不冒充因果。
"""
from ..base import hormone, receptor, register, seat
from ...state import clamp

ORGAN = ("vascular", "血管内皮 · 血小板", "局部脂类介质")

register("prostacyclin", 180.0)
register("thromboxane", 120.0)
register("bradykinin", 90.0)
register("leukotriene", 300.0)

HORMONES = {
    "prostacyclin": hormone("Prostacyclin", "PGI2Gland", "prostacyclin", 0.20,
                            "前列环素 PGI2，内皮释放，抗血小板聚集、舒张血管"),
    "thromboxane": hormone("ThromboxaneA2", "Platelet", "thromboxane", 0.10,
                           "血栓素 A2，血小板释放，促聚集、收缩血管"),
    "bradykinin": hormone("Bradykinin", "Vascular", "bradykinin", 0.05,
                          "缓激肽，激肽系统终产物，致痛与血管扩张"),
    "leukotriene": hormone("LeukotrieneC4", "MastCell", "leukotriene", 0.05,
                           "半胱氨酰白三烯，肥大细胞释放，气道收缩与黏液分泌"),
}

RECEPTORS = {
    "r_ip": receptor("IP", "prostacyclin", 0.20, n=1.0, expr="血小板、血管平滑肌",
                     note="IP 受体介抗聚集与舒张"),
    "r_tp": receptor("TP", "thromboxane", 0.10, n=1.0, expr="血小板、血管平滑肌",
                     note="TP 受体与 IP 拮抗"),
    "r_b1": receptor("B1", "bradykinin", 0.05, n=1.0, expr="炎症组织（诱导表达）",
                     note="B1 在炎症/酸中毒时上调"),
    "r_b2": receptor("B2", "bradykinin", 0.05, n=1.0, expr="血管内皮、感觉神经",
                     note="B2 组成型表达，介急性痛与水肿"),
    "r_cylslt1": receptor("CYSLTR1", "leukotriene", 0.05, n=1.0, expr="气道平滑肌、肥大细胞",
                          note="CysLT1 是孟鲁司特等抗哮喘药的靶点"),
    "r_cylslt2": receptor("CYSLTR2", "leukotriene", 0.05, n=1.0, expr="气道、心脏",
                          note="CysLT2 介血管通透性与心肌收缩抑制"),
}


def step(s, dt):
    """内皮与血小板的两个方向各自逼近，不互相加减（真实系统由受体层面拮抗）。"""
    # PGI2：剪切应力（心输出量）+ 应激时的内皮激活
    shear = clamp((float(s.co) - 5.6) / 3.0, -1.0, 1.0)
    s.prostacyclin = seat(s.prostacyclin,
                          clamp(0.16 + 0.10 * shear + 0.25 * (s.stress / 100.0), 0.02, 1.20),
                          dt, "prostacyclin")

    # TXA2：交感激活与儿茶酚胺促进血小板活化
    s.thromboxane = seat(s.thromboxane,
                         clamp(0.06 + 0.30 * (s.stress / 100.0)
                               + 0.20 * clamp(s.adrenaline, 0.0, 1.5), 0.02, 1.10),
                         dt, "thromboxane")

    # 缓激肽：酸中毒（乳酸）与运动时激肽释放酶激活
    acid = clamp((float(s.lactate) - 1.0) / 6.0, 0.0, 1.0)
    s.bradykinin = seat(s.bradykinin,
                        clamp(0.03 + 0.60 * acid + (0.10 if s.exercising else 0.0), 0.01, 1.00),
                        dt, "bradykinin")

    # 白三烯：应激作炎症代理（无独立炎症标量，见模块头）
    s.leukotriene = seat(s.leukotriene,
                         clamp(0.03 + 0.35 * (s.stress / 100.0)
                               + 0.20 * clamp(s.lactate / 8.0, 0.0, 1.0), 0.01, 0.90),
                         dt, "leukotriene")
