"""下丘脑与垂体：三套释放激素 + 前叶六个促激素 + 后叶两个。

APK 依据（il2cpp 元数据，见 apk_report/il2cpp_strings.txt）：
  腺体  CRHGland / TRHGland / GnRHGland / ACTHGland / TSHGland
        FSHGland / LHGland / VasopressinGland
  激素  CRH / TRH / GnRH / ACTH / TSH / FSH / LH / Prolactin / Oxytocin /
        Vasopressin（Vasopressin = ADH，已有）
  受体  CRH1 / TRHR / GnRHR（垂体前叶表达）、ACTHReceptor（肾上腺）

本模块只负责**释放**这一侧。ACTH 与 TSH 的旧公式原样保留在下方
（step_hpa / step_thyroid 的旧实现分别散在肾上腺与甲状腺的语境里，
这里只搬运不改写），新增的 CRH/TRH/GnRH 等作为可观测的上游驱动量。
"""
from ..base import hormone, receptor, register, seat
from ...dynamics import approach
from ...state import clamp
from ...blood import BV_REF_ML

ORGAN = ("central", "下丘脑 · 垂体", "释放激素与促激素")

register("crh", 600.0)          # CRH 半时约 10 分钟（门脉血中极短，此处按中枢步进取整）
register("trh", 900.0)          # TRH
register("gnrh", 1800.0)        # GnRH 脉冲式，取整夜平均
register("fsh", 3600.0)         # FSH/LH 半时以小时计，取 1 h 步进
register("lh", 1800.0)
register("prolactin", 1800.0)
register("oxytocin", 120.0)     # 催产素脉冲式释放，消退快
register("adh", 300.0)          # 抗利尿激素（v1.3.0 原值，勿改）

HORMONES = {
    "crh": hormone("CRH", "CRHGland", "crh", 0.30,
                   "下丘脑室旁核促肾上腺皮质激素释放激素"),
    "trh": hormone("TRH", "TRHGland", "trh", 0.50,
                   "促甲状腺激素释放激素，夜间偏高"),
    "gnrh": hormone("GnRH", "GnRHGland", "gnrh", 0.30,
                   "促性腺激素释放激素，脉冲式"),
    "fsh": hormone("FSH", "FSHGland", "fsh", 0.40,
                   "卵泡刺激素，卵泡期升高、排卵前次峰"),
    "lh": hormone("LH", "LHGland", "lh", 0.30,
                   "黄体生成素，排卵前骤升形成 LH 峰"),
    "prolactin": hormone("Prolactin", "Prolactin(APK)", "prolactin", 0.30,
                         "催乳素，睡眠期升高、多巴胺抑制其释放"),
    "oxytocin": hormone("Oxytocin", "Oxytocin(APK)", "oxytocin", 0.10,
                        "催产素，正性情绪与亲密接触时升高"),
}

RECEPTORS = {
    # 垂体前叶三种释放激素受体：单体 GPCR，n=1
    "r_crh1": receptor("CRH1", "crh", 0.30, n=1.0, expr="垂体前叶促肾上腺皮质细胞",
                       note="CRH1 介导 ACTH 释放"),
    "r_trhr": receptor("TRHR", "trh", 0.50, n=1.0, expr="垂体前叶促甲状腺细胞",
                       note="TRHR 介导 TSH 释放"),
    "r_gnrhr": receptor("GnRHR", "gnrh", 0.30, n=1.0, expr="垂体前叶促性腺细胞",
                        note="GnRHR 介导 FSH/LH 释放"),
}


