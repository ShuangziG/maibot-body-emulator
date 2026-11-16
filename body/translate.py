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
#
# 2026-11-16 新增 NORMAL_BAND：每个维度的「正常带」(lo, hi)。
# 用途有且只有一个 —— describe() 判断某一项要不要说出口。
# 落在正常带内的事项对 LLM 没有任何信息量（"心率正常"这句话不改变
# 她该说什么），写进去只是占注意力。所以正常就整项省略，异常才点名。
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

# 各维度的正常带（落在带内就不必对 LLM 提）。取值来自生理常识与
# 本项目原有分档表的「正常」档，宁可略宽 —— 去噪的目的是砍掉
# "一切正常"的废话，不是把轻微波动也报成异常。
NORMAL_BAND = {
    "hr": (55.0, 95.0),
    "map": (70.0, 105.0),
    "sao2": (95.0, 100.0),
    "glucose": (3.9, 7.8),      # 餐后 <7.8 属正常（ADA 正常糖耐量口径）
    "temp": (36.3, 37.3),
    "energy": (45.0, 100.0),    # 低于 45 才算"精力不济"
    "hunger": (0.0, 55.0),      # 高于 55 才算"饿了"
    "thirst": (0.0, 55.0),
    "stress": (0.0, 55.0),
    "depression": (0.0, 15.0),
    "pain": (0.0, 10.0),
}

# 体感条一律按「数值 0→100 从左到右」画，不做任何反向。
# 2026-11-16 二修：上一版把饥饿/口渴/压力/疼痛/心境全部取反，想让
# "满=舒服"六行统一 —— 但这是错的方向。压力、疼痛、心境本来就应该
# 「0 在左、100 在右」，把它们倒过来画反而不符合直觉。
# 真正别扭的只有「饥饿」「口渴」两行：标签是"饥饿"，值却写着"不饿"，
# 读起来自相矛盾。解决方式是**换标签**而不是翻条：
#   饥饿 -> 饱腹（值高 = 吃饱了）
#   口渴 -> 水分（值高 = 不缺水）
# 这样标签、数值、条的方向三者一致，且条保持 0-100 正向。
FEELING_DIRECTION = {
    "energy": True,
    # 饥饿/口渴这两个字段的原始语义是「值越高越糟」（hunger 高 = 饿）。
    # 界面把它们改名成「饱腹」「水分」（值越高越好），所以**数值必须反转**，
    # 否则会出现「刚吃饱 -> 饱腹条只剩 1% -> 写着很饿」的自相矛盾。
    # 压力/疼痛/心境不改名，保持原值直通。
    "hunger": False,
    "thirst": False,
    "stress": True,
    "pain": True,
    "depression": True,
}

