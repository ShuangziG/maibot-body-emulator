"""数值 -> 人话 的翻译层。

生理引擎输出的是 hr=72.3、glucose=5.4 这样的数字，但接给 AI 当肉体时，
需要的是「有点累」「饿得慌」「心跳得快」这种可直接进入语言模型的表述。
本模块负责这三件事：
  1. describe() 把一次快照翻成一段中文自述
  2. FEELING_LEVELS 给出各维度的分档阈值与词
  3. brief() 输出一行紧凑摘要，用于高频注入上下文

2026-11-05 修订：只保留 APK 元数据有依据的维度。睡眠债/孤独/卫生/性欲/
自信 等分档已删除（APK 里查无对应量），判据见 apk_report/字段对照表.md。
"""
from .state import BodyState, clamp


class _Attrs:
    """把 snapshot() 返回的 dict 包成可属性访问的对象。

    snapshot() 只导出对外关心的字段，describe 里还引用 pain/thermal 等，
    因此先用一份默认 BodyState 兜底，再用 dict 覆盖 —— 缺字段不会炸。
    """
    def __init__(self, d):
        self.__dict__.update(BodyState().__dict__)
        self.__dict__.update(d)


def _as_state(s):
    return _Attrs(s) if isinstance(s, dict) else s

# 每维：[(上界, 词), ...]，从低到高匹配
FEELING_LEVELS = {
    "hr": [(50, "心率偏慢"), (60, "心率正常偏低"), (85, "心率正常"),
           (100, "心跳偏快"), (130, "心跳很快"), (999, "心脏狂跳")],
    "map": [(60, "血压偏低"), (70, "血压正常偏低"), (100, "血压正常"),
            (110, "血压偏高"), (130, "血压高"), (999, "血压很高")],
    "sao2": [(90, "明显缺氧"), (94, "血氧偏低"), (97, "血氧正常偏低"), (101, "血氧充足")],
    "glucose": [(3.0, "低血糖，手抖"), (4.0, "血糖偏低，有点饿"), (6.5, "血糖平稳"),
                (9.0, "血糖偏高"), (999, "血糖很高")],
    "temp": [(35.5, "体温过低"), (36.3, "有点发冷"), (37.2, "体温正常"),
             (38.0, "低烧"), (39.0, "发烧"), (999, "高烧")],
    "energy": [(10, "几乎撑不住"), (25, "极度疲惫"), (40, "很累"), (60, "有点乏"),
               (80, "精神尚可"), (101, "精力充沛")],
    "hunger": [(10, "不饿"), (30, "有点想吃东西"), (55, "饿了"), (75, "很饿"),
               (90, "饿得发慌"), (101, "饿到胃痛")],
    "thirst": [(10, "不渴"), (30, "有点口干"), (55, "口渴"), (75, "很渴"),
               (90, "渴得难受"), (101, "严重缺水")],
    "stress": [(15, "很放松"), (35, "平静"), (55, "有点紧绷"), (75, "压力较大"),
               (90, "非常焦虑"), (101, "濒临崩溃")],
    "depression": [(5, "情绪平稳"), (15, "有点低落"), (35, "情绪低迷"),
                   (60, "明显抑郁"), (101, "重度低落")],
    "pain": [(1, "没有疼痛"), (20, "轻微不适"), (45, "明显疼痛"), (70, "很疼"), (101, "剧痛")],
}

# 昼夜段
PHASES = [((0, 5), "深夜"), ((5, 8), "清晨"), ((8, 12), "上午"),
          ((12, 14), "中午"), ((14, 18), "下午"), ((18, 23), "晚上"), ((23, 24), "深夜")]


def _pick(table, value):
    for hi, word in table:
        if value < hi:
            return word
    return table[-1][1]


def eat_phase(gastric_ml: float) -> str:
    """按胃内容物体积给一个体感词。阈值对应「刚吃完 / 半排空 / 快空了」。"""
    if gastric_ml > 700.0:
        return "不少东西，有点撑"
    if gastric_ml > 350.0:
        return "东西，还没消化完"
    if gastric_ml > 150.0:
        return "点东西，快排空了"
    return "少量残渣"


def drunk_level(bac: float) -> str:
    """BAC 分档。0.2 微醺，0.5 明显，0.8 法定醉驾线，1.5 以上危险。"""
    if bac > 1.5:
        return "明显醉酒"
    if bac > 0.8:
        return "醉得厉害"
    if bac > 0.5:
        return "有点上头"
    return "微醺"


def phase(hour: float) -> str:
    for (lo, hi), name in PHASES:
        if lo <= hour < hi:
            return name
    return "深夜"


