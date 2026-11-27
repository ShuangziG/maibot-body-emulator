# -*- coding: utf-8 -*-
"""生活习惯判定：从状态反推"现在符合哪些生活状态"（v1.4.0 新增）。

这是本版三条需求里唯一**纯判定、零副作用**的模块：它只读 state
（外加自己维护的几个计数器），绝不写任何生理量。因此它可以在任何
时刻被反复调用，也不会改变 30 天回归曲线的任何一个数。

判定来源：用户提供的《生活习惯.xlsx》16 项 —— 9 项正向、7 项负向。
每项的生理学定义与阈值都写在下表的 rule 里，阈值有文献的标 [Rxx]，
没有文献的标「估算」。评分口径与 nutrition/blood/immune 的字段对应。

诚实标注（三条代理指标）
------------------------
本模型没有"打坐""工作"这两个动作，但 Excel 里既有「冥想」也有「工作狂」。
与其假装有，不如把代理口径写明：
  * 冥想 / 松弛 —— 由**自主神经状态**反推：清醒、压力低、心率低于
    日内基线并持续一段时间，即"处于放松状态"。没有独立的冥想动作。
    放松类干预确实能降低焦虑与压力评分（冥想 [R57]），瑜伽在多项指标上
    优于或等同于普通运动（[R58]），这也是为什么它们被列为正向习惯。
  * 工作狂 —— 由**深夜清醒 + 高压力**反推：一周里 ≥3 天在 23:00 后
    仍清醒，且平均压力 ≥ 60。没有工作内容建模。
  * 久坐 —— 由**本周运动量**反推：一周运动 < 60 分钟为久坐，
    没有计步器与坐姿时长。
这三条在 WebUI 里带「代理」角标，文档里也照此说明，不冒充直接观测。
"""
from .state import clamp
from .dynamics import H2S

__all__ = ["HABIT_FIELDS", "HABITS", "step", "judge", "active_list",
           "registry_audit"]

# ---------------------------------------------------------------------------
# 判定表
# ---------------------------------------------------------------------------
# 每项：(id, 中文名, 极性, 说明文本（来自 xlsx 的口径）, 代理标记)
HABITS = [
    ("relax_therapy", "松弛", +1,
     "放松以达到更平静的状态，降低疼痛、焦虑、压力或愤怒", "proxy"),
    ("yoga", "瑜伽", +1,
     "强化与拉伸并重、结合深呼吸的身体练习", ""),
    ("meditation", "冥想", +1,
     "专注呼吸或单一对象以训练注意力的练习", "proxy"),
    ("regular_sleep", "规律睡眠", +1,
     "稳定的入睡与起床模式，是优质夜间睡眠的前提 [R59]", ""),
    ("adequate_water", "适量饮水", +1,
     "水分调节体温、润滑关节、输送营养 [R60]", ""),
    ("healthy_diet", "健康饮食", +1,
     "供能比合理、纤维与微量营养素充足 [R53][R54]", ""),
    ("aerobic", "有氧运动", +1,
     "每周中等及以上强度有氧活动 ≥150 分钟（WHO 2020 活动指南量级）", ""),
    ("strength", "力量训练", +1,
     "每周 ≥2 次抗阻训练，增加肌力与骨密度", ""),
    ("fitness_lover", "健身爱好者", +1,
     "每周训练数小时并追求健身目标", ""),
    ("malnutrition", "营养不良", -1,
     "营养素缺乏、过剩或比例不当 [R41][R53]", ""),
    ("dehydration", "脱水", -1,
     "失水多于摄入，身体无法维持正常功能 [R60]", ""),
    ("overweight", "超重", -1,
     "体脂超出健康水平（中国标准 BMI 24~27.9，WS/T 428-2013）", ""),
    ("obese", "肥胖", -1,
     "体脂积聚到危害健康的程度（中国标准 BMI ≥28）", ""),
    ("sedentary", "久坐", -1,
     "缺少或不规律体育活动的生活方式 [R56]", "proxy"),
    ("junk_food", "垃圾食品成瘾", -1,
     "高油高盐高糖食物占能量比重长期偏高 [R61]", ""),
    ("workaholic", "工作狂", -1,
     "沉迷工作（本模型以深夜清醒+高压力作代理）", "proxy"),
]

