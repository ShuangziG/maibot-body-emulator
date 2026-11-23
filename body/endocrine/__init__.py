"""内分泌系统：按器官分科，激素产生与受体占据。

为什么把单文件 endocrine.py 拆成包（v1.3.1）
==========================================
v1.3.0 的 endocrine.py 一个文件里塞了 6 套子系统、214 行。要加一条激素或
一个受体，得在同一个文件里来回翻；各轴的时间常数与 APK 名字散在注释里，
连「这条受体到底在哪算」都要靠 grep。v1.3.1 要补的激素是 38 条、受体 40 余个 ——
在单文件里做，等于把可维护性彻底放弃。故按「由哪个器官分泌 / 在哪个器官表达」分科：

    organs/central.py       下丘脑 · 垂体
    organs/thyroid.py       甲状腺
    organs/parathyroid.py   甲状旁腺 · 钙磷
    organs/adrenal.py       肾上腺（皮质 / 髓质）
    organs/pancreas.py      胰岛
    organs/gut.py           胃肠腺体
    organs/kidney.py        肾脏（RAAS / 活性维生素 D）
    organs/heart.py         心脏（ANP）
    organs/liver.py         肝脏（血管紧张素原）
    organs/gonads.py        生殖腺
    organs/pineal.py        松果体
    organs/skin.py          皮肤（维生素 D 前体）
    organs/vascular.py      血管内皮 · 血小板
    organs/neural.py        神经胺类与递质

两条结构约定
------------
1) **激素写在产地里，受体写在靶器官里**。胰岛素在 pancreas，胰岛素受体在
   pancreas（靶器官口径按器官就近，肝/肌/脂肪没有独立模块，故随配体放在胰岛）。
   α1/β1/M2 这类由交感张力而非单一激素驱动的受体保留在 step_autonomic_receptors。
2) **谁写哪个字段，有唯一归属**。模块只写自己的字段，跨器官只读。两个器官
   写同一个字段是最典型的漂移来源，__init__ 里对重复登记直接抛错。

数值纪律（本版最重要的约束）
--------------------------
新增的一切都是「只读既有量、只写新字段」。因此 v1.3.0 已发布的标定
**逐位不变**：心率、血压、血糖、尿量、体温、睡眠、月经的轨迹与旧版一致。
这不是保守，是必要条件 —— v1.3.1 要能直接替换已部署的 v1.3.0，
不能让用户的身体在升级后忽然换了个人。验证见 docs/更新报告_v1.3.1.md。
"""
from ..dynamics import TAU
from ..state import clamp
from .base import REST_KD_MULT, kd_for, occupy
from .organs import (adrenal, central, gonads, gut, heart, kidney, liver,
                     neural, pancreas, parathyroid, pineal, skin, thyroid,
                     vascular)

__all__ = ["HORMONES", "RECEPTORS", "ORGAN_MODULES", "ORGAN_TABLE", "GCR_KD", "GCR_N",
           "step", "receptors_step", "registry_audit", "organ_table",
           "step_raas", "step_adh", "step_hpa", "step_adrenaline", "step_thyroid",
           "step_gcr_hormone", "step_autonomic_receptors",
           "REST_KD_MULT", "kd_for", "occupy"]

# 器官模块清单 —— 同时是文档与 WebUI 的分组顺序（上游到下游不重要，
# 这里按「中枢 -> 腺体 -> 靶器官 -> 局部介质」的人体解剖习惯排）。
ORGAN_MODULES = (central, thyroid, parathyroid, adrenal, pancreas, gut, kidney,
                 heart, liver, gonads, pineal, skin, vascular, neural)

# 新增轴的推进顺序：上游先算，下游后算，单步内不留环路。
# v1.3.0 的 6 条轴不在此列 —— 它们在 step() 里按旧顺序单独调用。
NEW_ORDER = (central, liver, parathyroid, kidney, thyroid, adrenal, pancreas,
             gut, heart, gonads, pineal, skin, vascular, neural)

# v1.3.0 的 GCR 参数，保持可导入（旧代码 / 文档引用）
GCR_KD = adrenal.GCR_KD
GCR_N = adrenal.GCR_N


def _collect():
    """把各器官的登记表汇总成一张总表，同时查重。"""
    hormones = {}
    receptors = {}
    for module in ORGAN_MODULES:
        organ = module.ORGAN[0]
        for name, meta in module.HORMONES.items():
            if name in hormones:
                raise ValueError("激素 %s 被 organs/%s.py 与 organs/%s.py 重复登记"
                                 % (name, hormones[name].get("organ"), organ))
            item = dict(meta)
            item["organ"] = organ
            hormones[name] = item
        for name, meta in module.RECEPTORS.items():
            if name in receptors:
                raise ValueError("受体 %s 被 organs/%s.py 与 organs/%s.py 重复登记"
                                 % (name, receptors[name].get("organ"), organ))
            item = dict(meta)
            item["organ"] = organ
            receptors[name] = item
    return hormones, receptors


HORMONES, RECEPTORS = _collect()

# 供文档/前端使用的器官索引：(id, 标题, 副标题, [激素字段], [受体字段])
ORGAN_TABLE = tuple(
    (m.ORGAN[0], m.ORGAN[1], m.ORGAN[2], tuple(m.HORMONES), tuple(m.RECEPTORS))
    for m in ORGAN_MODULES
)


