# -*- coding: utf-8 -*-
"""食物表：LIFE.apk FoodType 枚举的 23 项，逐项给成分。

依据分级（见 apk_report/字段对照表.md）：
  A 级 —— 食物名称/分组/存在性：来自 global-metadata.dat 的 FoodType 枚举，
          官方分组串为「饮品 / 肉蛋鱼 / 碳水-水果-零食」。
  B 级 —— 每份的容量与营养素数值：APK 打包资源里没有数值（149 个
          MonoBehaviour 的 typetree 全扫 food/rice/banana 命中 0），
          故按 USDA FoodData Central 与《中国食物成分表》的常见份量估算。
          这一级必须在文档里标注为估算，不得冒充 APK 真值。

单位约定：
  volume_ml   一份的体积（mL），固体按近似体积记，用于胃扩张/饱腹
  water_ml    其中可自由水（mL）
  na_mmol     钠（mmol），1 g 食盐 ≈ 17.1 mmol Na
  carb_g      可利用碳水（g）
  protein_g   蛋白（g）
  fat_g       脂肪（g）
  alcohol_g   纯乙醇（g），体积 × 酒精度 × 0.789
  caffeine_mg 咖啡因（mg）
  kcal        热量（kcal）
  gi          升糖指数（葡萄糖=100），仅碳水类有意义，0 表示不适用

2026-11-11 份量重标（第十二版）：
  旧份量按「一小份」给（一碗饭 150 g、两片面包 60 g），一份正餐总胃内容物
  只有 366~535 mL，除以 900 mL 的胃容量基准只到 4~6 分饱，与"人吃正餐
  起码七分饱"不符。现按中式成人一餐的常见份量重标：一碗饭 200 g 熟、
  一份肉 120 g 熟、一碗汤 350 mL，并在餐谱里补足副食项，使
  午餐/晚餐总容积落在 640~840 mL（≈7~9 分饱），早餐 450~620 mL
  （早餐本就吃不到午餐的量，属正常）。
"""
import asyncio
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Food:
    key: str          # 与 FoodType 枚举同名
    label: str        # 中文名
    group: str        # 官方分组
    portion: str      # 一份是多少
    volume_ml: float
    water_ml: float
    na_mmol: float
    carb_g: float
    protein_g: float
    fat_g: float
    alcohol_g: float
    caffeine_mg: float
    kcal: float
    gi: int
    selectable: bool = True   # Chyle 是消化中间物，不作为可选食物


