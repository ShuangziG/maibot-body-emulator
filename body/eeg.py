# -*- coding: utf-8 -*-
"""EEG 脑电波生成。

**这不是医学数据，是仿真波形。** 不可用于任何诊断。

数据依据：
  - APK 元数据有 `EEG` / `Electroencephalogram` 指标（与 ECG 同级）
  - 频段划分（Delta/Theta/Alpha/Beta/Gamma）是脑电常识
  - 各频段功率与睡眠阶段的对应关系，是睡眠医学共识

两条驱动通路：
  1. **睡眠阶段** → 主导频段（这是主体）
       清醒 → Beta 为主（13-30 Hz）
       浅睡 → Theta 为主（4-8 Hz）
       深睡 → Delta 为主（0.5-4 Hz，高波幅慢波）
       REM  → 混合低频 + 类似清醒的 β（"锯齿波"）
  2. **情绪** → 频段偏移（**预留接口，当前未接入**）
       依据常识：焦虑/紧张使 Beta 升高、Alpha 下降；
       平静放松使 Alpha 升高；抑郁使左右不对称（这里不做半球区分）。
       情绪输入从 `emotion_arousal` 与 `emotion_valence` 两个维度来，
       由外部（对话情绪分析、麦生活状态）写入。默认中性，不影响波形。

采样率取 200 Hz（真实 EEG 是 200-1000 Hz，展示够用）。
"""
from __future__ import annotations

import math

DEFAULT_HZ = 200

# 各睡眠阶段的基础频段功率（相对权重）。
# 顺序：delta(0.5-4), theta(4-8), alpha(8-13), beta(13-30), gamma(30-45)
_STAGE_BANDS = {
    "awake": (0.05, 0.15, 0.25, 0.45, 0.10),
    "light": (0.15, 0.45, 0.20, 0.18, 0.02),
    "deep":  (0.70, 0.20, 0.06, 0.04, 0.00),
    "rem":   (0.10, 0.35, 0.10, 0.40, 0.05),
}

_BAND_RANGES = (
    ("delta", 0.5, 4.0),
    ("theta", 4.0, 8.0),
    ("alpha", 8.0, 13.0),
    ("beta", 13.0, 30.0),
    ("gamma", 30.0, 45.0),
)


def emotion_band_shift(arousal: float = 0.0, valence: float = 0.0) -> tuple:
    """**情绪接口（预留，当前未接线）**

    输入两个维度，返回对各频段功率的乘性修正系数。

    arousal（唤醒度）: -1 极度平静 … 0 中性 … +1 高度紧张
    valence（效价）  : -1 非常消极 … 0 中性 … +1 非常积极

    返回 (d_delta, d_theta, d_alpha, d_beta, d_gamma)，都是乘法系数。

    **当前实现**：接收参数但不产生任何偏移，返回全 1.0。
    这样接线时只要把真实的情绪值传进来，不用改调用方。

    未来接线方式：
      1. 在 BodyState 里加 emotion_arousal / emotion_valence 字段
      2. 由聊天情绪分析或麦生活状态写入
      3. 把这两个值传给 generate_eeg()
    """
    a = max(-1.0, min(1.0, float(arousal or 0.0)))
    v = max(-1.0, min(1.0, float(valence or 0.0)))

    # ---- 预留逻辑（暂时不生效，返回中性）----
    # 设计意图（等接线时取消注释）：
    #   高唤醒 → beta/gamma 升，alpha/delta 降
    #   消极效价 → theta 略升（思虑），alpha 再降
    # if a != 0.0 or v != 0.0:
    #     d_delta = 1.0 - 0.25 * max(0.0, a)
    #     d_theta = 1.0 + 0.15 * max(0.0, -v)
    #     d_alpha = 1.0 - 0.30 * max(0.0, a)
    #     d_beta  = 1.0 + 0.40 * max(0.0, a)
    #     d_gamma = 1.0 + 0.30 * max(0.0, a)
    #     return (d_delta, d_theta, d_alpha, d_beta, d_gamma)

    del a, v   # 明确表示当前不参与计算
    return (1.0, 1.0, 1.0, 1.0, 1.0)


def generate_eeg(stage: str = "awake", seconds: float = 10.0,
                 hz: int = DEFAULT_HZ, amplitude_scale: float = 1.0,
                 arousal: float = 0.0, valence: float = 0.0,
                 t_offset: float = 0.0) -> dict:
    """生成一段 EEG 波形。

    t_offset（秒）决定从时间轴上的哪一点开始。每次调用传不同的值，
    拿到的就是不同的波形段 —— 前端刷新时看到的是真正流动的脑电，
    而不是同一段录音重播。

    2026-11-14 修：旧实现每次都从 t=0 生成，实测连续两次调用返回的点完全相同。

    返回：
      {"hz": 200, "seconds": 10.0, "points": [...], "stage": "deep",
       "bands": {"delta": 0.70, ...}, "note": "仿真波形，非医学数据"}

    points 是 -1~1 的浮点数组。
    """
    st = str(stage or "awake")
    if st not in _STAGE_BANDS:
        st = "awake"
    base = _STAGE_BANDS[st]

    # 情绪修正（当前恒为 1.0，因为 emotion_band_shift 未接线）
    shift = emotion_band_shift(arousal, valence)
    weights = [base[i] * shift[i] for i in range(5)]
    total = sum(weights) or 1.0
    weights = [w / total for w in weights]

    eff_hz = max(100, int(hz))
    n = int(max(1.0, seconds) * eff_hz)
    dt = 1.0 / eff_hz

    # 每个频段用一个固定"频率簇"合成，避免逐点随机导致锯齿。
    # 每个频段取 3 个相近频率叠加，形成自然的拍频感。
    band_waves = []
    for bi, (name, lo, hi) in enumerate(_BAND_RANGES):
        w = weights[bi]
        if w <= 0.001:
            band_waves.append(None)
            continue
        # 中心频率 + 两个侧频
        fc = (lo + hi) / 2.0
        freqs = (fc * 0.85, fc, fc * 1.15)
        # 相位用确定性方式生成，保证同一时刻刷新波形一致
        phases = [(bi * 1.7 + k * 2.3) % (2 * math.pi) for k in range(3)]
        band_waves.append((w, freqs, phases))

    points = []
    for i in range(n):
        # 加上 t_offset：从时间轴上的真实位置开始，而非永远从 0。
        t = i * dt + float(t_offset or 0.0)
        v = 0.0
        for bw in band_waves:
            if bw is None:
                continue
            w, freqs, phases = bw
            s = 0.0
            for k in range(3):
                s += math.sin(2 * math.pi * freqs[k] * t + phases[k]) / 3.0
            v += w * s
        # 深睡时波幅更大（慢波高波幅是 EEG 的真实特征）
        amp = amplitude_scale * (1.35 if st == "deep" else 1.0)
        v *= amp
        points.append(round(max(-1.0, min(1.0, v)), 4))

    return {
        "hz": eff_hz,
        "seconds": round(float(seconds), 2),
        "points": points,
        "stage": st,
        "bands": {name: round(weights[i], 3)
                  for i, (name, _lo, _hi) in enumerate(_BAND_RANGES)},
        "arousal": round(float(arousal or 0.0), 2),
        "valence": round(float(valence or 0.0), 2),
        "emotion_wired": False,
        "note": "仿真波形，非医学数据",
    }