def step(s, dt):
    """推进一整个内分泌步。

    v1.3.0 的 6 条轴顺序**逐字保留**（raas -> adh -> hpa -> adrenaline ->
    thyroid -> gcr）。重排会改变同一 dt 内的先后，数值就会漂 ——
    拆分时已用逐位一致的回归脚本验证过，改动后必须重跑。
    """
    # ---- v1.3.0：旧 6 轴，勿动 ----
    kidney.step_raas(s, dt)
    central.step_adh(s, dt)
    adrenal.step_hpa(s, dt)
    adrenal.step_adrenaline(s, dt)
    thyroid.step_thyroid(s, dt)
    adrenal.step_gcr_hormone(s)

    # ---- v1.3.1 新增：按器官分科推进，只写新字段 ----
    for module in NEW_ORDER:
        module.step(s, dt)

    # ---- 最后统一算受体占据（含专门实现的几条）----
    receptors_step(s)


def receptors_step(s):
    """按登记表统一计算 Hill 占据率。

    新增受体**不需要写代码**：在器官模块的 RECEPTORS 里加一条
    dict(ligand=..., kd=..., n=...) 即可 —— 这正是这版拆包的目的。
    impl 不是 "generic" 的（有专门实现或未驱动）在这里跳过。
    """
    for name, meta in RECEPTORS.items():
        if meta.get("impl") != "generic":
            continue
        ligand = float(getattr(s, meta["ligand"], 0.0))
        setattr(s, name, occupy(ligand, meta["kd"] / REST_KD_MULT, meta["n"]))
    # 腺苷受体带咖啡因竞争性拮抗，不能套通用式
    neural.step_adenosine_receptors(s)


def step_autonomic_receptors(s, symp, para):
    """自主神经受体占据（v1.3.0 原实现逐字保留 + v1.3.1 补乙酰胆碱）。

    前三条仍是**线性映射**而非 Hill 方程 —— 原实现就是线性，迁移只搬位置
    不改公式。要让它们也走 Hill，须先确定各受体 Kd 与内源配体量纲，另案。

    symp / para 由调用方（engine._autonomic）算好后传入，而不是从 s 上读：
    s.symp / s.para 是动态属性，state.py 里没有声明，asdict() 不会序列化，
    从存档恢复的身体上并不存在 —— 若在此 getattr 就会悄悄改变行为。
    """
    s.r_beta1 = clamp(0.12 + 0.55 * symp + 0.10 * s.adrenaline, 0, 1)
    s.r_alpha1 = clamp(0.08 + 0.45 * symp + 0.07 * s.ang2, 0, 1)
    s.r_m2 = clamp(0.15 + 0.55 * para, 0, 1)
    # v1.3.1：乙酰胆碱是副交感末梢的递质，只能在这里算（需要 para）。
    # 声明在 organs/neural.py，实现在此，注释互指，避免"有声明无实现"。
    s.acetylcholine = clamp(0.15 + 0.70 * para, 0.05, 1.00)


def organ_table():
    """返回器官索引的浅拷贝，供文档生成与前端分组使用。"""
    return [{"id": i, "title": t, "subtitle": st,
             "hormones": list(h), "receptors": list(r)}
            for i, t, st, h, r in ORGAN_TABLE]


def registry_audit():
    """自检：登记表 <-> BodyState <-> 时间常数 三者是否脱节。只读，不改状态。

    检查项（新增激素/受体时先跑这个，比等到运行时 AttributeError 好）：
      1) 登记的激素/受体名必须是 BodyState 的真实字段
      2) 激素的 tau 若给了字符串键，必须在 TAU 里登记过
      3) 激素声明的静息值必须等于该字段的默认值（防「文档写 0.3、代码默认 0.5」）
      4) 受体的配体必须是真实字段，Kd 必须为正
      5) impl 为 None 的受体必须写明 status（如实标注未驱动，而不是静默失效）
    返回错误列表，空表示通过。
    """
    import dataclasses
    from ..state import BodyState
    fields = {f.name: f for f in dataclasses.fields(BodyState)}
    errors = []

    for name, meta in HORMONES.items():
        if name not in fields:
            errors.append("HORMONES." + name + " 不在 BodyState 字段里")
            continue
        tau = meta.get("tau")
        if isinstance(tau, str) and tau not in TAU:
            errors.append("HORMONES." + name + " 的 tau='" + tau + "' 未在 TAU 登记")
        rest = meta.get("rest")
        default = fields[name].default
        if isinstance(rest, float) and isinstance(default, (int, float)):
            if abs(float(default) - rest) > 1e-9:
                errors.append("HORMONES." + name + " 声明静息值 %s 与 state 默认值 %s 不符"
                              % (rest, default))

    for name, meta in RECEPTORS.items():
        if name not in fields:
            errors.append("RECEPTORS." + name + " 不在 BodyState 字段里")
        ligand = meta.get("ligand")
        if ligand and ligand not in fields:
            errors.append("RECEPTORS." + name + " 的配体字段 " + str(ligand) + " 不存在")
        if float(meta.get("kd", 0.0)) <= 0.0:
            errors.append("RECEPTORS." + name + " 的 Kd 必须为正")
        if meta.get("impl") is None and not meta.get("status"):
            errors.append("RECEPTORS." + name + " 既无 impl 也未标注 status")
    return errors