# ----------------------------------------------------------------------
# 23 项食物表（顺序与 FoodType 枚举一致）
# ----------------------------------------------------------------------
FOODS = {
    # ---- Chyle：食糜本身，不是可进食对象 ----
    "Chyle": Food("Chyle", "食糜", "消化中间物", "胃内混合物", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                  selectable=False),
    # ---- 饮品 ----
    "Water": Food("Water", "水", "饮品", "250 mL 一杯", 250, 250, 0, 0, 0, 0, 0, 0, 0, 0),
    "SmallWater": Food("SmallWater", "小口水", "饮品", "150 mL 一小杯", 150, 150, 0, 0, 0, 0, 0, 0, 0, 0),
    "Milk": Food("Milk", "牛奶", "饮品", "250 mL 一盒", 250, 220, 4.8, 12, 8, 8, 0, 0, 150, 39),
    "Coke": Food("Coke", "可乐", "饮品", "330 mL 一罐", 330, 320, 1.4, 35, 0, 0, 0, 0, 140, 63),
    "Beer": Food("Beer", "啤酒", "饮品", "330 mL 一罐 5%vol", 330, 320, 1.2, 11, 1.6, 0, 13.0, 0, 140, 89),
    "Wine": Food("Wine", "葡萄酒", "饮品", "150 mL 一杯 12%vol", 150, 130, 0.4, 3, 0.1, 0, 14.2, 0, 125, 0),
    "Vodka": Food("Vodka", "伏特加", "饮品", "45 mL 一shot 40%vol", 45, 27, 0.0, 0, 0, 0, 14.2, 0, 100, 0),
    "Coffee": Food("Coffee", "咖啡", "饮品", "240 mL 一杯", 240, 235, 0.2, 0, 0.3, 0, 0, 95, 2, 0),
    "EnergyDrink": Food("EnergyDrink", "能量饮料", "饮品", "250 mL 一罐", 250, 245, 4.3, 27, 0, 0, 0, 80, 110, 70),
    "OrangeJuice": Food("OrangeJuice", "橙汁", "饮品", "250 mL 一杯", 250, 220, 0.2, 26, 1.7, 0.3, 0, 0, 112, 50),
    # ---- 肉蛋鱼（一份 120 g 熟，约一个成人一餐的荤菜量）----
    "Chicken": Food("Chicken", "鸡胸肉", "肉蛋鱼", "120 g 熟", 110, 78, 3.8, 0, 37, 4.3, 0, 0, 198, 0),
    "Pork": Food("Pork", "猪肉", "肉蛋鱼", "120 g 熟", 100, 62, 2.9, 0, 31, 20, 0, 0, 312, 0),
    "Egg": Food("Egg", "鸡蛋", "肉蛋鱼", "一个 58 g", 58, 45, 3.2, 0.7, 7.3, 5.8, 0, 0, 83, 0),
    "Salmon": Food("Salmon", "三文鱼", "肉蛋鱼", "120 g 熟", 110, 78, 3.1, 0, 24, 16, 0, 0, 250, 0),
    # ---- 碳水 / 水果 / 零食（一碗饭 200 g 熟、两片吐司 80 g）----
    "Rice": Food("Rice", "米饭", "碳水", "一碗 200 g 熟", 200, 125, 0.2, 56, 5.4, 0.7, 0, 0, 260, 73),
    "Bread": Food("Bread", "面包", "碳水", "两片 80 g", 95, 30, 17.0, 40, 6.7, 2.7, 0, 0, 215, 75),
    "Pizza": Food("Pizza", "披萨", "碳水", "三块 200 g", 190, 88, 42.0, 53, 19, 16, 0, 0, 456, 60),
    "Soup": Food("Soup", "汤", "碳水", "350 mL 一碗", 350, 338, 25.0, 7, 3.5, 2.3, 0, 0, 70, 0),
    "Apple": Food("Apple", "苹果", "水果", "一个 200 g", 190, 172, 0.1, 28, 0.6, 0.3, 0, 0, 105, 36),
    "Banana": Food("Banana", "香蕉", "水果", "一根 120 g", 115, 90, 0.1, 27, 1.3, 0.4, 0, 0, 105, 51),
    "Nut": Food("Nut", "坚果", "零食", "一小把 40 g", 40, 2.0, 0.1, 8, 8, 20, 0, 0, 240, 15),
    "Chocolate": Food("Chocolate", "巧克力", "零食", "50 g 黑巧", 48, 0.6, 0.4, 22, 4, 17, 0, 25, 270, 40),
}

ALL_FOODS = list(FOODS.values())
SELECTABLE = [f for f in ALL_FOODS if f.selectable]


def get(key: str) -> Food:
    return FOODS[key]


# ----------------------------------------------------------------------
# 三餐组合策略
#   不是随机撒——按现实的中式/西式混搭习惯给出候选池。
#   2026-11-11 起每餐补足副食项，使总容积达到七分饱以上（午餐/晚餐
#   640~840 mL，早餐 450~620 mL）。挑选用调用方传入的 random.Random。
# ----------------------------------------------------------------------
MEAL_POOLS = {
    "breakfast": [["Milk", "Bread", "Egg", "Apple"], ["Coffee", "Bread", "Egg", "Banana"],
                  ["Milk", "Banana", "Nut", "Bread"], ["OrangeJuice", "Bread", "Egg", "Apple"],
                  ["Coffee", "Chocolate", "Bread", "Egg", "Banana"]],
    "lunch": [["Rice", "Chicken", "Soup"], ["Rice", "Pork", "Soup"],
              ["Rice", "Salmon", "Soup"], ["Pizza", "Coke", "Apple"],
              ["Rice", "Egg", "Apple", "Soup"], ["Bread", "Chicken", "Soup", "Apple"]],
    "dinner": [["Rice", "Pork", "Apple", "Soup"], ["Rice", "Salmon", "Soup"],
               ["Pizza", "Beer", "Apple"], ["Rice", "Chicken", "Banana", "Soup"],
               ["Bread", "Egg", "Soup", "Apple"], ["Rice", "Pork", "Wine", "Soup"],
               ["Rice", "Chicken", "Beer"]],
}