def step(s, dt):
    """推进下丘脑-垂体轴的新增释放激素。

    旧的 ACTH / TSH 公式不在这里 —— 它们在 adrenal.step_hpa 与
    thyroid.step_thyroid 里，且必须保持逐位一致（v1.3.0 标定依赖）。
    """
    # ---- 下丘脑三套释放激素 ----
    # CRH：昼夜相位 + 心理应激。与皮质醇的负反馈在真机上由皮质醇抑制 CRH，
    # 这里皮质醇上升不反压 CRH：旧 HPA 轴没有负反馈项，若此处单方面补上，
    # 等于给已标定的闭环新增一条未知增益的回路（可能发散）。留作另案。
    crh_t = 0.10 + 0.55 * s.circ + 0.75 * (s.stress / 100.0)
    s.crh = seat(s.crh, crh_t, dt, "crh")

    # TRH：夜间升高（对应 TSH 的夜间峰）
    s.trh = seat(s.trh, 0.20 + 0.60 * s.circ, dt, "trh")

    # GnRH：以雌激素负反馈为主，排卵前解除抑制形成峰。
    #
    # 关键的边界：月经周期**没开**时 estrogen 恒为 0。若照样用「雌激素低 ->
    # 解除负反馈」这条公式，FSH/LH 会停在 0.86/0.80 的高位 —— 那是**去势/绝经**
    # 的内分泌象，不是一具 18 岁身体的样子。周期关闭 ≠ 卵巢被摘掉，
    # 它只是本插件不模拟这一层。故此处显式分成两支，并如实写进注释与文档。
    e2_pull = clamp01(float(s.estrogen))
    cycle_on = bool(str(getattr(s, "menstrual_phase", ""))) or e2_pull > 1e-9
    if not cycle_on:
        # 周期未启用：给一套与「周期中期」等价的基线，只保留昼夜与应激调制
        s.gnrh = seat(s.gnrh, 0.30, dt, "gnrh")
    else:
        gnrh_t = 0.15 + 0.75 * (1.0 - 0.70 * e2_pull)
        if str(getattr(s, "menstrual_phase", "")) == "ovulation":
            gnrh_t += 0.35
        s.gnrh = seat(s.gnrh, gnrh_t, dt, "gnrh")

    # ---- 垂体前叶：FSH / LH / 催乳素 ----
    if not cycle_on:
        # 周期关闭：固定在中位水平，不让 FSH/LH 出现去势式的高位平台
        s.fsh = seat(s.fsh, 0.40, dt, "fsh")
        s.lh = seat(s.lh, 0.30, dt, "lh")
    else:
        # FSH 跟随 GnRH，高雌激素时被压制（负反馈）
        s.fsh = seat(s.fsh, clamp01(0.10 + 0.85 * s.gnrh) * (1.0 - 0.35 * e2_pull),
                     dt, "fsh")
        # LH 同源，但排卵峰更陡（真实 LH surge 是数量级突变，这里用加权近似）
        lh_t = clamp01(0.08 + 0.80 * s.gnrh) * (1.0 - 0.30 * e2_pull)
        if str(getattr(s, "menstrual_phase", "")) == "ovulation":
            lh_t *= 2.2
        s.lh = seat(s.lh, lh_t, dt, "lh")
    # 催乳素：睡眠与应激促进，多巴胺抑制（r_d2 在 neural 里算）
    prol_t = 0.15 + 0.45 * float(getattr(s, "sleep_depth", 0.0)) + 0.20 * (s.stress / 100.0)
    s.prolactin = seat(s.prolactin, prol_t, dt, "prolactin")

    # ---- 垂体后叶：催产素 ----
    # 正性情绪时升高；不给它接子宫收缩（孕期/分娩未建模）
    oxy_t = 0.05 + 0.45 * clamp01(float(getattr(s, "euphoria", 0.0)) / 100.0)
    s.oxytocin = seat(s.oxytocin, oxy_t, dt, "oxytocin")


def clamp01(x):
    """把量压回 0..1。局部小工具，不走 state.clamp 以避免每步多一次导入开销。"""
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def step_adh(s, dt):
    """抗利尿激素（v1.3.0 原实现，逐字保留）。

    容量抑制：血容量偏高时 ADH 下降（水负荷抑制），这是打破「高钠 -> 高 ADH ->
    少尿 -> 钠更积」正反馈的关键。酒精抑制 ADH 释放（独立于渗透压与容量的通路），
    即"喝酒利尿"。语义上它属于垂体后叶，故落在本模块。
    """
    dif = s.osmol - 290.0
    # v1.4.0：参考血容量由 5000 改 BV_REF_ML（4000）。三个公式都是"相对参考值的
    # 比值"，换常量后基线行为不变，但失血/贫血有了正确的分母。
    vol_suppress = clamp(BV_REF_ML / max(s.blood_vol, BV_REF_ML * 0.76), 0.55, 2.0)
    adh_t = clamp((1.0 + dif * 0.15) * vol_suppress, 0.05, 4.0)
    if s.bac > 0.0:
        adh_t = clamp(adh_t * (1.0 - 0.62 * clamp(s.bac / 1.0, 0.0, 1.0)),
                      0.03, 4.0)
    s.adh = approach(s.adh, adh_t, dt, "adh")
