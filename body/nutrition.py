# -*- coding: utf-8 -*-
"""膳食营养结构：供能比、膳食纤维、四种微量营养素与膳食质量分（v1.4.0 新增）。

自造声明
--------
LIFE.apk 只给食物**种类**（FoodType 23 项），没有任何营养素结构。
本模块是**自造系统**，评分口径参照国际通行的膳食质量指数（HEI-2015 [R54]）
与我国的膳食指南/DRIs 推荐量，而不是自创一套阈值。

为什么用"评分"而不是"扣血"
--------------------------
用户明确要求习惯/状态要"根据状态判断并显示"，不要变成选择项。
因此营养侧只做两件事：
  1. 记账：今天吃了多少能量、三大供能比、纤维、钾钙铁维C、钠；
  2. 打分：与推荐量比，得出一个 0~100 的膳食质量分与若干缺乏项标记。
分数与标记会喂给 body/habits.py 判定"健康饮食/营养不良/超重/肥胖/垃圾食品成瘾"，
以及 blood.py 的铁吸收（膳食铁）与 immune.py 的免疫张力。

推荐量（成年女性，18~49 岁）
---------------------------
能量 参照 Mifflin-St Jeor 估算 + 活动系数（估算，非文献测量）
蛋白 1.0 g/kg，脂肪供能 20%~30%，碳水 50%~65%  —— 中国居民膳食指南 2022
纤维 25 g   —— 同上；与 Reynolds 2019 的 25~29 g/天一致 [R53]
钾 2000 mg、钙 800 mg、铁 18 mg、维C 100 mg —— 中国居民膳食营养素参考摄入量
钠 <2000 mg（食盐 5 g）—— 中国居民膳食指南 2022 / WHO 钠摄入指南
"""
from .state import clamp
from .dynamics import H2S
from . import nutrient_profile as NP

__all__ = ["NUTRITION_FIELDS", "RECOMMEND", "score_for", "record_intake",
           "step", "reset_day", "registry_audit"]

# ---------------------------------------------------------------------------
# 推荐量与学生态
# ---------------------------------------------------------------------------
RECOMMEND = dict(
    protein_g_per_kg=1.0,
    fat_share=(0.20, 0.30),
    carb_share=(0.50, 0.65),
    fiber_g=25.0,
    k_mg=2000.0,
    ca_mg=800.0,
    fe_mg=18.0,
    vc_mg=100.0,
    na_mg=2000.0,
)

NA_MG_PER_MMOL = 22.99
KCAL_PER_KG = 7700.0        # 体脂能量密度（估算，用于体重派生）
ACTIVITY_FACTOR = 1.35      # 轻体力活动（估算）

# 评分权重（合计 100）。权重不是随便定的：宏量供能比与能量是否充足
# 权重最大，因为它们决定"吃得够不够、结构对不对"；微量营养素只占小头，
# 因为本模型的微量数据是类别画像（B 级估算），不宜给过大权重。
W_ENERGY, W_PROTEIN, W_FAT, W_CARB = 20.0, 15.0, 15.0, 10.0
W_FIBER, W_K, W_CA, W_FE, W_VC = 15.0, 5.0, 10.0, 5.0, 5.0
PENALTY_NA_MAX = 10.0

NUTRITION_FIELDS = {
    "nut_kcal": dict(unit="kcal", note="今日摄入能量"),
    "nut_protein_g": dict(unit="g", note="今日蛋白"),
    "nut_fat_g": dict(unit="g", note="今日脂肪"),
    "nut_carb_g": dict(unit="g", note="今日碳水"),
    "nut_fiber_g": dict(unit="g", note="今日膳食纤维"),
    "nut_k_mg": dict(unit="mg", note="今日钾"),
    "nut_ca_mg": dict(unit="mg", note="今日钙"),
    "nut_fe_mg": dict(unit="mg", note="今日铁"),
    "nut_vc_mg": dict(unit="mg", note="今日维生素 C"),
    "nut_na_mg": dict(unit="mg", note="今日钠"),
    "nut_junk_kcal": dict(unit="kcal", note="今日来自垃圾食品的能量"),
    "nut_day": dict(unit="天", note="当日账本对应的日序"),
    "diet_score": dict(unit="0-100", note="膳食质量分 [R54]"),
    "diet_flags": dict(unit="", note="缺乏/超标项标记（逗号分隔）"),
    "protein_per_kg": dict(unit="g/kg", note="每公斤体重蛋白摄入"),
    "fat_share": dict(unit="0-1", note="脂肪供能比"),
    "carb_share": dict(unit="0-1", note="碳水供能比"),
    "protein_share": dict(unit="0-1", note="蛋白供能比"),
    "junk_share": dict(unit="0-1", note="垃圾食品供能占比"),
    "junk_streak": dict(unit="天", note="连续高垃圾食品天数"),
    "fe_intake_24h": dict(unit="mg/天", note="膳食铁 EMA（blood 读取）"),
    "energy_need_kcal": dict(unit="kcal", note="估算需求"),
    "energy_balance_kcal": dict(unit="kcal", note="累计能量盈亏"),
    "weight_kg": dict(unit="kg", note="派生体重"),
    "bmi": dict(unit="", note="派生 BMI（中国标准）"),
    "nut_meals_today": dict(unit="次", note="今日正餐次数"),
}