# 口渴时的补给选择（按口渴程度权重）
DRINK_POOL = ["Water", "Water", "Water", "SmallWater", "SmallWater",
              "Coke", "Coffee", "OrangeJuice", "EnergyDrink", "Milk"]

# 饥饿但非饭点时的加餐
SNACK_POOL = ["Nut", "Chocolate", "Banana", "Apple", "Bread", "Milk",
              "OrangeJuice", "Coffee"]


def pick_meal(slot: str, rng) -> list:
    """按餐段返回一份食物组合（key 列表）。"""
    pool = MEAL_POOLS.get(slot) or MEAL_POOLS["lunch"]
    return list(rng.choice(pool))


def pick_drink(rng) -> list:
    return [rng.choice(DRINK_POOL)]


def pick_snack(rng) -> list:
    return [rng.choice(SNACK_POOL)]


# ----------------------------------------------------------------------
# 日程文本 -> 食物（2026-11-11 新增）
#   麦生活的日程会写「点了一份生榨米粉配油条外卖」「黄焖鸡米饭」这类
#   具体食物。旧实现只取 kind=meal 判断"该吃饭了"，然后按菜谱池随机配，
#   日程里写的食物完全没进身体。这里把文本里的食物词映射到食物 key：
#   原始映射只覆盖 APK 的 23 项 FoodType；装了 food_extra.py 之后，
#   映射表被扩展（见文件末尾的钩子），能落到 300+ 个具体食物上。
#   顺序有意义：长词在前，避免「鸡」先命中「黄焖鸡」。
# ----------------------------------------------------------------------
FOOD_ALIASES = [
    ("黄焖鸡米饭", ["Chicken", "Rice"]), ("鸡米饭", ["Chicken", "Rice"]),
    ("生榨米粉", ["Rice"]), ("米粉", ["Rice"]), ("米线", ["Rice"]),
    ("河粉", ["Rice"]), ("炒饭", ["Rice", "Egg"]), ("盖饭", ["Rice"]),
    ("米饭", ["Rice"]), ("白饭", ["Rice"]), ("粥", ["Rice"]),
    ("面条", ["Rice"]), ("拉面", ["Rice"]), ("意面", ["Rice"]),
    ("馒头", ["Bread"]), ("包子", ["Bread"]), ("油条", ["Bread"]),
    ("苏打饼干", ["Bread"]), ("饼干", ["Bread"]), ("吐司", ["Bread"]),
    ("三明治", ["Bread"]), ("面包", ["Bread"]),
    ("鸡胸", ["Chicken"]), ("鸡腿", ["Chicken"]), ("鸡肉", ["Chicken"]),
    ("黄焖鸡", ["Chicken"]), ("炸鸡", ["Chicken"]),
    ("排骨", ["Pork"]), ("红烧肉", ["Pork"]), ("叉烧", ["Pork"]),
    ("猪肉", ["Pork"]), ("肉", ["Pork"]),
    ("三文鱼", ["Salmon"]), ("鱼", ["Salmon"]),
    ("鸡蛋", ["Egg"]), ("煎蛋", ["Egg"]), ("炒蛋", ["Egg"]), ("蛋", ["Egg"]),
    ("披萨", ["Pizza"]), ("匹萨", ["Pizza"]), ("汉堡", ["Pizza"]),
    ("汤", ["Soup"]), ("羹", ["Soup"]),
    ("苹果", ["Apple"]), ("香蕉", ["Banana"]),
    ("坚果", ["Nut"]), ("核桃", ["Nut"]), ("花生", ["Nut"]), ("杏仁", ["Nut"]),
    ("巧克力", ["Chocolate"]),
    ("奶茶", ["Milk"]), ("拿铁", ["Milk"]), ("牛奶", ["Milk"]), ("奶", ["Milk"]),
    ("可乐", ["Coke"]), ("汽水", ["Coke"]),
    ("啤酒", ["Beer"]), ("红酒", ["Wine"]), ("葡萄酒", ["Wine"]),
    ("白酒", ["Vodka"]), ("伏特加", ["Vodka"]),
    ("咖啡", ["Coffee"]), ("美式", ["Coffee"]),
    ("橙汁", ["OrangeJuice"]), ("果汁", ["OrangeJuice"]),
    ("能量饮料", ["EnergyDrink"]),
    ("水", ["Water"]), ("茶", ["Water"]),
]


