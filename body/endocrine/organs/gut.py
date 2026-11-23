"""胃肠道腺体：胃泌素族、肠抑胃肽族与新增的神经降压素、组胺。

APK 依据：
  腺体  GastrinGland / SecretinGland / CholecystokininGland / GIPGLand /
        PYYGland / HistamineGland / NeurotensinGland / DuodenumGland /
        JejunumGland / HClGland / HCO3Gland
  激素  Gastrin / Secretin / Somatostatin / GIP / PYY / Cholecystokinin /
        Neurotensin / Histamine
  受体  CCK1 / CCK2 / Secretin Receptor / H2（胃壁细胞）/ NPY

**重要**：gastrin / secretin / cck / gip / pyy / glp1 / ghrelin 这 7 条
由 engine._digest 推进（历史实现，和胃排空、肠吸收同处计算才守恒）。
本模块只**登记元数据**，不重算它们 —— 在这里再写一遍就是两份公式，迟早走偏。
新增的只有 neurotensin 与 histamine。
"""
from ..base import hormone, receptor, register, seat
from ...state import clamp

ORGAN = ("gut", "胃肠腺体", "胃泌素族与肠激素")

register("neurotensin", 900.0)
register("histamine", 600.0)

HORMONES = {
    "gastrin": hormone("Gastrin", "GastrinGland", None, 0.15,
                       "胃泌素（engine._digest 推进）"),
    "secretin": hormone("Secretin", "SecretinGland", None, 0.10,
                        "促胰液素（engine._digest 推进）"),
    "cck": hormone("Cholecystokinin", "CholecystokininGland", None, 0.10,
                   "缩胆囊素（engine._digest 推进）"),
    "gip": hormone("GIP", "GIPGLand", None, 0.10,
                   "抑胃肽（engine._digest 推进）"),
    "pyy": hormone("PYY", "PYYGland", None, 0.10,
                   "酪酪肽（engine._digest 推进）"),
    "glp1": hormone("GLP-1", "LIGland", None, 0.10,
                    "胰高血糖素样肽-1（engine._digest 推进；APK 无单独串）"),
    "ghrelin": hormone("Ghrelin", "Ghrelin(APK)", None, 0.30,
                       "胃饥饿素（engine._digest 推进）"),
    "neurotensin": hormone("Neurotensin", "NeurotensinGland", "neurotensin", 0.10,
                           "神经降压素，回肠脂肪刺激分泌，抑制胃排空"),
    "histamine": hormone("Histamine", "HistamineGland", "histamine", 0.20,
                         "组胺，ECL 细胞受胃泌素刺激释放，作用于壁细胞 H2"),
}

RECEPTORS = {
    "r_cck1": receptor("CCK1", "cck", 0.10, n=1.0, expr="胆囊、胰腺外分泌、迷走传入",
                       note="CCK1 介胆囊收缩与胰酶分泌"),
    "r_cck2": receptor("CCK2", "gastrin", 0.15, n=1.0, expr="胃壁细胞、ECL 细胞",
                       note="CCK2（原 CCK-B）亲和胃泌素，介胃酸分泌"),
    "r_secretin": receptor("Secretin Receptor", "secretin", 0.10, n=1.0,
                           expr="胰管、胆管",
                           note="促胰液素受体介碳酸氢盐分泌"),
    "r_glp1": receptor("GLP1R", "glp1", 0.10, n=1.0, expr="胰岛 β、胃、脑",
                       note="GLP-1 受体；APK 只给了 Secretin Receptor 族，GLP1R 按文献补"),
    "r_h2": receptor("H2", "histamine", 0.20, n=1.0, expr="胃壁细胞",
                     note="H2 介胃酸分泌（法莫替丁等抑酸药的靶点）"),
    "r_h1": receptor("H1", "histamine", 0.20, n=1.0, expr="血管内皮、感觉神经、气道",
                     note="H1 介痒、血管扩张与气道收缩"),
    "r_npy": receptor("NPY", "pyy", 0.10, n=1.0, impl=None, status="未驱动",
                      expr="下丘脑、血管",
                      note="配体 NPY 不在 APK 的 HormoneType 枚举里，无法驱动；"
                           "PYY 只作为近似配体登记，勿据其做判断"),
}


def step(s, dt):
    """补算两条胃肠激素。其余 7 条归 engine._digest。"""
    # 神经降压素：回肠脂肪与残余刺激。餐后峰值比 CCK 晚、持续更久。
    nt_t = clamp(0.05 + 1.10 * clamp(s.chyme_fat / 30.0 + s.ileum_residue / 50.0, 0.0, 1.0),
                 0.02, 1.30)
    s.neurotensin = seat(s.neurotensin, nt_t, dt, "neurotensin")

    # 组胺：ECL 细胞受胃泌素驱动。取 0.85 的增益而不是 1:1 ——
    # ECL 的组胺储备有上限，胃泌素再高也不会无限放大。
    s.histamine = seat(s.histamine, clamp(0.10 + 0.85 * clamp(s.gastrin / 1.20, 0.0, 1.0),
                                          0.02, 1.10), dt, "histamine")
