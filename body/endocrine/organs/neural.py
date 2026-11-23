"""中枢与自主神经的胺类/递质：多巴胺、5-羟色胺、腺苷、乙酰胆碱、内源性大麻素。

APK 依据：
  物质枚举（il2cpp_strings 5847 / 947 行）含 Epinephrine / Norepinephrine /
        Dopamine / Histamine / Serotonin / Melatonin / Acetylcholine /
        Adenosine / Anandamide
  激素表 Others 组含 Acetylcholine / Adenosine / Anandamide；
        Monoamines 组含 Norepinephrine / Dopamine / Histamine / Serotonin
  受体  5-HT1A / 5-HT2A / 5-HT2C / D1 / D2 / D3 / D4 / D5 / A1 / A2 /
        CB1 / CB2 / GABA A / NMDA Receptor

为什么把它们算进「内分泌」：APK 自己就把这些单胺与乙酰胆碱和肽类激素
放在同一张 HormoneType 表里。若只做腺体、不做这些，等于把 APK 表切掉一半。

边界：GABA 与谷氨酸（NMDA）的配体不在 HormoneType 枚举里，故其受体
只登记、不驱动（status 明写）。去甲肾上腺素归肾上腺髓质（organs/adrenal.py），
此处不重复定义 —— 同一字段两处写值是最典型的漂移来源。
"""
from ..base import hormone, receptor, register, seat, occupy
from ...state import clamp

ORGAN = ("neural", "神经胺类与递质", "中枢与自主神经")

register("dopamine", 300.0)
register("serotonin", 600.0)
register("adenosine", 900.0)
register("anandamide", 300.0)

# 腺苷静息值。受体占据要按它的静息口径反解 Kd，故与 BodyState 默认值同源。
ADENOSINE_REST = 0.40
# 咖啡因的竞争性拮抗强度：200 mg（约两杯咖啡）时 A1 占据被压低约 75%。
# 依据 APK 里 Caffeine 与 A1/A2 受体的并列关系（engine._autonomic 已按
# 「咖啡因阻断腺苷 A1/A2A」写过交感解释），此处把该说法落到可算的受体占据上。
CAFFEINE_BLOCK_A1 = 0.75
CAFFEINE_BLOCK_A2 = 0.65
CAFFEINE_FULL_MG = 200.0

HORMONES = {
    "dopamine": hormone("Dopamine", "Monoamines", "dopamine", 0.30,
                        "多巴胺，奖赏与运动启动"),
    "serotonin": hormone("Serotonin", "Monoamines", "serotonin", 0.40,
                         "5-羟色胺，心境与睡眠-觉醒"),
    "adenosine": hormone("Adenosine", "Others", "adenosine", ADENOSINE_REST,
                         "腺苷，睡眠压力累积量；咖啡因拮抗其受体"),
    "acetylcholine": hormone("Acetylcholine", "Others", None, 0.30,
                             "乙酰胆碱，副交感递质（由 endocrine.step_autonomic_receptors 推进）"),
    "anandamide": hormone("Anandamide", "Others", "anandamide", 0.10,
                          "内源性大麻素，运动后升高"),
}