def parse_menu(text: str, limit: int = 6) -> list:
    """从一段日程/聊天文本里抽出食物 key 列表（去重、保序）。

    只做关键词匹配，不做语义理解：认不出来的返回空列表，
    调用方据此回落随机配餐。这是一层保守的近似映射，
    把「生榨米粉配油条」落成 Rice + Bread，而不是当成没吃过。
    """
    if not text:
        return []
    s = str(text)
    found = []
    consumed = [False] * len(s)
    for word, keys in FOOD_ALIASES:
        start = 0
        while True:
            idx = s.find(word, start)
            if idx < 0:
                break
            if any(consumed[idx:idx + len(word)]):
                start = idx + 1
                continue
            for i in range(idx, idx + len(word)):
                consumed[i] = True
            for k in keys:
                if k not in found:
                    found.append(k)
            start = idx + len(word)
            if len(found) >= limit:
                return found[:limit]
    return found[:limit]


# 复合菜名：这些词是"一道菜"，光靠关键词只能得到其中一样料，
# 需要模型补全（红烧肉 -> 猪肉 + 米饭）。其余别名（香蕉、牛奶、鸡蛋）
# 本身就是单一食材，再拆纯属浪费一次模型调用。
_DISH_WORDS = frozenset({
    "红烧肉", "炒饭", "盖饭", "三明治", "汉堡", "披萨", "匹萨", "包子",
    "黄焖鸡", "黄焖鸡米饭", "鸡米饭", "炸鸡", "粥", "面条", "拉面", "意面",
    "米粉", "米线", "河粉", "生榨米粉", "馒头", "油条", "苏打饼干",
    "奶茶", "拿铁", "排骨", "叉烧",
})


def is_plain_food(text: str) -> bool:
    """判断这段文本是不是"本身就是一样东西"，而不是一道需要拆的菜。

    True 表示不必再调模型：香蕉、牛奶、鸡蛋、米饭这类单一食材，
    以及直接给出的英文 key。
    """
    s = str(text or "").strip()
    if not s:
        return False
    if s in FOODS:
        return True
    if s in _DISH_WORDS:
        return False
    for word, _keys in FOOD_ALIASES:
        if word == s:
            return word not in _DISH_WORDS
    return False


# ----------------------------------------------------------------------
# 菜名 -> 食材拆解（2026-11-11 新增，走宿主 utils 模型）
#   关键词映射只能做"一菜对一料"的近似：红烧肉 -> Pork，"红烧肉盖饭"
#   也只会得到 Pork，米饭那半份直接丢了。真正的拆解需要语义理解，
#   交给宿主的 utils 轻量模型（ctx.llm.generate，task=utils）。
#   模型调用失败一律回落关键词结果 —— 进食链路不能因为模型抖动而断。
# ----------------------------------------------------------------------

# 给模型的食材表：key + 中文名。
_DECOMPOSE_KEYS = [
    ("Milk", "牛奶"), ("Bread", "面包"), ("Egg", "鸡蛋"), ("Rice", "米饭"),
    ("Pork", "猪肉"), ("Chicken", "鸡肉"), ("Salmon", "三文鱼"), ("Soup", "汤"),
    ("Pizza", "披萨"), ("Apple", "苹果"), ("Banana", "香蕉"), ("Nut", "坚果"),
    ("Chocolate", "巧克力"), ("Coffee", "咖啡"), ("Water", "水"),
    ("Coke", "可乐"), ("OrangeJuice", "橙汁"), ("EnergyDrink", "能量饮料"),
    ("Beer", "啤酒"), ("Wine", "葡萄酒"), ("Vodka", "白酒"),
]

# 代码围栏标记（避免在源码里直接写字面反引号）。
_FENCE = chr(96) * 3

_DECOMPOSE_PROMPT = """你在把"人吃掉的一份食物"拆成实际进入身体的基础食材。

只能从下面这些 key 里选，不得新增、不得自造：
{keys}

规则：
1. 只输出一个 JSON 数组，元素是上面的 key 字符串。不要解释、不要代码块、不要任何多余文字。
2. 一道菜拆成它的组成食材：红烧肉盖饭 -> ["Pork","Rice"]；番茄炒蛋 -> ["Egg"]；
   黄焖鸡米饭 -> ["Chicken","Rice"]；牛肉面 -> ["Rice"]。
3. 如果写的是一顿正餐（早饭/午饭/晚饭/外卖）但没点明主食，按中式习惯补主食：
   早餐补 "Bread"，午晚餐补 "Rice"。
4. 饮品只给饮品 key，不要补主食。
5. 完全认不出任何食物，输出 []。
6. 最多 {limit} 项，去重。

要拆解的内容：{text}
JSON 数组："""


