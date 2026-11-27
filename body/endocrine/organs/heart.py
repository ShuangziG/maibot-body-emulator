"""心脏：心房钠尿肽（ANP）。

APK 依据：
  激素  ANP；受体 ANPReceptor（登记在 kidney，因为它是靶器官口径）
  触发  心房牵张 —— 即循环血量与心房压

为什么 ANP 值得单独一条：它是 RAAS 的**生理对手**。插件里 RAAS 只有
「低容量 -> 保钠」这一半，没有「高容量 -> 排钠」那一半，于是容量负荷
只能靠肾脏的压力性利尿硬扛。把 ANP 算出来并暴露，才有条件将来给它接线。
本版按既定纪律**只计算不驱动**，理由同 r_gcr：不确定增益的回路不进已标定闭环。
"""
from ..base import hormone, register, seat
from ...state import clamp
from ...blood import BV_REF_ML

ORGAN = ("heart", "心脏", "心房钠尿肽")

register("anp", 120.0)

HORMONES = {
    "anp": hormone("ANP", "Heart(Atrial)", "anp", 0.25,
                   "心房钠尿肽，心房牵张时释放"),
}

RECEPTORS = {}


def step(s, dt):
    """心房牵张 = 容量偏移 + 压力偏移 + 钠负荷（三者都会拉高心房压）。"""
    stretch = (clamp((s.blood_vol - BV_REF_ML) / 800.0, 0.0, 1.5)
               + 0.6 * clamp((s.map - 100.0) / 40.0, 0.0, 1.0)
               + 0.4 * clamp((s.na - 140.0) / 10.0, 0.0, 1.0))
    s.anp = seat(s.anp, clamp(0.10 + 0.85 * stretch, 0.02, 2.00), dt, "anp")
