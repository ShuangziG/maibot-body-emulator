# -*- coding: utf-8 -*-
"""ECG 心电图波形生成。

**这不是医学数据，是仿真波形。** 用于 WebUI 展示「生命体征」的视觉表现，
不可用于任何诊断或医学判断。

为什么是生成而不是存储：
  真实 ECG 采样率 250-500 Hz，1 分钟就是 15000~30000 个采样点。
  存进 body_state.json 会让存档爆炸。所以这里只在需要展示时
  实时生成一个短窗口（默认 10 秒），前端拿到的是点的数组，不落库。

数据依据：
  - APK 元数据有 `ECG` / `Electrocardiography` 指标（与 HR/BP 同级）
  - 波形形态是标准心电图常识：P 波 → QRS 波群 → T 波
  - 具体振幅与时间参数是按教科书典型值写的**估算**，无专门文献

波形与已有生理量的关系：
  - 周期长度由 hr（心率）决定：RR 间期 = 60 / hr 秒
  - QRS 振幅由 sv（每搏输出量）轻度调制
  - 基线漂移由呼吸频率 rr 调制（呼吸性窦性心律不齐）
"""
from __future__ import annotations

import math

# 默认采样率。真实 ECG 是 250-500 Hz，但展示用的曲线 100 Hz 已经足够平滑，
# 点数少一半，前端也轻。
DEFAULT_HZ = 100


def _gauss(t: float, center: float, width: float, amp: float) -> float:
    """高斯脉冲，用于构造 P/Q/R/S/T 各波。

    用高斯而不是方波，是因为真实心电波形是平滑的电位变化，
    用方波画出来会像方块拼接。
    """
    if width <= 0:
        return 0.0
    d = (t - center) / width
    return amp * math.exp(-0.5 * d * d)


def beat_waveform(phase: float, sv_ratio: float = 1.0) -> float:
    """一个心动周期内的电位（归一化到 -1~1 附近）。

    phase 为 0~1 的周期内相位。标准形态：
      P 波  在 0.10 附近，小正向
      Q 波  在 0.20 附近，小负向
      R 波  在 0.24 附近，尖锐正向（最高）
      S 波  在 0.28 附近，负向
      T 波  在 0.50 附近，较宽正向

    时间位置按心率 60-100 的典型比例取，是**估算**。
    """
    # P 波（心房去极化）：宽而低
    p = _gauss(phase, 0.10, 0.025, 0.15)
    # Q 波：小负向
    q = _gauss(phase, 0.20, 0.008, -0.10)
    # R 波：最高最尖，振幅随每搏输出量微调
    r = _gauss(phase, 0.24, 0.010, 1.0 * sv_ratio)
    # S 波：负向
    s = _gauss(phase, 0.28, 0.012, -0.25)
    # T 波（心室复极化）：宽而中高
    t = _gauss(phase, 0.50, 0.045, 0.30)
    return p + q + r + s + t


def generate_ecg(hr: float = 72.0, sv: float = 82.0, rr: float = 16.0,
                 seconds: float = 10.0, hz: int = DEFAULT_HZ,
                 seed_noise: float = 0.012, t_offset: float = 0.0) -> dict:
    """生成一段 ECG 波形。

    t_offset（秒）决定波形从「时间轴上的哪一点」开始。
    每次调用传不同的 t_offset，拿到的就是不同的波形段 —— 这样前端
    循环刷新时看到的是真正向前流动的曲线，而不是同一段录音重播。

    2026-11-14 修：旧实现没有这个参数，每次都从 t=0 生成，
    结果是「一段 20 秒波形无限循环」，与真实监护仪的持续新生完全不同。
    实测：连续两次 generate_ecg(hr=76) 返回的点完全相同。

    返回：
      {"hz": 100, "seconds": 10.0, "points": [...], "bpm": 72.0,
       "beats": 12, "rr_ms": 833}
    points 是 -1~1 之间的浮点数组，前端直接用。

    **噪声用小幅度确定性抖动而不是随机数**：这样同一时刻反复刷新
    看到的波形完全一致，不会因为刷新而"跳动"。
    """
    hr = max(30.0, min(200.0, float(hr or 72.0)))
    rr = max(6.0, min(40.0, float(rr or 16.0)))
    # 每搏输出量对 R 波振幅的影响：sv 82 为基准，最多 ±15%
    sv_ratio = 1.0 + max(-0.15, min(0.15, (float(sv or 82.0) - 82.0) / 82.0 * 0.5))

    period = 60.0 / hr                      # 一个心动周期的秒数
    # 采样率要足够高才能捕获 R 波尖峰。R 波宽度约 0.01 周期，
    # 100 Hz 在心率 100 时每周期只有 60 个采样点，容易漏掉峰值。
    # 自适应：保证每个心动周期至少 120 个采样点。
    min_per_beat = 120
    need_hz = int(min_per_beat / period) if period > 0 else 100
    eff_hz = max(int(hz), need_hz, 100)
    n = int(max(1.0, seconds) * eff_hz)
    dt = 1.0 / eff_hz

    points = []
    for i in range(n):
        # 关键：加上 t_offset，让波形从时间轴上的真实位置开始，
        # 而不是每次都从 0 开始（那样就是同一段无限重播）。
        t = i * dt + float(t_offset or 0.0)
        phase = (t % period) / period       # 0~1 周期内相位

        v = beat_waveform(phase, sv_ratio)

        # 呼吸性基线漂移：吸气时心率略快、基线略升，这是真实的
        # （respiratory sinus arrhythmia + 电极随呼吸移动）
        breath_period = 60.0 / rr if rr > 0 else 4.0
        breath_phase = 2.0 * math.pi * (t % breath_period) / breath_period
        v += 0.035 * math.sin(breath_phase)

        # 确定性微抖动，模拟电极噪声（不是随机数，保证可复现）
        v += seed_noise * math.sin(t * 137.0) * math.cos(t * 61.0)

        points.append(round(v, 4))

    beats = int(seconds / period) if period > 0 else 0
    return {
        "hz": eff_hz,
        "seconds": round(float(seconds), 2),
        "points": points,
        "bpm": round(hr, 1),
        "beats": beats,
        "rr_ms": int(round(period * 1000.0)),
        "note": "仿真波形，非医学数据",
    }