RECEPTORS = {
    # ---- 自主神经受体（v1.3.0 就有，v1.3.1 补进登记表）----
    # 这三条由**交感/副交感张力**驱动，不是由某一条激素驱动，故 ligand 留空，
    # 由 endocrine.step_autonomic_receptors 用线性映射算（原实现即线性）。
    # 登记它们的目的不是重算，而是让「受体总表」完整：审计与文档都按登记表走，
    # 漏登一条，就等于那条受体永远不在检查范围内。
    # rest 给的是静息占据的参考值，Kd 仅作登记、线性映射不使用。
    "r_alpha1": receptor("Adrenoceptor(alpha1)", None, 0.10, n=1.0,
                         impl="endocrine.step_autonomic_receptors",
                         expr="血管平滑肌",
                         note="血管收缩；线性映射（v1.3.0 原实现，未改为 Hill）"),
    "r_beta1": receptor("Adrenoceptor(beta1)", None, 0.15, n=1.0,
                        impl="endocrine.step_autonomic_receptors",
                        expr="心肌",
                        note="心率提升；线性映射（v1.3.0 原实现）"),
    "r_m2": receptor("M Receptor", None, 0.20, n=1.0,
                     impl="endocrine.step_autonomic_receptors",
                     expr="窦房结、心房",
                     note="迷走减速；线性映射（v1.3.0 原实现）"),

    "r_d1": receptor("D1", "dopamine", 0.30, n=1.0, expr="纹状体、前额叶",
                     note="D1 家族（D1/D5）兴奋性"),
    "r_d2": receptor("D2", "dopamine", 0.30, n=1.0, expr="纹状体、垂体",
                     note="D2 抑制性；垂体 D2 抑制催乳素释放"),
    "r_5ht1a": receptor("5-HT1A", "serotonin", 0.40, n=1.0, expr="中缝核、海马",
                        note="5-HT1A 自身受体，介抗焦虑"),
    "r_5ht2a": receptor("5-HT2A", "serotonin", 0.40, n=1.0, expr="皮层、血小板",
                        note="5-HT2A 介知觉与血管收缩"),
    "r_5ht2c": receptor("5-HT2C", "serotonin", 0.40, n=1.0, expr="脉络丛、下丘脑",
                        note="5-HT2C 介食欲抑制"),
    "r_a1": receptor("A1", "adenosine", ADENOSINE_REST, n=1.0,
                     impl="neural.step_adenosine_receptors", expr="脑、心、肾",
                     note="A1 介睡眠压力与心动过缓；咖啡因竞争性拮抗"),
    "r_a2": receptor("A2", "adenosine", ADENOSINE_REST, n=1.0,
                     impl="neural.step_adenosine_receptors", expr="纹状体、冠脉",
                     note="A2A 介血管舒张；咖啡因竞争性拮抗"),
    "r_cb1": receptor("CB1", "anandamide", 0.10, n=1.0, expr="中枢、外周神经",
                      note="CB1 介食欲与欣快"),
    "r_cb2": receptor("CB2", "anandamide", 0.10, n=1.0, expr="免疫细胞",
                      note="CB2 介免疫调节"),
    "r_gaba": receptor("GABA A", "anandamide", 0.10, n=1.0, impl=None,
                       status="未驱动：配体 GABA 不在 HormoneType 枚举里",
                       expr="中枢", note="登记以对齐 APK；本插件无 GABA 通路"),
    "r_nmda": receptor("NMDA Receptor", "anandamide", 0.10, n=1.0, impl=None,
                       status="未驱动：配体谷氨酸不在 HormoneType 枚举里",
                       expr="中枢", note="登记以对齐 APK；本插件无谷氨酸通路"),
}


def step(s, dt):
    """推进四条神经化学量。乙酰胆碱不在这里 —— 它需要副交感张力 para，
    由 engine._autonomic 传入 endocrine.step_autonomic_receptors。"""
    # 多巴胺：正性情绪驱动、负性情绪抑制；运动小幅提升
    s.dopamine = seat(s.dopamine,
                      clamp(0.25 + 0.50 * clamp(float(s.euphoria) / 100.0, 0.0, 1.0)
                            - 0.25 * clamp(float(s.dysphoria) / 100.0, 0.0, 1.0)
                            + (0.06 if s.exercising else 0.0), 0.05, 1.20),
                      dt, "dopamine")

    # 5-HT：与褪黑素共用色氨酸通路，故与 melatonin 反相；经前下降（PMS 的 5-HT 假说）
    s.serotonin = seat(s.serotonin,
                       clamp(0.30 + 0.45 * (1.0 - clamp(float(s.melatonin), 0.0, 1.3))
                             - 0.30 * clamp(float(s.pms_load), 0.0, 1.0), 0.05, 1.20),
                       dt, "serotonin")

    # 腺苷：清醒时长线性累积（睡眠压力），睡眠中清除（半时约 15 分钟的近似取 900 s）
    if s.sleeping:
        ade_t = 0.10
    else:
        ade_t = clamp(0.15 + 0.85 * clamp(float(s.awake_hours) / 16.0, 0.0, 1.0),
                      0.10, 1.00)
    s.adenosine = seat(s.adenosine, ade_t, dt, "adenosine")

    # 内源性大麻素：运动与欣快
    s.anandamide = seat(s.anandamide,
                        clamp(0.08 + (0.35 if s.exercising else 0.0)
                              + 0.20 * clamp(float(s.euphoria) / 100.0, 0.0, 1.0), 0.02, 0.90),
                        dt, "anandamide")


def step_adenosine_receptors(s):
    """腺苷受体的竞争性拮抗（见模块头 Kd 与咖啡因口径）。

    咖啡因不是「减少腺苷」，而是占住受体让它进不去 —— 所以腺苷水平照算，
    只在**占据率**上打折。这也是「喝完咖啡腺苷照样在涨、一停就困」的机制。
    """
    block = clamp(float(s.caffeine) / CAFFEINE_FULL_MG, 0.0, 1.0)
    occ = occupy(s.adenosine, ADENOSINE_REST, 1.0)
    s.r_a1 = clamp(occ * (1.0 - CAFFEINE_BLOCK_A1 * block), 0.0, 1.0)
    s.r_a2 = clamp(occ * (1.0 - CAFFEINE_BLOCK_A2 * block), 0.0, 1.0)
