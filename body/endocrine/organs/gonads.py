"""生殖腺：雌激素三组分、睾酮、雄烯二酮、雄甾二烯酮。

APK 依据：
  腺体  EstrogenGland / ProgesteroneGland / TesteroneGland /
        AndrostenoloneGland / AndrostadienoneGland
  激素  Estriol / Estrone / Estradiol / Progesterone / Progestin /
        Testosterone / Androstenolone / Androstadienone
  受体  ER / PR / LHR / FSHR / GnRHR（垂体，登记在 central）

**雌二醇/雌酮/雌三醇是从既有 estrogen 拆出来的组分，不是新的驱动源**：
engine._menstrual 已经把「雌激素总活性」算成一条有昼夜-周期结构的曲线，
再单独造三条会得到互相冲突的四个数。这里按生理比例分摊：
雌二醇占主导（0.75）、雌酮次之（0.20 + 外周雄烯二酮转化）、
雌三醇只在妊娠期有量（非孕期取 0.02 的痕迹值）。
"""
from ..base import hormone, receptor, register
from ...state import clamp

ORGAN = ("gonads", "生殖腺", "性激素组分")

# 这些组分是既有 estrogen 的即时分摊，不需要各自的时间常数（不引入滞后）。
E2_SHARE = 0.75     # 雌二醇：育龄女性最主要的活性雌激素
E1_SHARE = 0.20     # 雌酮：绝经后与外周转化的主要形式
E3_SHARE = 0.02     # 雌三醇：妊娠期胎盘产物，非孕期仅为痕迹

HORMONES = {
    "estrogen": hormone("Estradiol", "EstrogenGland", None, 0.00,
                        "雌激素总活性（engine._menstrual 推进）"),
    "progesterone": hormone("Progesterone", "ProgesteroneGland", None, 0.00,
                            "孕激素（engine._menstrual 推进）"),
    "estradiol": hormone("Estradiol", "EstrogenGland", None, 0.00,
                         "雌二醇 E2，雌激素的主要活性组分"),
    "estrone": hormone("Estrone", "EstrogenGland", None, 0.00,
                       "雌酮 E1，部分由雄烯二酮外周转化"),
    "estriol": hormone("Estriol", "EstrogenGland", None, 0.00,
                       "雌三醇 E3，妊娠期为主"),
    "testosterone": hormone("Testosterone", "TesteroneGland", None, 0.06,
                            "睾酮，女性由肾上腺与卵巢少量分泌"),
    "androstadienone": hormone("Androstadienone", "AndrostadienoneGland", None, 0.05,
                               "雄甾二烯酮，汗液中的信息素类固醇"),
}

RECEPTORS = {
    # 雌/孕激素的静息值是 0（周期关闭时为 0，卵泡期早期也接近 0），
    # 直接用 0 反解 Kd 会得到 0 —— 那就成了一个永远饱和的假受体。
    # 故这两条按「周期活跃值的一半」作参考值（0.50）：E2 到 0.5 时占据 0.28，
    # 到 1.0 时 0.61，与真实核受体的剂量-反应形状一致。
    "r_er": receptor("ER", "estradiol", 0.50, n=2.0, expr="子宫、乳腺、骨、脑",
                     note="雌激素受体是核受体二聚体；配体取雌二醇（活性最强组分）；"
                          "参考值取周期活跃值而非静息值，见下注"),
    "r_pr": receptor("PR", "progesterone", 0.50, n=2.0, expr="子宫内膜、乳腺",
                     note="孕激素受体，黄体期上调；参考值同上"),
    "r_lhr": receptor("LHR", "lh", 0.30, n=1.0, expr="卵巢卵泡膜、黄体",
                      note="LHR 介排卵与黄体化"),
    "r_fshr": receptor("FSHR", "fsh", 0.40, n=1.0, expr="卵巢颗粒细胞",
                       note="FSHR 介卵泡发育与雌二醇合成"),
}


def step(s, dt):
    """把既有 estrogen 分摊成三个组分，并补算睾酮与信息素类固醇。

    分摊是**即时**的（不经过一阶滞后）：estrogen 本身已经是一条平滑曲线，
    再套一层滞后只会让 E2 与 E1 的比值在周期转折处失真，没有生理收益。
    """
    est = clamp(float(s.estrogen), 0.0, 2.0)
    s.estradiol = est * E2_SHARE
    s.estrone = est * E1_SHARE + 0.25 * float(s.androstenedione)
    s.estriol = est * E3_SHARE

    lh_n = clamp(float(s.lh), 0.0, 2.5)
    s.testosterone = clamp(0.06 + 0.22 * lh_n + 0.10 * float(s.androstenedione),
                           0.02, 1.00)
    # 雄甾二烯酮由睾酮衍生（汗腺分泌），量级比睾酮低一个档
    s.androstadienone = clamp(0.05 + 0.35 * clamp(s.testosterone, 0.0, 1.0), 0.02, 0.60)
