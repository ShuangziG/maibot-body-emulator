"""把一次身体快照画成一张 HTML 卡片，供 render.html2png 出图。

设计约束（由宿主决定，不是偏好）：
1. 宿主 render.html2png 默认 allow_network=False，外部字体、CSS、图片一律加载不到，
   所以样式必须全部写在文档内联的 <style> 里，颜色不能引用外部变量。
2. 卡片是给人看快照，不承载仿真 —— 只读 BodyState，不推进任何状态。
3. 任何字段缺失都要能降级，不能用 KeyError 把整张图搞没。
"""

from __future__ import annotations

import html as _html

from .translate import FEELING_LEVELS, eat_phase, phase

# 画布定宽。viewport 给 760，卡片自身 720 + 左右留白，避免被裁边。
CARD_WIDTH = 720

_TONE_COLOR = {
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "bad": "#f87171",
    "info": "#60a5fa",
}


def _pick(table, value: float) -> str:
    """复用 translate 的分档表，取第一个上界大于 value 的词。"""

    for hi, word in table:
        if value < hi:
            return word
    return table[-1][1]


def _tone(levels, value: float) -> str:
    """把分档词映射成颜色档。levels 为 [(上界, 词, 档位), ...]。"""

    for hi, _word, tone in levels:
        if value < hi:
            return tone
    return levels[-1][2]


def _esc(text: str) -> str:
    return _html.escape(str(text or ""), quote=True)


def _clock(hour: float) -> str:
    total = int(round((hour % 24.0) * 60))
    return f"{total // 60:02d}:{total % 60:02d}"


def _bar(label: str, value: float, maximum: float, color: str) -> str:
    """一根水平进度条。value 超出 maximum 时截断，不画到轨道外面。"""

    ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    width = ratio * 100.0
    return (
        f'<div class="bar-row">'
        f'<span class="bar-label">{_esc(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" '
        f'style="width:{width:.1f}%;background:{color}"></span></span>'
        f'<span class="bar-value">{value:.0f}</span>'
        f'</div>'
    )


def _tile(label: str, value: str, unit: str, word: str, color: str) -> str:
    return (
        f'<div class="tile">'
        f'<div class="tile-label">{_esc(label)}</div>'
        f'<div class="tile-value">{_esc(value)}'
        f'<span class="tile-unit">{_esc(unit)}</span></div>'
        f'<div class="tile-word" style="color:{color}">{_esc(word)}</div>'
        f'</div>'
    )