def describe(s, detail: bool = True) -> str:
    """把一次身体快照翻成一段中文自述。接受 BodyState 或 snapshot() 的 dict。"""
    s = _as_state(s)
    parts = [f"现在是{phase(s.hour)}"]

    # 核心体征
    parts.append(f"心率{s.hr:.0f}（{_pick(FEELING_LEVELS['hr'], s.hr)}）"
                 f"，血压{s.sbp:.0f}/{s.dbp:.0f}（{_pick(FEELING_LEVELS['map'], s.map)}）"
                 f"，血氧{s.sao2:.0f}%（{_pick(FEELING_LEVELS['sao2'], s.sao2)}）")

    if detail:
        parts.append(f"血糖{s.glucose:.1f} mmol/L（{_pick(FEELING_LEVELS['glucose'], s.glucose)}）"
                     f"，体温{s.temp:.1f}℃（{_pick(FEELING_LEVELS['temp'], s.temp)}）")

    # 主观感受
    feel = [f"精力：{_pick(FEELING_LEVELS['energy'], s.energy)}",
            f"饥饿：{_pick(FEELING_LEVELS['hunger'], s.hunger)}",
            f"口渴：{_pick(FEELING_LEVELS['thirst'], s.thirst)}",
            f"情绪：{_pick(FEELING_LEVELS['stress'], s.stress)}"]
    if detail:
        if s.pain > 1:
            feel.append(f"疼痛：{_pick(FEELING_LEVELS['pain'], s.pain)}")
        if s.depression > 15:
            feel.append(f"心境：{_pick(FEELING_LEVELS['depression'], s.depression)}")
    parts.append("，".join(feel))

    # 消化与嗜好品：这一层是 APK FoodType/AddContent/BAC 的对应物，
    # 只说「吃了什么」和「胃里还压着多少」，不说热量，避免变成营养软件。
    gut = []
    if getattr(s, "gastric_volume", 0.0) > 60.0:
        gut.append(f"胃里有{eat_phase(s.gastric_volume)}")
    if getattr(s, "last_meal_items", ""):
        gut.append(f"最近一餐是{s.last_meal_items}")
    if gut:
        parts.append("，".join(gut))
    if getattr(s, "bac", 0.0) > 0.10:
        parts.append(f"血液里有酒精（BAC {s.bac:.2f} g/L，{drunk_level(s.bac)}）")
    if getattr(s, "caffeine", 0.0) > 40.0:
        parts.append("咖啡因还在起效")

    if getattr(s, "hospital_until", 0.0) > s.t:
        left_h = (s.hospital_until - s.t) / 3600.0
        parts.append(f"【正在医院接受急救，还有约 {left_h:.1f} 小时】")
    elif getattr(s, "critical", False):
        parts.append("【生命体征不稳，正在观察】")
    elif s.health < 40:
        parts.append(f"（健康状况 {s.health:.0f}/100，需要关注）")

    return "。".join(parts) + "。"


def brief(s) -> str:
    """一行紧凑摘要，用于高频注入上下文。接受 BodyState 或 dict。"""
    s = _as_state(s)
    flags = []
    if s.hunger > 70:
        flags.append("饿")
    if s.thirst > 70:
        flags.append("渴")
    if s.stress > 70:
        flags.append("紧绷")
    if s.pain > 30:
        flags.append("疼")
    if s.hr > 100:
        flags.append("心跳快")
    if s.sao2 < 93:
        flags.append("缺氧")
    if getattr(s, "bac", 0.0) > 0.5:
        flags.append("醉")
    if getattr(s, "gastric_volume", 0.0) > 700.0:
        flags.append("饱")

    tag = ("[" + "/".join(flags) + "]") if flags else ""
    return (f"{phase(s.hour)} HR{s.hr:.0f} BP{s.sbp:.0f}/{s.dbp:.0f} "
            f"Glu{s.glucose:.1f} E{s.energy:.0f} H{s.hunger:.0f} "
            f"T{s.thirst:.0f} S{s.stress:.0f} {tag}").strip()


def explain_change(prev: dict, cur: dict, keys=None) -> str:
    """对比两次快照，用自然语言说明身体发生了什么变化。重点看体感维度。"""
    keys = keys or ["energy", "hunger", "thirst", "stress", "hr", "glucose", "temp"]
    names = {"energy": "精力", "hunger": "饥饿", "thirst": "口渴", "stress": "压力",
             "hr": "心率", "glucose": "血糖", "temp": "体温", "sao2": "血氧"}
    out = []
    for k in keys:
        if k not in prev or k not in cur:
            continue
        d = cur[k] - prev[k]
        unit = {"hr": "bpm", "glucose": "mmol/L", "temp": "℃"}.get(k, "")
        if abs(d) < 0.5:
            continue
        arrow = "升" if d > 0 else "降"
        out.append(f"{names.get(k, k)}{arrow}{abs(d):.1f}{unit}")
    return "，".join(out) + "。" if out else "身体各项指标基本稳定。"
