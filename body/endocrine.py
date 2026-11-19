"""内分泌系统：激素轴与受体占据（2026-11-18 从 engine._endocrine 拆出）。

为什么拆：原先 _endocrine() 一个方法里塞了 6 套子系统（RAAS / ADH / HPA /
肾上腺素 / 甲状腺 / 糖皮质激素受体），约 190 行连排。加受体时无从下手，
且无法单独为某条轴写测例。拆开后每套轴是一个纯函数，接收 state 原地修改，
彼此只通过 state 通信 —— 与 engine 其余子系统（_cardio/_renal/…）同构。

纪律：本文件只做「按已有公式推进状态」，不引入新的生理常数而不标注依据。
转运顺序必须与拆分前完全一致，否则数值会漂（已验证逐位一致）。
"""
from .state import clamp
from .dynamics import TAU, _tau, approach, approach_tau, hill_occupancy

__all__ = ["HORMONES", "RECEPTORS", "GCR_KD", "GCR_N",
           "step_raas", "step_adh", "step_hpa", "step_adrenaline",
           "step_thyroid", "step_gcr_hormone",
           "step_autonomic_receptors", "registry_audit", "step"]

# ---------------------------------------------------------------------------
# 激素/受体注册表
# ---------------------------------------------------------------------------
# 用途：把「这条激素是什么、哪来的、时间常数多少、APK 里叫什么」集中成数据，
# 而不是散落在注释里。计算仍走各自的 step_* 函数 —— 注册表只是**可维护性**
# 设施，不参与数值。将来接 APK 的 HormoneLibrary 时，这张表就是落点。
#
# tau：对应 dynamics.TAU 里的 key（数字表示直接给秒数）
# apk：LIFE.apk global-metadata.dat 里能对上的名字（交叉验证见
#      _emotion_probe/APK交叉验证_情绪系统.md）
HORMONES = {
    "renin":       dict(apk="Renin", gland="ReninGland", tau="raas",
                        note="RAAS 起始酶"),
    "ang2":        dict(apk="Angiotensin II", gland="AngiotensinogenGland",
                        tau="ang2", note="血管紧张素 II"),
    "aldosterone": dict(apk="Aldosterone", gland="AlAldosteroneGland",
                        tau="aldo", note="保钠排钾"),
    "adh":         dict(apk="Vasopressin", gland="VasopressinGland",
                        tau="adh", note="抗利尿，酒精抑制"),
    "cortisol":    dict(apk="Cortisol", gland="CortisolGland/AdrenalGland",
                        tau="cortisol", note="HPA 终产物"),
    "acth":        dict(apk="ACTH", gland="ACTHGland",
                        tau="acth", note="垂体促肾上腺皮质激素"),
    "adrenaline":  dict(apk="Epinephrine", gland="EpinephrineGland",
                        tau=30.0, note="SAM 轴，τ 直接给秒"),
    "tsh":         dict(apk="TSH", gland="TSHGland",
                        tau="thyroid", note="促甲状腺激素"),
    "t3":          dict(apk="T3", gland="T3Gland/T4Gland",
                        tau="thyroid", note="甲状腺激素"),
}

# 受体表：配体 -> 受体 -> Hill 参数
# 目前只有 GCR 走 Hill；alpha1/beta1/m2/insulin 仍是 engine._autonomic 里的
# 线性映射（历史实现），未迁入本表，避免一次改动太大。
GCR_KD = 0.8   # 半饱和常数（cortisol 相对量纲：静息 0.5、应激峰 1.5~3.0）
GCR_N = 2.0    # Hill 系数，2 对应受体二聚化

# impl：该受体的计算实际写在哪（单一索引，便于"这条受体到底在哪算"）
# status：impl 为 None 时的如实标注
RECEPTORS = {
    "r_gcr": dict(apk="Glucocorticoid_receptor", ligand="cortisol",
                  kd=GCR_KD, n=GCR_N, impl="endocrine.step_gcr_hormone",
                  note="Hill 方程 n=2；介导 HPA 负反馈与情绪效应"),
    "r_alpha1": dict(apk="Adrenoceptor(alpha1)", ligand="交感张力 + AngII",
                     impl="endocrine.step_autonomic_receptors",
                     note="血管收缩；线性映射（原实现如此，未改公式）"),
    "r_beta1": dict(apk="Adrenoceptor(beta1)", ligand="交感张力 + 肾上腺素",
                    impl="endocrine.step_autonomic_receptors",
                    note="心率提升；线性映射"),
    "r_m2": dict(apk="M Receptor", ligand="副交感张力 para",
                 impl="endocrine.step_autonomic_receptors",
                 note="迷走减速；线性映射"),
    "r_insulin": dict(apk="Insulin Receptor", ligand="insulin",
                      impl=None, status="未驱动",
                      note="engine 全程从不更新此字段，恒为初始值 0.30。"
                           "v1.2.5 起已从 WebUI「内脏细则」移除（常数当指标显示是误导），"
                           "仅保留在 state 中，勿据其做判断"),
}

# 与改动前一致的时间常数登记（拆分时原样搬运，勿改）
_tau("raas", 90.0)
_tau("ang2", 60.0)
_tau("aldo", 300.0)
_tau("adh", 300.0)
_tau("cortisol", 600.0)
_tau("acth", 300.0)
_tau("thyroid", 1800.0)