HABIT_FIELDS = {
    "habits_active": dict(unit="", note="当前命中的习惯 id（逗号分隔）"),
    "habits_count": dict(unit="项", note="当前命中项数"),
    "relax_minutes_today": dict(unit="min", note="今日放松状态累计分钟"),
    "relax_segment_max_min": dict(unit="min", note="今日最长一段放松（分钟）"),
    "awake_minutes_today": dict(unit="min", note="今日清醒累计分钟"),
    "water_ml_today": dict(unit="mL", note="今日饮水累计"),
    "ex_minutes_today": dict(unit="min", note="今日运动累计分钟"),
    "ex_week_cardio_min": dict(unit="min", note="本周有氧分钟（7 日滑窗）"),
    "ex_week_strength_min": dict(unit="min", note="本周力量分钟"),
    "ex_week_yoga_min": dict(unit="min", note="本周瑜伽/拉伸分钟"),
    "ex_week_strength_sessions": dict(unit="次", note="本周力量训练次数"),
    "late_night_days": dict(unit="天", note="本周 23:00 后仍清醒的天数"),
    "late_night_mark": dict(unit="天", note="上一次记晚睡的日序"),
    "sleep_onsets": dict(unit="小时", note="最近 14 次入睡时刻（小时）"),
    "habit_day": dict(unit="天", note="计数器所属日序"),
}

# 放松判据（自主神经反推）：压力 < 35、心率 < 日内基线 +5、清醒、未运动
RELAX_STRESS_MAX = 35.0
RELAX_HR_MAX = 78.0

# 阈值
WATER_TARGET_ML = 1500.0     # [R60] 女性总水摄入 2.0 L/天，其中饮水约 1.5 L
AEROBIC_WEEK_MIN = 150.0     # WHO 2020
STRENGTH_WEEK_SESSIONS = 2
STRENGTH_WEEK_MIN = 60.0
YOGA_WEEK_MIN = 60.0
FITNESS_WEEK_MIN = 240.0
REGULAR_SLEEP_SD_MIN = 45.0  # 入睡时刻标准差 ≤45 分钟（估算，参照 [R59] 的规律性口径）
REGULAR_SLEEP_MEAN_H = 6.5
# 脱水阈值取 4%（相对自身 7 日基线）。轻度失水即影响体能与认知 [R64]，
# 但"脱水"作为状态标签应留给更明确的失水程度，故不与"缺水"混用。
# 为什么不取 3%：本模型稳态总体水在 40~41 L（见 30 天回归），
# 3% 相当于 40.7 L，正常日子也会踩线，判定变成"天天脱水"。5%（39.9 L）
# 只在运动/高温/饮水不足的日子里触发，与 [R60] 的口径也不冲突 ——
# 那种 2%~3% 的轻度失水在临床上叫"缺水"而非"脱水"。
DEHYDRATE_FRAC = 0.04
JUNK_SHARE = 0.30
JUNK_STREAK_DAY = 3.0
WORK_LATE_DAYS = 3
WORK_STRESS = 60.0
SEDENTARY_WEEK_MIN = 60.0
SEDENTARY_TODAY_MIN = 10.0


