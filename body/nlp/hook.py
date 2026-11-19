"""情绪判定：把一句话变成 valence 增量。

口径（依据 _emotion_probe 实测）：
  1. 方向用 score() 的 pos-neg 符号，不用 polarity() —— 后者归一化后
     把强度抹平了（"烦" 与 "很烦很烦" 都是 -1.0，绝对分却是 1.0 vs 9.0）。
  2. 强度用 (|pos|+|neg|) 做对数压缩：mag=1 -> 约 0.3，mag=9 -> 约 1.0。
  3. |极性| 太小的句子不判（中性陈述不该被硬塞一个情绪）。

已知精度边界（诚实标注）：
  pxsenti 在电商评价体上自评 96%，但在**对话体**实测只有 54.2%；
  补下面这套领域词表后可到约 90%（同集自评，真实泛化未验证）。
  反讽（"真是太好了呢"）判不出来，这是规则法的固有边界。
"""
import math

from .pxsenti import Sentiment

# 领域词表：HowNet 未收录、但在日常对话里高频的情绪表达。
# 实测：不补这套词，对话体只有 54%；补后约 90%（同一份测试集，偏乐观）。
DOMAIN_POS = (
    "好耶", "厉害", "棒", "不错", "开心", "顺利", "成功", "喜欢", "舒服",
    "踏实", "安心", "感动", "可爱", "真好", "太棒", "嘿嘿", "诶嘿", "给力",
    "满意", "放心", "期待", "惊喜", "顺利搞定", "真香", "值了",
)
DOMAIN_NEG = (
    "累死", "好累", "没劲", "没意思", "烦死", "不想动", "不想理", "随便你",
    "担心", "难受", "低落", "委屈", "生气", "吃醋", "唉", "呜呜", "闷",
    "怎么回事", "多少次", "每次都这样", "不能这样", "受不了", "敷衍",
    "离谱", "无语", "过分", "懒得", "算了",
)
DOMAIN_DENY = ("别", "不用", "没事", "不会")

_ENGINE = None


def get_engine():
    """懒加载单例。首次约 60 ms、+5 MB，之后常驻；插件初始化时调用一次即可。"""
    global _ENGINE
    if _ENGINE is None:
        eng = Sentiment()
        eng.add_words(pos=DOMAIN_POS, neg=DOMAIN_NEG, deny=DOMAIN_DENY)
        _ENGINE = eng
    return _ENGINE


def analyze(text, scale=1.0, min_chars=4, max_valence=60.0, dead_zone=0.15):
    """把一句话判成 valence 增量。

    返回 dict：{"valence": float, "mag": float, "label": str, "hit": bool}
    其中 valence 是**建议写入的增量**，不写 0 以外的值表示"这句不判"。
    """
    out = {"valence": 0.0, "mag": 0.0, "label": "跳过", "hit": False}
    if not text:
        return out
    t = text.strip()
    if len(t) < min_chars:
        return out

    eng = get_engine()
    sc = eng.score(t)
    p = float(sc.get("pos", 0.0))
    n = float(sc.get("neg", 0.0))
    mag = abs(p) + abs(n)
    out["mag"] = mag
    if mag <= 0.0:
        return out

    delta = p - n
    if mag == 0.0:
        return out
    pol = delta / mag            # -1..1，方向
    if abs(pol) < dead_zone:
        out["label"] = "中性"
        return out

    # 强度：mag=1 -> ~0.30，mag=9 -> ~1.0（对数压缩，避免一句脏话顶满）
    strength = min(1.0, math.log1p(mag) / math.log1p(9.0))
    v = (1.0 if pol > 0 else -1.0) * strength * max_valence * scale
    v = max(-max_valence * scale, min(max_valence * scale, v))
    out["valence"] = round(v, 2)
    out["label"] = "正面" if pol > 0 else "负面"
    out["hit"] = abs(v) > 0.5
    return out