def build_html(s) -> str:
    """把 BodyState 画成一张 PNG 卡片对应的 HTML。"""

    hr_tone = _tone([(50, "", "warn"), (60, "", "warn"), (85, "", "ok"),
                     (100, "", "warn"), (999, "", "bad")], s.hr)
    map_tone = _tone([(60, "", "bad"), (70, "", "warn"), (100, "", "ok"),
                      (110, "", "warn"), (999, "", "bad")], s.map)
    sao2_tone = "bad" if s.sao2 < 94 else ("warn" if s.sao2 < 97 else "ok")
    glu_tone = "bad" if (s.glucose < 3.0 or s.glucose > 9.0) else (
        "warn" if (s.glucose < 4.0 or s.glucose > 6.5) else "ok")
    temp_tone = "ok" if 36.3 <= s.temp <= 37.2 else (
        "bad" if (s.temp < 35.5 or s.temp >= 38.0) else "warn")
    rr_tone = "ok" if 10.0 <= s.rr <= 20.0 else "warn"

    health_tone = "ok" if s.health >= 80 else ("warn" if s.health >= 40 else "bad")

    vitals = "".join([
        _tile("心率", f"{s.hr:.0f}", "bpm",
              _pick(FEELING_LEVELS["hr"], s.hr), _TONE_COLOR[hr_tone]),
        _tile("血压", f"{s.sbp:.0f}/{s.dbp:.0f}", "mmHg",
              _pick(FEELING_LEVELS["map"], s.map), _TONE_COLOR[map_tone]),
        _tile("血氧", f"{s.sao2:.0f}", "%",
              _pick(FEELING_LEVELS["sao2"], s.sao2), _TONE_COLOR[sao2_tone]),
        _tile("血糖", f"{s.glucose:.1f}", "mmol/L",
              _pick(FEELING_LEVELS["glucose"], s.glucose), _TONE_COLOR[glu_tone]),
        _tile("体温", f"{s.temp:.1f}", "℃",
              _pick(FEELING_LEVELS["temp"], s.temp), _TONE_COLOR[temp_tone]),
        _tile("呼吸", f"{s.rr:.0f}", "次/分",
              "平稳" if rr_tone == "ok" else "偏快或偏慢", _TONE_COLOR[rr_tone]),
    ])

    bars = "".join([
        _bar("精力", s.energy, 100.0, "#60a5fa"),
        _bar("饥饿", s.hunger, 100.0, "#fbbf24"),
        _bar("口渴", s.thirst, 100.0, "#22d3ee"),
        _bar("压力", s.stress, 100.0, "#a78bfa"),
    ])

    gastric = getattr(s, "gastric_volume", 0.0)
    gut_note = eat_phase(gastric) if gastric > 60.0 else "空腹"
    meal = getattr(s, "last_meal_items", "") or "还没有记录"

    flags = ""
    if getattr(s, "hospital_until", 0.0) > s.t:
        left_h = (s.hospital_until - s.t) / 3600.0
        flags = f'<div class="flag">正在医院接受急救，还有约 {left_h:.1f} 小时</div>'
    elif getattr(s, "critical", False):
        flags = '<div class="flag">生命体征不稳，正在观察</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    margin: 0; padding: 20px;
    background: #0f1218;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  #card {{
    width: {CARD_WIDTH}px;
    background: linear-gradient(160deg, #171d26 0%, #131820 100%);
    border: 1px solid #2a323f;
    border-radius: 18px;
    padding: 26px 28px 22px;
    color: #e9eef6;
  }}
  .head {{ display: flex; align-items: flex-end; justify-content: space-between; }}
  .title {{ font-size: 26px; font-weight: 700; letter-spacing: 1px; }}
  .title span {{ font-size: 15px; font-weight: 400; color: #7d8b9f; margin-left: 10px; }}
  .stamp {{ font-size: 14px; color: #7d8b9f; text-align: right; line-height: 1.6; }}
  .stamp b {{ color: #c7d2e0; font-weight: 600; }}
  .divider {{ height: 1px; background: #262e3a; margin: 18px 0; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .tile {{
    background: #1b2230; border: 1px solid #262f3d; border-radius: 12px;
    padding: 14px 16px 12px;
  }}
  .tile-label {{ font-size: 13px; color: #7d8b9f; }}
  .tile-value {{ font-size: 27px; font-weight: 700; margin-top: 4px; letter-spacing: .5px; }}
  .tile-unit {{ font-size: 12px; font-weight: 400; color: #6f7d90; margin-left: 5px; }}
  .tile-word {{ font-size: 12px; margin-top: 3px; }}
  .sec-title {{ font-size: 13px; color: #7d8b9f; margin-bottom: 10px; letter-spacing: .5px; }}
  .bar-row {{ display: flex; align-items: center; margin-bottom: 9px; }}
  .bar-label {{ width: 44px; font-size: 13px; color: #a9b6c7; }}
  .bar-track {{
    flex: 1; height: 9px; background: #1b2230; border-radius: 5px; overflow: hidden;
    margin: 0 12px;
  }}
  .bar-fill {{ display: block; height: 100%; border-radius: 5px; }}
  .bar-value {{ width: 30px; text-align: right; font-size: 13px; color: #c7d2e0; }}
  .gut {{ display: flex; gap: 12px; }}
  .gut-box {{
    flex: 1; background: #1b2230; border: 1px solid #262f3d; border-radius: 12px;
    padding: 12px 16px;
  }}
  .gut-k {{ font-size: 12px; color: #7d8b9f; }}
  .gut-v {{ font-size: 15px; color: #dbe4ef; margin-top: 5px; }}
  .flag {{
    margin-top: 14px; padding: 10px 14px; border-radius: 10px;
    background: rgba(248,113,113,.12); border: 1px solid rgba(248,113,113,.35);
    color: #fca5a5; font-size: 14px;
  }}
  .foot {{ margin-top: 18px; font-size: 12px; color: #5f6c7e; display: flex; justify-content: space-between; }}
</style></head>
<body><div id="card">
  <div class="head">
    <div class="title">麦麦身体<span>此刻的生理状态</span></div>
    <div class="stamp">第 <b>{s.t / 86400.0:.1f}</b> 天 · <b>{_clock(s.hour)}</b><br>{_esc(phase(s.hour))}</div>
  </div>
  <div class="divider"></div>
  <div class="grid">{vitals}</div>
  <div class="divider"></div>
  <div class="sec-title">体感</div>
  {bars}
  <div class="divider"></div>
  <div class="sec-title">消化 · 健康</div>
  <div class="gut">
    <div class="gut-box">
      <div class="gut-k">胃内容物</div>
      <div class="gut-v">{gastric:.0f} mL · {_esc(gut_note)}</div>
    </div>
    <div class="gut-box">
      <div class="gut-k">最近一餐</div>
      <div class="gut-v">{_esc(meal)}</div>
    </div>
    <div class="gut-box">
      <div class="gut-k">健康</div>
      <div class="gut-v" style="color:{_TONE_COLOR[health_tone]}">{s.health:.0f} / 100</div>
    </div>
  </div>
  {flags}
  <div class="foot"><span>生理状态为仿真数据，不构成医学建议</span><span>lifebody</span></div>
</div></body></html>"""