# 各维度展示给用户看的词。
# 注意口径：这里的词描述的是**该维度自身的数值**，不是"还剩多少舒适"。
# 饥饿改为「饱腹」后，数值越高越饱，所以词也要按这个方向重写。
FEELING_DISPLAY = {
    # 饱腹：入参是 hunger 原值，所以**低值 = 饱**，高值 = 饿。
    "hunger": [(10, "吃得很饱"), (30, "挺饱的"), (55, "不太饿"), (75, "有点想吃"),
               (90, "饿了"), (101, "很饿")],
    # 水分：入参是 thirst 原值，低值 = 不缺水，高值 = 渴。
    "thirst": [(10, "水分充足"), (30, "不渴"), (55, "还行"), (75, "有点口干"),
               (90, "口渴"), (101, "很渴")],
    "stress": [(15, "很放松"), (35, "轻松"), (55, "平静"), (70, "有点紧绷"),
               (85, "压力较大"), (95, "非常焦虑"), (101, "濒临崩溃")],
    "pain": [(1, "没有疼痛"), (20, "轻微不适"), (40, "有点疼"), (60, "明显疼痛"),
             (80, "很疼"), (95, "疼得厉害"), (101, "剧痛")],
    "depression": [(5, "情绪平稳"), (15, "平静"), (35, "有点低落"), (55, "情绪低迷"),
                   (75, "明显低落"), (90, "很低落"), (101, "非常低落")],
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
    """按胃内容物体积给一个口语化的饱腹感。"""
    if gastric_ml > 700.0:
        return "挺饱的"
    if gastric_ml > 350.0:
        return "还不太饿"
    if gastric_ml > 150.0:
        return "差不多消化完了"
    return "已经空了"


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


def _is_normal(key: str, value: float) -> bool:
    """该维度是否落在正常带内（= 不必对 LLM 提）。"""
    band = NORMAL_BAND.get(key)
    if band is None:
        return False
    lo, hi = band
    return lo <= value <= hi


def feeling_word(key: str, value: float) -> str:
    """体感条的展示词（描述该维度自身的数值，不描述"还剩多少舒适"）。"""
    table = FEELING_DISPLAY.get(key)
    if table is None:
        table = FEELING_LEVELS.get(key, [])
    return _pick(table, value) if table else ""


def feeling_bar_pct(key: str, value: float) -> float:
    """体感条的填充百分比。

    方向由 FEELING_DIRECTION 决定：
      True  —— 原值直通（精力/压力/疼痛/心境：值高，条长）
      False —— 取反（hunger/thirst：原值是「越饿越高」，但界面标签
               已改成「饱腹/水分」，含义反过来了，条也必须反过来画）

    不取反会出现「刚吃饱 -> 饱腹条 0% -> 写着很饿」的矛盾。
    """
    v = max(0.0, min(100.0, float(value)))
    if FEELING_DIRECTION.get(key, True):
        return v
    return 100.0 - v


def describe(s, detail: bool = True) -> str:
    """把一次身体快照翻成一段中文自述。接受 BodyState 或 snapshot() 的 dict。

    2026-11-16 重写：正常项整项省略，只说异常。

    旧实现是「把 66 个字段里挑十来个，每个后面挂一个括号评语」，于是
    健康人身上会生成一长串「心率72（心率正常），血压118/77（血压正常），
    血氧100%（血氧充足）」—— 这些话对 LLM 的决策没有一分钱信息量，
    只是占注意力、稀释真正要紧的那一两项。
    新口径：落在 NORMAL_BAND 内的一律不说；正常时 describe 只输出
    时段与少量"有内容"的项（刚吃了什么、饿不饿），异常时点名异常项
    并给出数值与程度词。
    """
    s = _as_state(s)
    parts = [f"现在是{phase(s.hour)}"]

    # ---- 核心体征：只在异常时点名 ----
    # 正常时这一段整体消失，异常时逐项列出"哪一项、多少、什么程度"。
    vital_bits = []
    if not _is_normal("hr", s.hr):
        vital_bits.append(f"心率{s.hr:.0f}（{_pick(FEELING_LEVELS['hr'], s.hr)}）")
    if not _is_normal("map", s.map):
        vital_bits.append(f"血压{s.sbp:.0f}/{s.dbp:.0f}（{_pick(FEELING_LEVELS['map'], s.map)}）")
    if not _is_normal("sao2", s.sao2):
        vital_bits.append(f"血氧{s.sao2:.0f}%（{_pick(FEELING_LEVELS['sao2'], s.sao2)}）")
    if detail:
        if not _is_normal("glucose", s.glucose):
            vital_bits.append(f"血糖{s.glucose:.1f} mmol/L（{_pick(FEELING_LEVELS['glucose'], s.glucose)}）")
        if not _is_normal("temp", s.temp):
            vital_bits.append(f"体温{s.temp:.1f}℃（{_pick(FEELING_LEVELS['temp'], s.temp)}）")
    if vital_bits:
        parts.append("，".join(vital_bits))

    # ---- 主观感受：同样只报异常 ----
    feel = []
    if not _is_normal("energy", s.energy):
        feel.append(f"精力：{_pick(FEELING_LEVELS['energy'], s.energy)}")
    if not _is_normal("hunger", s.hunger):
        feel.append(f"饥饿：{_pick(FEELING_LEVELS['hunger'], s.hunger)}")
    if not _is_normal("thirst", s.thirst):
        feel.append(f"口渴：{_pick(FEELING_LEVELS['thirst'], s.thirst)}")
    if not _is_normal("stress", s.stress):
        feel.append(f"情绪：{_pick(FEELING_LEVELS['stress'], s.stress)}")
    if detail:
        if not _is_normal("pain", s.pain):
            feel.append(f"疼痛：{_pick(FEELING_LEVELS['pain'], s.pain)}")
        if not _is_normal("depression", s.depression):
            feel.append(f"心境：{_pick(FEELING_LEVELS['depression'], s.depression)}")
    if feel:
        parts.append("，".join(feel))

    # ---- 消化与嗜好品：这一层是 APK FoodType/AddContent/BAC 的对应物，
    # 只说「吃了什么」和「胃里还压着多少」，不说热量，避免变成营养软件。
    # 注意这两项与上面的"正常就省略"略有不同：「刚吃了米饭」即使不异常
    # 也是 LLM 常用的事实（会被问"你吃了没"），故仍然保留。
    gut = []
    if getattr(s, "last_meal_items", ""):
        gut.append(f"刚吃了{s.last_meal_items}")
    if getattr(s, "gastric_volume", 0.0) > 150.0:
        gut.append(eat_phase(s.gastric_volume))
    if gut:
        parts.append("，".join(gut))
    if getattr(s, "bac", 0.0) > 0.10:
        parts.append(f"喝过点酒，{drunk_level(s.bac)}")
    if getattr(s, "caffeine", 0.0) > 40.0:
        parts.append("咖啡因还在起效")

    # ---- 需要点名的严重状态 ----
    if getattr(s, "hospital_until", 0.0) > s.t:
        left_h = (s.hospital_until - s.t) / 3600.0
        parts.append(f"【正在医院接受急救，还有约 {left_h:.1f} 小时】")
    elif getattr(s, "critical", False):
        parts.append("【生命体征不稳，正在观察】")
    elif s.health < 40:
        parts.append(f"（健康状况 {s.health:.0f}/100，需要关注）")

    # 全部正常：给一句明确的"没什么特别的"，比空字符串更稳 ——
    # LLM 拿到空内容容易误以为注入失败而自行编造状态。
    if len(parts) == 1:
        parts.append("身体没什么特别的感觉")
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
