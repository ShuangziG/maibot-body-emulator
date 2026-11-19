"""数值动力学底座：时间常数登记与一阶逼近。

为什么单独成文件（2026-11-18 结构规整）：
  engine.py 的各子系统（内分泌、情绪、体温…）都要用 approach()。
  若把它留在 engine 里，endocrine.py 导入 engine 就会形成循环导入，
  只能退化成「在 endocrine 里复制一份公式」—— 两份公式迟早会走偏。
  因此把「与生理语义无关的纯数值工具」独立出来：
    state（数据） -> dynamics（数值） -> endocrine/engine（语义）

纪律：本文件只放通用数学，不放任何生理常数或业务判断。
"""
from .state import clamp

H2S = 3600.0
TAU = {}   # 各子系统时间常数（秒），由各模块在 import 期登记

__all__ = ["H2S", "TAU", "clamp", "register_tau", "_tau", "approach", "approach_tau",
           "hill_occupancy"]


def register_tau(key, sec):
    """登记某子系统的一阶时间常数，供 approach() 按 key 取用。"""
    TAU[key] = sec
    return sec


# 旧名保留：engine.py 内有 20 余处 _tau("...") 调用，改名没有收益只有风险。
_tau = register_tau


def approach(cur, target, dt, key):
    """一阶惯性逼近，显式欧拉 + 稳定限幅。

    2026-11-08 修：旧实现一律写 cur + (target-cur)*dt/TAU，当 dt >= TAU 时
    系数 >= 1，显式欧拉越过稳定界 —— dt=20 s 时 TAU["hr"]=12 系数 1.667，
    心率与血压会在几十步内发散到负数（实测 SBP=-517、DBP=-1725）。
    限幅到 1.0 后，dt 再大也只是「一步到位」，不会过冲。
    """
    return cur + (target - cur) * min(1.0, dt / TAU[key])


def approach_tau(cur, target, dt, tau_sec):
    """同上，但时间常数直接给秒数（用于未登记进 TAU 的局部量）。"""
    return cur + (target - cur) * min(1.0, dt / max(tau_sec, 1e-6))


def hill_occupancy(ligand, kd, n=2.0):
    """受体占据率：Hill 方程。返回 0..1。

    为什么用 Hill 而不是线性比例：受体是有限位点，配体浓度翻倍不等于
    效应翻倍。n=2 对应受体二聚化的经典情形（如糖皮质激素受体）。
    临床药理与受体生理学里这是标准形式，见 ENGINE 的 GCR 实现。

    对 kd<=0 或 ligand<=0 做保护，避免除零与负幂。
    """
    if kd <= 0.0:
        return 0.0
    if ligand <= 0.0:
        return 0.0
    lp = ligand ** n
    return clamp(lp / (kd ** n + lp), 0.0, 1.0)