def _extract_key_list(raw, limit: int = 6) -> list:
    """从模型回包里抠出合法的食物 key 列表，非法项一律丢弃。"""

    s = str(raw or "").strip()
    if not s:
        return []
    if _FENCE in s:
        for part in s.split(_FENCE):
            if "[" in part:
                s = part
                break
    lo = s.find("[")
    hi = s.rfind("]")
    if lo < 0 or hi <= lo:
        return []
    try:
        data = json.loads(s[lo:hi + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            item = item.get("key") or item.get("name") or ""
        k = str(item or "").strip()
        if k not in FOODS:
            for key, label in _DECOMPOSE_KEYS:
                if k == label:
                    k = key
                    break
        entry = FOODS.get(k)
        if entry is None or not entry.selectable:
            continue
        if k not in out:
            out.append(k)
        if len(out) >= limit:
            break
    return out


async def decompose_menu(text: str, limit: int = 6, llm=None,
                         timeout: float = 20.0) -> tuple:
    """把一段「吃了什么」的文本拆成食物 key 列表。

    返回 (keys, source)，source 取值：
      "alias"  —— 关键词命中已够，未调模型
      "llm"    —— 模型拆解成功（与关键词结果取并集）
      "alias+" —— 模型失败/超时，回落关键词结果
      "empty"  —— 文本为空

    设计取舍：模型结果与关键词结果取并集，模型在前。模型容易漏掉文本里
    明明写着的具体名词（"一杯牛奶"只回 Milk 是对的，但"牛奶配油条"
    可能只回 Milk），关键词能兜住；反过来关键词把"红烧肉"只当 Pork，
    模型能补出米饭。并集再截断到 limit。
    """

    raw = str(text or "").strip()
    if not raw:
        return [], "empty"
    base = parse_menu(raw, limit=limit)
    if llm is None or not hasattr(llm, "generate"):
        return base, "alias"
    # 关键词已经认出两样以上，语义空间不大，省一次模型调用。
    if len(base) >= 2:
        return base, "alias"

    keys_text = "\n".join(f"- {k}（{label}）" for k, label in _DECOMPOSE_KEYS)
    prompt = _DECOMPOSE_PROMPT.format(keys=keys_text, limit=int(limit), text=raw[:400])
    try:
        payload = await asyncio.wait_for(
            llm.generate(
                prompt=prompt,
                task_name="utils",
                temperature=0.1,
                max_tokens=200,
            ),
            timeout=timeout,
        )
    except Exception:
        return base, "alias+"

    if not isinstance(payload, dict) or not payload.get("success", False):
        return base, "alias+"
    got = _extract_key_list(payload.get("response") or payload.get("reasoning"), limit)
    if not got:
        return base, "alias+"
    merged = list(got)
    for k in base:
        if k not in merged:
            merged.append(k)
    return merged[:limit], "llm"


# 七分饱的容积下限（mL）。真人一餐 700~900 mL 才到七八分饱，
# 这里取 620 作为"一份正餐"的门槛：低于它就再补一道主食/汤。
MEAL_TARGET_ML = 620.0

# 补足用的候选（按容积从小到大）：挑"刚好补够"的那一项，
# 避免本来 593 mL 的早餐被强塞一整碗汤变成 943 mL。
_TOPUP_LADDER = ["Nut", "Banana", "Bread", "Apple", "Soup", "Rice"]


def ensure_meal_size(keys, target: float = MEAL_TARGET_ML) -> list:
    """把一份正餐补到七分饱以上（按容积），返回新的 key 列表。

    2026-11-11 新增。旧实现里一份早餐 366 mL、午餐 535 mL，
    吃到胃里 gut_full 只有 0.4~0.6，压根不到七分饱 —— 用户反馈
    「吃一次东西胃里太少了」。现在所有"正餐"（meal、外部日程）都过这道闸，
    按容积补到 620 mL；零食和饮料不走这条路径。

    补法：只追加不删除，先挑"补上刚好够"的最小一项（坚果/香蕉/面包/苹果/
    汤/米饭），一项不够就继续补，最多补三道。所以只点了杯奶茶就当正餐时，
    最终会补成"牛奶 + 米饭 + 汤"——这是有意的：正餐就该有正餐的量。
    调用方若只是喝一杯，应传 kind=drink，不走这条路径。
    """
    out = [k for k in keys if k in FOODS]
    if not out:
        return out
    vol = combo_totals(out)["volume_ml"]
    if vol >= target:
        return out
    # 先找"补上就够"的最小一项；没有能一次补够的，就追加最大的那项。
    best = None
    for cand in _TOPUP_LADDER:
        if cand in out:
            continue
        v = FOODS[cand].volume_ml
        if vol + v >= target:
            best = cand
            break
    if best is None:
        rest = [c for c in _TOPUP_LADDER if c not in out]
        if rest:
            best = rest[-1]
    if best is not None:
        out.append(best)
    # 极端情况（点了一杯奶茶当"正餐"）一项补不够，再补到够为止。
    guard = 0
    while combo_totals(out)["volume_ml"] < target * 0.85 and guard < 3:
        guard += 1
        rest = [c for c in _TOPUP_LADDER if c not in out]
        if not rest:
            break
        out.append(rest[-1])
    return out


def combo_summary(keys) -> str:
    """把一组食物 key 翻成一句中文，如「米饭 + 鸡胸肉 + 汤」。"""
    return " + ".join(FOODS[k].label for k in keys if k in FOODS)


def combo_totals(keys) -> dict:
    """合计一组食物的全部成分，用于写事件与自检。"""
    tot = {"volume_ml": 0.0, "water_ml": 0.0, "na_mmol": 0.0, "carb_g": 0.0,
           "protein_g": 0.0, "fat_g": 0.0, "alcohol_g": 0.0,
           "caffeine_mg": 0.0, "kcal": 0.0}
    for k in keys:
        f = FOODS.get(k)
        if not f:
            continue
        for name in tot:
            tot[name] += getattr(f, name)
    return tot


# ----------------------------------------------------------------------
# 扩展食物库（可选）：23 项之外的中式日常饮食，见同目录 food_extra.py。
#
# 钩子必须放在文件末尾：此时 Food / FOODS / 各池子都已定义，扩展模块里的
# `from .food import Food` 才不会拿到半成品模块。
#
# 旧写法是 `except Exception: pass` —— 扩展坏了也静默通过，看起来改好了
# 其实什么都没变（issue 里正是踩了这个坑）。这里失败一律写 warning，
# 并把结果记在 _FOOD_EXTRA_ADDED，便于自检：
#   > 0  新增条数（正常；本表为 288，最终 23 -> 311）
#   = 0  扩展存在但没有可新增的（重复安装，正常）
#   =-1  扩展加载失败（去看日志）
# 删掉 food_extra.py 即完全回滚；本文件除这一小段外未做任何改动。
# ----------------------------------------------------------------------
def _load_food_extra() -> int:
    """加载可选扩展表，返回新增条数；-1 表示加载失败。

    三种情况必须分开，否则又回到「静默失败」：
      0    没有 food_extra.py —— 用户删掉了扩展，属正常回滚，不告警
      > 0  装上了，追加了这么多条
      -1   文件在但坏了 —— 写 warning，主库照常可用
    """
    import logging
    from pathlib import Path

    log = logging.getLogger(__name__)
    # 先看文件在不在。不能用异常类型区分：`from . import x` 在文件缺失时
    # 抛的是 ImportError("cannot import name ...")，不是 ModuleNotFoundError，
    # 照类型判断会把「没装」误报成「装坏」。
    if not Path(__file__).with_name("food_extra.py").exists():
        return 0
    try:
        from . import food_extra
    except Exception as exc:  # noqa: BLE001
        log.warning("食物库扩展导入失败：%s: %s", type(exc).__name__, exc)
        return -1
    try:
        return int(food_extra.install())
    except Exception as exc:  # noqa: BLE001
        log.warning("食物库扩展安装失败：%s: %s", type(exc).__name__, exc)
        return -1


_FOOD_EXTRA_ADDED = _load_food_extra()