# ---------------------------------------------------------------------------
# 各轴（纯函数：入参 state，原地推进，无返回值）
# ---------------------------------------------------------------------------
def step_raas(s, dt):
    """肾素-血管紧张素-醛固酮系统。

    驱动：血容量不足、平均动脉压偏低。醛固酮的保钠动作不在这里 ——
    它必须与 _renal 的排钠同处计算才能守恒（钠只能从尿里省，不能凭空造）。
    """
    renin_t = clamp(1.0 + (1.0 - s.blood_vol / 5000.0) * 3.0
                   + (90.0 - s.map) / 40.0, 0.2, 4.0)
    s.renin = approach(s.renin, renin_t, dt, "raas")
    s.ang2 = approach(s.ang2, clamp(s.renin * 1.2, 0.2, 3.0), dt, "ang2")
    s.aldosterone = approach(s.aldosterone, clamp(s.ang2 * 0.8, 0.0, 3.0),
                             dt, "aldo")


def step_adh(s, dt, dif):
    """抗利尿激素（血管加压素）。

    dif 是血浆渗透压相对 290 的偏差，由调用方算一次后传进来。
    容量抑制：血容量偏高时 ADH 下降（水负荷抑制），这是打破
    「高钠 -> 高 ADH -> 少尿 -> 钠更积」正反馈的关键。
    酒精抑制 ADH 释放（独立于渗透压与容量的通路），即"喝酒利尿"。
    """
    vol_suppress = clamp(5000.0 / max(s.blood_vol, 3800.0), 0.55, 2.0)
    adh_t = clamp((1.0 + dif * 0.15) * vol_suppress, 0.05, 4.0)
    if s.bac > 0.0:
        adh_t = clamp(adh_t * (1.0 - 0.62 * clamp(s.bac / 1.0, 0.0, 1.0)),
                      0.03, 4.0)
    s.adh = approach(s.adh, adh_t, dt, "adh")


def step_hpa(s, dt):
    """下丘脑-垂体-肾上腺轴。

    目标值由昼夜相位、心理压力、高血糖三者合成；ACTH 跟随皮质醇 0.8 倍。
    皮质醇升糖这一条写在 _metabolic 之外是有历史原因的（原实现就在这里），
    迁移时原样保留、未做语义重排 —— 重排会改变同一 dt 内的计算次序。
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


def step_thyroid(s, dt):
    """甲状腺轴。tsh 恒定回 1.0；t3 随昼夜相位小幅摆动。"""
    s.tsh = approach(s.tsh, 1.0, dt, "thyroid")
    s.t3 = approach(s.t3, clamp(1.0 + 0.15 * s.circ, 0.7, 1.4), dt, "thyroid")


def step_gcr_hormone(s):
    """糖皮质激素受体占据（GCR）。

    依据 LIFE.apk 的 Glucocorticoid_receptor：皮质醇是配体，GCR 是靶点。
    用 Hill 方程而不是线性比例 —— 线性会让"皮质醇翻倍=效应翻倍"，
    与受体饱和的真实行为不符。

    注意：本字段目前**只计算并导出，不参与 stress_t**，因此默认配置下
    对既有标定零影响。要让它反馈情绪需单独评估（stress->cortisol->stress
    已有 0.384 正反馈，再叠一层非线性有发散风险）。
    """
    s.r_gcr = hill_occupancy(s.cortisol, GCR_KD, GCR_N)


def step_autonomic_receptors(s, symp, para):
    """自主神经受体占据（2026-11-18 从 engine._autonomic 迁入）。

    与 GCR 不同，这三条仍是**线性映射**而非 Hill 方程 —— 原实现就是线性，
    迁移只搬位置不改公式（改了就是行为变更，不在本次范围）。
    要让它们也走 Hill，须先确定各受体 Kd 与内源配体量纲，另案。

    symp / para 由调用方算好后传入，而**不是**从 s 上读：
    s.symp / s.para 是 engine 动态挂的属性，state.py 里没有声明，
    asdict() 不会序列化它们 —— 从存档恢复的身体上这两个属性并不存在。
    若在此 getattr(s, "symp") 就会在恢复后拿到默认值、悄悄改变行为。
    传参把这个耦合显式化。
    """
    s.r_beta1 = clamp(0.12 + 0.55 * symp + 0.10 * s.adrenaline, 0, 1)
    s.r_alpha1 = clamp(0.08 + 0.45 * symp + 0.07 * s.ang2, 0, 1)
    s.r_m2 = clamp(0.15 + 0.55 * para, 0, 1)


def registry_audit():
    """自检：注册表与 BodyState 是否脱节。

    只读，不改状态。返回错误列表（空 = 通过）。
    用途：防止"注册表登记了某激素，但 state 里根本没这个字段"这类
    静默失效 —— 加新激素时先跑一遍，比等到运行时报 AttributeError 好。
    """
    import dataclasses
    from .state import BodyState
    fields = {f.name for f in dataclasses.fields(BodyState)}
    errors = []
    for name, meta in HORMONES.items():
        if name not in fields:
            errors.append("HORMONES." + name + " 不在 BodyState 字段里")
        tau = meta.get("tau")
        if isinstance(tau, str) and tau not in TAU:
            errors.append("HORMONES." + name + " 的 tau='" + tau + "' 未在 TAU 登记")
    for name, meta in RECEPTORS.items():
        if name not in fields:
            errors.append("RECEPTORS." + name + " 不在 BodyState 字段里")
        if meta.get("impl") is None and not meta.get("status"):
            errors.append("RECEPTORS." + name + " 既无 impl 也未标注 status")
    return errors


def step(s, dt):
    """推进一整个内分泌步。调用顺序与拆分前严格一致，勿重排。"""
    dif = s.osmol - 290.0
    step_raas(s, dt)
    step_adh(s, dt, dif)
    step_hpa(s, dt)
    step_adrenaline(s, dt)
    step_thyroid(s, dt)
    step_gcr_hormone(s)