# ---------------------------------------------------------------------------
# 记账
# ---------------------------------------------------------------------------
def reset_day(s, day: float) -> None:
    """清空当日账本（跨零点时调用）。"""
    for name in ("nut_kcal", "nut_protein_g", "nut_fat_g", "nut_carb_g",
                 "nut_fiber_g", "nut_k_mg", "nut_ca_mg", "nut_fe_mg",
                 "nut_vc_mg", "nut_na_mg", "nut_junk_kcal"):
        setattr(s, name, 0.0)
    s.nut_meals_today = 0
    s.nut_day = float(day)


def record_intake(s, totals: dict, slot: str = "", junk_kcal: float = 0.0) -> None:
    """记一次进食（由 engine._load_stomach 调用）。

    totals 是 food.combo_totals 的结果，已含纤维与四种微量营养素。
    这里只做累加，不做任何生理推进 —— 生理侧仍由 _digest 负责。
    """
    s.nut_kcal = float(getattr(s, "nut_kcal", 0.0) or 0.0) + float(totals.get("kcal") or 0.0)
    s.nut_protein_g = float(getattr(s, "nut_protein_g", 0.0) or 0.0) + float(totals.get("protein_g") or 0.0)
    s.nut_fat_g = float(getattr(s, "nut_fat_g", 0.0) or 0.0) + float(totals.get("fat_g") or 0.0)
    s.nut_carb_g = float(getattr(s, "nut_carb_g", 0.0) or 0.0) + float(totals.get("carb_g") or 0.0)
    s.nut_fiber_g = float(getattr(s, "nut_fiber_g", 0.0) or 0.0) + float(totals.get("fiber_g") or 0.0)
    s.nut_k_mg = float(getattr(s, "nut_k_mg", 0.0) or 0.0) + float(totals.get("k_mg") or 0.0)
    s.nut_ca_mg = float(getattr(s, "nut_ca_mg", 0.0) or 0.0) + float(totals.get("ca_mg") or 0.0)
    s.nut_fe_mg = float(getattr(s, "nut_fe_mg", 0.0) or 0.0) + float(totals.get("fe_mg") or 0.0)
    s.nut_vc_mg = float(getattr(s, "nut_vc_mg", 0.0) or 0.0) + float(totals.get("vc_mg") or 0.0)
    s.nut_na_mg = float(getattr(s, "nut_na_mg", 0.0) or 0.0) \
        + float(totals.get("na_mmol") or 0.0) * NA_MG_PER_MMOL
    s.nut_junk_kcal = float(getattr(s, "nut_junk_kcal", 0.0) or 0.0) + float(junk_kcal or 0.0)
    # 饮水进"今日饮水"计数器（习惯判定用；与 chyme_water 分账：
    # 前者是"喝了多少"，后者是"胃里还剩多少可吸收水"）
    s.water_ml_today = float(getattr(s, "water_ml_today", 0.0) or 0.0) + float(totals.get("water_ml") or 0.0)
    # 能量盈亏按**增量**记：吃进来的这一笔加到累计账，消耗由 step() 按时间扣。
    # 旧写法拿"当日累计"当速率再乘 dt/天，等于把一天的总量又除了一次 24，
    # 实测两天凭空少 900 kcal/天（体重一路掉）。
    s.energy_balance_kcal = clamp(float(getattr(s, "energy_balance_kcal", 0.0) or 0.0)
                                  + float(totals.get("kcal") or 0.0), -60000.0, 60000.0)
    if slot in ("breakfast", "lunch", "dinner", "meal"):
        s.nut_meals_today = int(getattr(s, "nut_meals_today", 0) or 0) + 1


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------
def _band_score(value: float, lo: float, hi: float, span: float, full: float) -> float:
    """落在 [lo, hi] 得满分；偏离时按 span 线性衰减到 0。"""
    if lo <= value <= hi:
        return full
    if value < lo:
        return full * clamp(1.0 - (lo - value) / max(span, 1e-6), 0.0, 1.0)
    return full * clamp(1.0 - (value - hi) / max(span, 1e-6), 0.0, 1.0)


