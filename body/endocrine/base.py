"""内分泌包的基础设施：声明结构、时间常数登记与受体占据约定。

拆包原因见 body/endocrine/__init__.py 的模块头。本文件只放与具体器官无关的东西：
数值约定、声明辅助函数、时间常数登记。

关键约定 —— 受体 Kd 的相对量纲口径
==================================
本插件所有激素都是**无量纲相对浓度**（皮质醇静息 0.5、应激峰 1.5~3.0），
不是血中真实浓度（真值在 pmol/L 级）。若照抄文献里的解离常数，
量纲对不上，Hill 方程会给出「占据率恒为 0」—— 看着成立，实际全错。

因此全包统一：
    Kd = REST_KD_MULT × 该激素的静息值
REST_KD_MULT 取 1.6，不是随手挑的：
    n=2 时 (L/Kd)^2 = (1/1.6)^2 = 0.3906，占据 = 0.3906 / 1.3906 = 0.281
与 v1.3.0 已发布、人工核对过的 r_gcr（cortisol 静息 0.5、Kd 0.8 → 占据 0.28）
**逐位一致**。于是新旧受体处在同一标尺上，跨受体比较才有意义。

n 的取法：二聚体 / 有正协同的核受体取 2（GCR、ER、PR、TR、VDR、PTHR 等），
单体 GPCR 取 1（肾上腺素能、M2、H1/H2、AT1、V2 等）。n=1 时静息占据为 0.385，
比 n=2 略高 —— 这是单体受体没有协同放大的真实差别，不是误差。
"""
from ..dynamics import TAU, _tau, approach, hill_occupancy

__all__ = ["REST_KD_MULT", "kd_for", "occupy", "register", "seat",
           "hormone", "receptor", "touch"]

# 静息配体 -> Kd 的倍数。见模块头：1.6 由已发布的 r_gcr 标定反解得到。
REST_KD_MULT = 1.6


def kd_for(rest: float) -> float:
    """静息配体值 -> 该受体的半饱和常数（口径见模块头）。"""
    return REST_KD_MULT * max(float(rest), 1e-6)


def occupy(ligand, rest, n: float = 2.0) -> float:
    """按静息值口径算受体占据率，等价于 hill_occupancy(ligand, kd_for(rest), n)。"""
    return hill_occupancy(ligand, kd_for(rest), n)


def register(key: str, sec: float) -> float:
    """登记本包的时间常数。

    重复登记同键同值是允许的（模块被重复导入时的幂等），
    同键**异值**直接抛错：两个器官都想用同一个键、却给了不同秒数，
    是典型的复制粘贴事故，静默覆盖会让某条轴的动力学悄悄变快或变慢。
    """
    if key in TAU and abs(TAU[key] - float(sec)) > 1e-9:
        raise ValueError("时间常数键冲突：%s 已登记为 %s，本次要写 %s"
                         % (key, TAU[key], sec))
    return _tau(key, float(sec))


def seat(cur, target, dt, key):
    """一阶分泌/清除：向 target 逼近。语义包装，让器官模块读起来像生理而非数学。"""
    return approach(cur, target, dt, key)


def touch(cur, target, dt, sec):
    """同上，但时间常数直接给秒数（用于不进 TAU 表的局部量）。"""
    from ..dynamics import approach_tau
    return approach_tau(cur, target, dt, sec)


def hormone(apk, gland, tau, rest, note="", organ=None):
    """声明一条激素。

    apk    : LIFE.apk il2cpp 元数据里能对上的名字（交叉验证见 apk_report）
    gland  : 分泌它的腺体/细胞（APK 的 *Gland 类名）
    tau    : 时间常数键（必须已 register）或直接秒数
    rest   : 静息相对浓度 —— 同时也是 BodyState 的默认值，registry_audit 会核对
    """
    return dict(apk=apk, gland=gland, tau=tau, rest=float(rest),
                note=note, organ=organ)


def receptor(apk, ligand, rest, n=2.0, impl="generic", expr="", note="", status=None):
    """声明一个受体。

    ligand : 配体在 BodyState 里的**字段名**（不是中文名）
    rest   : 配体参考值，用于反解 Kd（口径见模块头）。取**静息值**；
             若配体静息就是 0（如雌二醇），则取「周期活跃值的一半」并在 note 里写明 ——
             用 0 反解会得到 Kd=0，那是一个永远饱和、恒为 1 的假受体。
    impl   : "generic" 表示由 endocrine.receptors_step 统一按 Hill 计算；
             其他值表示另有专门实现（如自主神经的线性映射、GCR 的历史实现）
    expr   : 表达组织（便于查「这个受体到底在哪个器官上」）
    status : impl 为 None 时的如实标注（未驱动 / 无可观测字段）
    """
    meta = dict(apk=apk, ligand=ligand, kd=round(kd_for(rest), 4), n=float(n),
                impl=impl, expr=expr, note=note, organ=None)
    if status:
        meta["status"] = status
    return meta