def _f(s, name, default=0.0):
    """安全读取浮点字段。"""
    try:
        return float(getattr(s, name, default) or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 计数器（只写本模块自己的字段）
# ---------------------------------------------------------------------------
def step(s, dt, baseline_hr: float = 70.0, relax_stress_max: float = None,
         aerobic_week_min: float = None, strength_week_sessions: float = None):
    """维护习惯判定所需的计数器。不触碰任何生理量。"""
    day = s.t / 86400.0
    cur = _f(s, "habit_day", -1.0)
    if cur < 0.0 or int(day) != int(cur):
        s.habit_day = day
        s.relax_minutes_today = 0.0
        s.relax_segment_max_min = 0.0
        s.awake_minutes_today = 0.0
        s.water_ml_today = 0.0
        s.ex_minutes_today = 0.0

    relax_lim = RELAX_STRESS_MAX if relax_stress_max is None else float(relax_stress_max)
    minutes = dt / 60.0
    sleeping = bool(getattr(s, "sleeping", False))
    if not sleeping:
        s.awake_minutes_today = _f(s, "awake_minutes_today") + minutes
        stress = _f(s, "stress", 20.0)
        hr = _f(s, "hr", 70.0)
        exercising = bool(getattr(s, "exercising", False))
        if (not exercising) and stress < relax_lim and hr < max(RELAX_HR_MAX, baseline_hr + 6.0):
            s.relax_minutes_today = _f(s, "relax_minutes_today") + minutes
            # 连续段：用 relax_seg_cur 记录当前段，跨到新段时归零
            seg = _f(s, "relax_seg_cur") + minutes
            s.relax_seg_cur = seg
            if seg > _f(s, "relax_segment_max_min"):
                s.relax_segment_max_min = seg
        else:
            s.relax_seg_cur = 0.0
    else:
        s.relax_seg_cur = 0.0

    # ---- 深夜清醒天数 ----
    hour = _f(s, "hour", 12.0)
    if (not sleeping) and (hour >= 23.0 or hour < 2.0):
        mark = int(_f(s, "late_night_mark", -1.0))
        if mark != int(day):
            s.late_night_mark = day
            s.late_night_days = min(7.0, _f(s, "late_night_days") + 1.0)

    # ---- 周滑窗（7 天线性衰减，等价于"最近一周累计"）----
    decay = clamp(1.0 - dt / (7.0 * 24.0 * H2S), 0.0, 1.0)
    for name in ("ex_week_cardio_min", "ex_week_strength_min",
                 "ex_week_yoga_min", "ex_week_strength_sessions",
                 "late_night_days"):
        setattr(s, name, _f(s, name) * decay)

    # ---- 水合基线（7 日 EMA）与当前命中项 ----
    water = _f(s, "body_water", 42.0)
    ref = _f(s, "body_water_ref", 0.0)
    if ref <= 0.0:
        s.body_water_ref = water
    else:
        s.body_water_ref = ref + (water - ref) * min(1.0, dt / (7.0 * 24.0 * H2S))
    hit = judge(s, relax_stress_max=relax_lim,
                aerobic_week_min=aerobic_week_min,
                strength_week_sessions=strength_week_sessions)
    s.habits_active = ",".join(h["id"] for h in hit)
    s.habits_count = len(hit)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def _sleep_regular(s):
    """近 7 次入睡时刻的规律性。返回 (sd_minutes, mean_hours, n)。"""
    raw = getattr(s, "sleep_onsets", None) or []
    vals = []
    for v in list(raw)[-14:]:
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < 3:
        return None, None, len(vals)
    # 入睡时刻是环形量：以第一个为基准展开，避免 23:50 与 00:10 被算成 23.7 小时差
    base = vals[0]
    unwrapped = []
    for v in vals:
        d = (v - base + 12.0) % 24.0 - 12.0
        unwrapped.append(base + d)
    mean = sum(unwrapped) / len(unwrapped)
    var = sum((x - mean) ** 2 for x in unwrapped) / len(unwrapped)
    sd = var ** 0.5 * 60.0
    # 2026-11-26 修：旧式是 hours / max(1, min(7, nights))。nights 以前数的是
    # **bout 段数**，一夜中断成两段就会数成 2，7 天能报出 11 夜；分母被
    # min(7, ...) 压死在 7，而分子含了 11 段的小时数，于是"每晚睡了多久"
    # 被系统性放大。现在 nights 是真正的夜数（同一夜多段 bout 已合账），
    # 分子分母口径一致，直接相除即可。
    mean_h = _f(s, "sleep_week_hours") / max(1.0, float(getattr(s, "sleep_week_nights", 0) or 1))
    return sd, mean_h, len(vals)


def judge(s, relax_stress_max: float = None, aerobic_week_min: float = None,
          strength_week_sessions: float = None) -> list:
    """返回当前命中的习惯列表。纯读，不写 state。"""
    aero_lim = AEROBIC_WEEK_MIN if aerobic_week_min is None else float(aerobic_week_min)
    str_lim = STRENGTH_WEEK_SESSIONS if strength_week_sessions is None else float(strength_week_sessions)
    out = []
    by_id = {h[0]: h for h in HABITS}
    diet = _f(s, "diet_score", 50.0)
    bmi = _f(s, "bmi", 21.0)
    # 脱水用"相对自身 7 日基线"的口径（见 body_water_ref 的说明）
    _ref_w = _f(s, "body_water_ref", 0.0) or _f(s, "body_water", 42.0)
    water_gap = (_ref_w - _f(s, "body_water", 42.0)) / max(_ref_w, 1e-6)
    na = _f(s, "na", 140.0)
    wk_cardio = _f(s, "ex_week_cardio_min")
    wk_strength_min = _f(s, "ex_week_strength_min")
    wk_strength_n = _f(s, "ex_week_strength_sessions")
    wk_yoga = _f(s, "ex_week_yoga_min")
    wk_total = wk_cardio + wk_strength_min + wk_yoga
    relax_today = _f(s, "relax_minutes_today")
    relax_seg = _f(s, "relax_segment_max_min")
    water_today = _f(s, "water_ml_today")
    junk_share = _f(s, "junk_share")
    junk_streak = _f(s, "junk_streak")
    stress = _f(s, "stress", 20.0)
    late_days = _f(s, "late_night_days")
    anemia = str(getattr(s, "anemia", "none") or "none")
    hb = _f(s, "hb", 136.0)

    def add(hid, ratio, evidence):
        hid, label, pol, desc, proxy = by_id[hid]
        out.append({"id": hid, "label": label, "polarity": pol,
                    "desc": desc, "proxy": proxy,
                    "strength": clamp(ratio, 0.0, 1.0),
                    "evidence": evidence})

    # ---------------- 正向 ----------------
    if relax_today >= 20.0:
        add("relax_therapy", relax_today / 40.0,
            "今日放松状态累计 %.0f 分钟" % relax_today)
    if wk_yoga >= YOGA_WEEK_MIN:
        add("yoga", wk_yoga / 120.0, "本周瑜伽/拉伸 %.0f 分钟" % wk_yoga)
    if relax_seg >= 10.0:
        add("meditation", relax_seg / 25.0,
            "最长一段连续放松 %.0f 分钟" % relax_seg)
    sd, mean_h, n = _sleep_regular(s)
    if sd is not None and sd <= REGULAR_SLEEP_SD_MIN and mean_h and mean_h >= REGULAR_SLEEP_MEAN_H:
        add("regular_sleep", 1.0 - sd / (REGULAR_SLEEP_SD_MIN * 2.0),
            "入睡时刻标准差 %.0f 分钟，日均 %.1f 小时" % (sd, mean_h))
    if water_today >= WATER_TARGET_ML and water_gap <= DEHYDRATE_FRAC and na <= 146.0:
        add("adequate_water", water_today / (WATER_TARGET_ML * 1.6),
            "今日饮水 %.0f mL，血钠 %.0f" % (water_today, na))
    if diet >= 70.0:
        add("healthy_diet", (diet - 70.0) / 30.0, "膳食质量分 %.0f" % diet)
    if wk_cardio >= aero_lim:
        add("aerobic", wk_cardio / (aero_lim * 2.0),
            "本周有氧 %.0f 分钟" % wk_cardio)
    if wk_strength_n >= str_lim and wk_strength_min >= STRENGTH_WEEK_MIN:
        add("strength", wk_strength_n / (str_lim * 2.0),
            "本周力量 %.0f 分钟 / %d 次" % (wk_strength_min, int(wk_strength_n)))
    if wk_total >= FITNESS_WEEK_MIN and (wk_cardio >= 60.0 or wk_strength_min >= 60.0):
        add("fitness_lover", wk_total / (FITNESS_WEEK_MIN * 2.0),
            "本周训练合计 %.0f 分钟" % wk_total)

    # ---------------- 负向 ----------------
    if diet < 45.0 or anemia in ("moderate", "severe") or hb < 110.0:
        add("malnutrition", (45.0 - min(diet, 45.0)) / 45.0 if diet < 45.0 else 0.7,
            "膳食质量分 %.0f，血红蛋白 %.0f g/L" % (diet, hb))
    if water_gap > DEHYDRATE_FRAC or na > 148.0 or _f(s, "osmol", 290.0) > 300.0:
        add("dehydration", clamp(water_gap / 0.08, 0.2, 1.0),
            "总体水缺 %.1f%%，血钠 %.0f" % (water_gap * 100.0, na))
    if 24.0 <= bmi < 28.0:
        add("overweight", (bmi - 24.0) / 4.0, "BMI %.1f" % bmi)
    if bmi >= 28.0:
        add("obese", clamp((bmi - 28.0) / 8.0, 0.2, 1.0), "BMI %.1f" % bmi)
    if wk_total < SEDENTARY_WEEK_MIN and _f(s, "ex_minutes_today") < SEDENTARY_TODAY_MIN:
        add("sedentary", 1.0 - wk_total / SEDENTARY_WEEK_MIN,
            "本周运动 %.0f 分钟" % wk_total)
    if junk_streak >= JUNK_STREAK_DAY and junk_share >= JUNK_SHARE:
        add("junk_food", clamp(junk_streak / 7.0, 0.3, 1.0),
            "垃圾食品供能占 %.0f%%，连续 %d 天" % (junk_share * 100.0, int(junk_streak)))
    if late_days >= WORK_LATE_DAYS and stress >= WORK_STRESS:
        add("workaholic", clamp(late_days / 6.0, 0.4, 1.0),
            "本周 %d 天深夜仍清醒，压力 %.0f" % (int(late_days), stress))
    return out


def active_list(s) -> list:
    """当前命中习惯的 id 列表（供状态接口/提示注入用）。"""
    raw = str(getattr(s, "habits_active", "") or "")
    return [x for x in raw.split(",") if x]


def registry_audit():
    """自检：注册表里的字段是否都在 BodyState 上。"""
    import dataclasses
    from .state import BodyState
    have = {f.name for f in dataclasses.fields(BodyState)}
    return ["HABIT_FIELDS." + n + " 不在 BodyState 字段里" for n in HABIT_FIELDS if n not in have]