def _ratio_score(value: float, target: float, full: float) -> float:
    """达到 target 得满分，不足按比例给分，超过 3 倍不再加分（也不过罚）。"""
    if target <= 0.0:
        return full
    return full * clamp(value / target, 0.0, 1.0)


def score_for(s) -> dict:
    """由当日账本算膳食质量分与标记（纯函数，不改 state）。"""
    kcal = max(0.0, float(getattr(s, "nut_kcal", 0.0) or 0.0))
    prot = max(0.0, float(getattr(s, "nut_protein_g", 0.0) or 0.0))
    fat = max(0.0, float(getattr(s, "nut_fat_g", 0.0) or 0.0))
    carb = max(0.0, float(getattr(s, "nut_carb_g", 0.0) or 0.0))
    fiber = max(0.0, float(getattr(s, "nut_fiber_g", 0.0) or 0.0))
    k = max(0.0, float(getattr(s, "nut_k_mg", 0.0) or 0.0))
    ca = max(0.0, float(getattr(s, "nut_ca_mg", 0.0) or 0.0))
    fe = max(0.0, float(getattr(s, "nut_fe_mg", 0.0) or 0.0))
    vc = max(0.0, float(getattr(s, "nut_vc_mg", 0.0) or 0.0))
    na = max(0.0, float(getattr(s, "nut_na_mg", 0.0) or 0.0))
    need = max(800.0, float(getattr(s, "energy_need_kcal", 1800.0) or 1800.0))
    weight = clamp(float(getattr(s, "weight_kg", 55.0) or 55.0), 30.0, 150.0)

    # 供能比：以实际宏量克的 4/9/4 折算，不用 kcal 字段（kcal 里还含酒精）
    p_kcal, f_kcal, c_kcal = 4.0 * prot, 9.0 * fat, 4.0 * carb
    macro = p_kcal + f_kcal + c_kcal
    f_share = f_kcal / macro if macro > 0 else 0.0
    c_share = c_kcal / macro if macro > 0 else 0.0
    p_share = p_kcal / macro if macro > 0 else 0.0

    sc = 0.0
    flags = []
    # 1) 能量是否够（需求 85%~115% 满分）
    e_ratio = kcal / need if need > 0 else 0.0
    sc += _band_score(e_ratio, 0.85, 1.15, 0.55, W_ENERGY)
    if e_ratio < 0.75:
        flags.append("能量不足")
    elif e_ratio > 1.30:
        flags.append("能量过剩")
    # 2) 蛋白（按体重）
    protein_per_kg = prot / weight
    sc += _ratio_score(protein_per_kg, RECOMMEND["protein_g_per_kg"], W_PROTEIN)
    if protein_per_kg < 0.8:
        flags.append("蛋白不足")
    # 3) 脂肪供能比
    lo, hi = RECOMMEND["fat_share"]
    sc += _band_score(f_share, lo, hi, 0.18, W_FAT)
    if f_share > hi + 0.05:
        flags.append("脂肪超标")
    # 4) 碳水供能比
    lo, hi = RECOMMEND["carb_share"]
    sc += _band_score(c_share, lo, hi, 0.22, W_CARB)
    if c_share < lo - 0.05:
        flags.append("碳水偏低")
    # 5) 纤维
    sc += _ratio_score(fiber, RECOMMEND["fiber_g"], W_FIBER)
    if fiber < 15.0:
        flags.append("纤维不足")
    # 6) 微量
    sc += _ratio_score(k, RECOMMEND["k_mg"], W_K)
    if k < RECOMMEND["k_mg"] * 0.6:
        flags.append("钾不足")
    sc += _ratio_score(ca, RECOMMEND["ca_mg"], W_CA)
    if ca < RECOMMEND["ca_mg"] * 0.6:
        flags.append("钙不足")
    sc += _ratio_score(fe, RECOMMEND["fe_mg"], W_FE)
    if fe < RECOMMEND["fe_mg"] * 0.6:
        flags.append("铁不足")
    sc += _ratio_score(vc, RECOMMEND["vc_mg"], W_VC)
    if vc < RECOMMEND["vc_mg"] * 0.6:
        flags.append("维C不足")
    # 7) 钠：超过 2000 mg 起罚，4000 mg 罚满
    if na > RECOMMEND["na_mg"]:
        sc -= PENALTY_NA_MAX * clamp((na - RECOMMEND["na_mg"]) / RECOMMEND["na_mg"], 0.0, 1.0)
        flags.append("钠超标")
    return {
        "score": clamp(sc, 0.0, 100.0),
        "flags": ",".join(flags),
        "fat_share": f_share, "carb_share": c_share, "protein_share": p_share,
        "protein_per_kg": protein_per_kg,
        "junk_share": (float(getattr(s, "nut_junk_kcal", 0.0) or 0.0) / kcal) if kcal > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------
def step(s, dt, height_cm: float = 162.0):
    """推进一整个营养步：跨日结算、能量盈亏与体重、评分、铁 EMA。"""
    day = s.t / 86400.0
    cur = float(getattr(s, "nut_day", -1.0) or -1.0)
    if cur < 0.0:
        s.nut_day = day
    elif int(day) != int(cur):
        reset_day(s, day)

    # ---- 能量需求：Mifflin-St Jeor × 活动系数 + 运动消耗 ----
    weight = clamp(float(getattr(s, "weight_kg", 55.0) or 55.0), 30.0, 150.0)
    h = clamp(float(height_cm or 162.0), 130.0, 210.0)
    bmr = 10.0 * weight + 6.25 * h - 5.0 * 18.0 - 161.0
    need = max(900.0, bmr * ACTIVITY_FACTOR)
    if bool(getattr(s, "exercising", False)):
        # 中等强度约 6 MET、高强度约 10 MET（1 MET ≈ 1 kcal/kg/h）
        met = 10.0 if str(getattr(s, "exercise_intent", "") or "mid") == "high" else 6.0
        need += met * weight * (dt / H2S)
    s.energy_need_kcal = need

    # ---- 能量盈亏 -> 派生体重 ----
    intake_rate = float(getattr(s, "nut_kcal", 0.0) or 0.0)
    # 摄入已经在 record_intake 里按增量记账，这里只扣"这段时间消耗了多少"。
    s.energy_balance_kcal = clamp(float(getattr(s, "energy_balance_kcal", 0.0) or 0.0)
                                  - need * (dt / (24.0 * H2S)), -60000.0, 60000.0)
    s.weight_kg = clamp(55.0 + s.energy_balance_kcal / KCAL_PER_KG, 35.0, 140.0)
    s.bmi = s.weight_kg / ((h / 100.0) ** 2)

    # ---- 评分（当日摄入过少时沿用上一分，避免凌晨归零造成抖动）----
    if intake_rate >= 300.0:
        res = score_for(s)
        prev = float(getattr(s, "diet_score", 50.0) or 50.0)
        # 一阶平滑（τ=6 h）：一天的极端饮食不该把分数立刻顶满/砸穿
        s.diet_score = clamp(prev + (res["score"] - prev) * min(1.0, dt / (6.0 * H2S)),
                             0.0, 100.0)
        s.diet_flags = res["flags"]
        s.fat_share = res["fat_share"]
        s.carb_share = res["carb_share"]
        s.protein_share = res["protein_share"]
        s.protein_per_kg = res["protein_per_kg"]
        s.junk_share = res["junk_share"]

    # ---- 垃圾食品连续天数（成瘾判定用）----
    streak = float(getattr(s, "junk_streak", 0.0) or 0.0)
    if float(getattr(s, "junk_share", 0.0) or 0.0) >= 0.30:
        s.junk_streak = streak + dt / (24.0 * H2S)
    else:
        s.junk_streak = max(0.0, streak - dt / (24.0 * H2S))

    # ---- 膳食铁 EMA（blood.step_iron 读取）----
    fe_today = float(getattr(s, "nut_fe_mg", 0.0) or 0.0)
    prev_fe = float(getattr(s, "fe_intake_24h", 0.0) or 0.0)
    s.fe_intake_24h = prev_fe + (fe_today - prev_fe) * min(1.0, dt / (24.0 * H2S))


def registry_audit():
    """自检：注册表里的字段是否都在 BodyState 上。"""
    import dataclasses
    from .state import BodyState
    have = {f.name for f in dataclasses.fields(BodyState)}
    return ["NUTRITION_FIELDS." + n + " 不在 BodyState 字段里"
            for n in NUTRITION_FIELDS if n not in have]
