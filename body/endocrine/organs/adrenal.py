"""肾上腺：皮质（糖/盐皮质激素、雄激素）与髓质（儿茶酚胺）。

APK 依据：
  腺体  AdrenalGland / CortisolGland / AlAldosteroneGland / EpinephrineGland
        NorepinephrineGland / AndrogenGland1 / AndrogenGland2
  激素  Cortisol / Aldosterone / Epinephrine / Norepinephrine /
        Androstenolone（DHEA）/ Steroids 族
  受体  GCR / MR / ACTHReceptor / α2 / β2

**旧实现逐字保留**：step_hpa（HPA 终产物与升糖）、step_adrenaline（SAM 轴）、
step_gcr_hormone（GCR 的 Hill）。这三条是 v1.3.0 已发布标定的组成部分，
搬运只换文件、不改公式 —— 拆分时验证过逐位一致。
"""
from ..base import hormone, receptor, register, seat, touch
from ...dynamics import approach, approach_tau, hill_occupancy
from ...state import clamp

ORGAN = ("adrenal", "肾上腺", "皮质与髓质")

register("ne", 45.0)
register("dhea", 7200.0)          # 肾上腺雄激素是缓慢节律，取 2 h
register("androstenedione", 3600.0)
register("cortisol", 600.0)       # v1.3.0 原值
register("acth", 300.0)           # v1.3.0 原值
register("aldo", 300.0)           # v1.3.0 原值

# GCR 的 Hill 参数：v1.3.0 原值，Kd=0.8（= 1.6 × cortisol 静息 0.5），与 base 口径一致
GCR_KD = 0.8
GCR_N = 2.0

HORMONES = {
    "cortisol": hormone("Cortisol", "CortisolGland/AdrenalGland", "cortisol", 0.50,
                        "糖皮质激素，HPA 终产物"),
    "aldosterone": hormone("Aldosterone", "AlAldosteroneGland", "aldo", 1.00,
                           "盐皮质激素，保钠排钾"),
    "acth": hormone("ACTH", "ACTHGland", "acth", 0.50,
                    "垂体促肾上腺皮质激素"),
    "adrenaline": hormone("Epinephrine", "EpinephrineGland", 30.0, 0.20,
                          "肾上腺素，SAM 轴，τ 直接给秒"),
    "norepinephrine": hormone("Norepinephrine", "NorepinephrineGland", "ne", 0.25,
                              "去甲肾上腺素，交感末梢与髓质共同释放"),
    "dhea": hormone("Androstenolone", "AndrogenGland1", "dhea", 0.30,
                    "DHEA（APK 名 Androstenolone），肾上腺雄激素前体"),
    "androstenedione": hormone("Androstenolone", "AndrogenGland2", "androstenedione", 0.15,
                               "雄烯二酮，外周可转睾酮/雌酮"),
}

RECEPTORS = {
    "r_gcr": receptor("Glucocorticoid_receptor", "cortisol", 0.50, n=2.0,
                      impl="adrenal.step_gcr_hormone", expr="全身（肝、肌、免疫、脑）",
                      note="Hill 方程 n=2；介导 HPA 负反馈与情绪效应"),
    "r_mr": receptor("MineralocorticoidReceptor", "aldosterone", 1.00, n=1.0,
                     expr="肾远曲小管、结肠",
                     note="MR 介导保钠排钾；对皮质醇也有亲和力，此处只按醛固酮算"),
    "r_acthr": receptor("ACTHReceptor", "acth", 0.50, n=1.0, expr="肾上腺皮质束状带",
                        note="ACTHR 介导皮质醇合成"),
    "r_beta2": receptor("Adrenoceptor(beta2)", "adrenaline", 0.20, n=1.0,
                        expr="支气管、血管平滑肌、子宫",
                        note="β2 介支气管舒张与外周血管舒张"),
    "r_alpha2": receptor("Adrenoceptor(alpha2)", "norepinephrine", 0.25, n=1.0,
                         expr="交感神经末梢（突触前）",
                         note="α2 是负反馈自身受体，抑制 NE 进一步释放"),
}


def step_hpa(s, dt):
    """下丘脑-垂体-肾上腺轴（v1.3.0 原实现，逐字保留）。

    目标值由昼夜相位、心理压力、高血糖三者合成；ACTH 跟随皮质醇 0.8 倍。
    """
    cort_t = clamp(0.5 + 0.09 * s.circ + 1.20 * (s.stress / 100.0)
                   + 0.30 * max(0.0, s.glucose - 8.0) * 0.2, 0.10, 3.0)
    s.cortisol = approach(s.cortisol, cort_t, dt, "cortisol")
    s.acth = approach(s.acth, clamp(s.cortisol * 0.8, 0.05, 2.0), dt, "acth")
    # 皮质醇升高血糖（0.004 mmol/L per 分钟）
    s.glucose += 0.004 * max(0.0, s.cortisol - 0.6) * dt / 60.0


def step_adrenaline(s, dt):
    """肾上腺髓质（SAM 轴）。运动时额外抬高。τ=30 s，比皮质醇快一个量级。"""
    adr_t = clamp(0.15 + 0.6 * (s.stress / 100.0)
                  + (0.5 if s.exercising else 0.0), 0.05, 1.5)
    s.adrenaline = approach_tau(s.adrenaline, adr_t, dt, 30.0)


def step_gcr_hormone(s):
    """糖皮质激素受体占据（GCR）。只计算并导出，不参与 stress 计算。"""
    s.r_gcr = hill_occupancy(s.cortisol, GCR_KD, GCR_N)


def step(s, dt):
    """推进肾上腺新增的激素：NE、DHEA、雄烯二酮。

    旧三步（HPA/SAM/GCR）由 endocrine/__init__ 按 v1.3.0 的顺序单独调用，
    不放进这里 —— 放进来的话，它们的相对次序会被新增量改变。
    """
    # 去甲肾上腺素：与肾上腺素同源但慢一档（τ 45 s vs 30 s），
    # 且应激时的**相对增幅更小**（NE 更多承担体位/持续张力，不负责爆发）
    ne_t = clamp(0.20 + 0.50 * (s.stress / 100.0)
                 + (0.25 if s.exercising else 0.0), 0.08, 1.20)
    s.norepinephrine = touch(s.norepinephrine, ne_t, dt, 45.0)

    # 肾上腺雄激素：ACTH 驱动 + 昼夜节律（清晨高，与皮质醇同向）
    s.dhea = seat(s.dhea, clamp(0.15 + 0.45 * s.circ + 0.20 * s.acth, 0.05, 1.20),
                  dt, "dhea")
    s.androstenedione = seat(s.androstenedione,
                             clamp(0.08 + 0.40 * s.acth, 0.02, 0.90),
                             dt, "androstenedione")
