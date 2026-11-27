# -*- coding: utf-8 -*-
"""食物养分画像：给 311 项食物补膳食纤维与 4 项关键矿物质/维生素（v1.4.0 新增）。

为什么要画像，而不是逐项查表
----------------------------
原食物表只有 volume / water / na / carb / protein / fat / alcohol /
caffeine / kcal / gi 十项。要做"膳食营养结构"，缺的是**纤维与微量营养素**。
311 项逐项查《中国食物成分表》当然最准，但本项目的既有纪律是
"查不到的标 B 级估算、不得冒充真值"（见 food.py 文档头）——
手工填 311 条微量营养素，绝大多数只能靠类别拍脑袋，反而制造了一种
"看起来很精确"的假象。

因此本表用三级法，每级都可核对：
  1. **类别画像**：每个食物分组给一套「每份能提供多少」的估计值，
     数值取自《中国食物成分表（第 6 版）》与 USDA FoodData Central 的
     该类食物常见份量区间中位；
  2. **关键词细分**：深色叶菜、全谷物、乳制品、动物肝脏、柑橘类等高影响
     子类用关键词规则改写画像（它们与同类其它食物的差异是数量级的）；
  3. **逐项覆盖**：23 项基础食物在 food.py 里直接写死真值，不进画像。

单位：一份（与 Food.portion 同一份量）提供
  fiber_g  膳食纤维 g
  k_mg     钾 mg
  ca_mg    钙 mg
  fe_mg    铁 mg
  vc_mg    维生素 C mg

可信度：本表是 **B 级估算**，用于"膳食结构评分"的量级判断，
不得当作营养素化验值引用。评分口径见 body/nutrition.py。
"""

__all__ = ["GROUP_PROFILE", "KEYWORD_PROFILE", "overrides", "profile_for",
           "is_junk", "NUTRIENT_KEYS"]

NUTRIENT_KEYS = ("fiber_g", "k_mg", "ca_mg", "fe_mg", "vc_mg")

# ---------------------------------------------------------------------------
# 第 1 级：分组画像（每份）
# ---------------------------------------------------------------------------
# 说明：菜品与主食的"一份"是成人一餐量（主食 200 g 熟、菜品约 150~200 g），
# 蔬菜 150~200 g，水果一个，饮品 250~500 mL，肉蛋 120 g 熟或一个蛋。
GROUP_PROFILE = {
    "主食":   dict(fiber_g=1.8, k_mg=90.0,  ca_mg=18.0,  fe_mg=0.6, vc_mg=0.0),
    "菜品":   dict(fiber_g=2.6, k_mg=280.0, ca_mg=70.0,  fe_mg=1.6, vc_mg=14.0),
    "蔬菜":   dict(fiber_g=2.6, k_mg=320.0, ca_mg=85.0,  fe_mg=1.4, vc_mg=28.0),
    "水果":   dict(fiber_g=2.4, k_mg=240.0, ca_mg=16.0,  fe_mg=0.4, vc_mg=22.0),
    "肉蛋":   dict(fiber_g=0.0, k_mg=250.0, ca_mg=14.0,  fe_mg=1.9, vc_mg=0.0),
    "水产":   dict(fiber_g=0.0, k_mg=300.0, ca_mg=45.0,  fe_mg=1.0, vc_mg=0.0),
    "饮品":   dict(fiber_g=0.0, k_mg=90.0,  ca_mg=20.0,  fe_mg=0.1, vc_mg=2.0),
    "零食":   dict(fiber_g=1.4, k_mg=130.0, ca_mg=30.0,  fe_mg=0.8, vc_mg=1.0),
    "甜点":   dict(fiber_g=0.8, k_mg=80.0,  ca_mg=40.0,  fe_mg=0.5, vc_mg=1.0),
    "酒类":   dict(fiber_g=0.0, k_mg=40.0,  ca_mg=6.0,   fe_mg=0.1, vc_mg=0.0),
    # 2026-11-27（v1.4.0）：调味类（食用油/糖/黄油）。这几样只有热量，
    # 微量营养素按 0 记，别让 _default 给它们凭空补出纤维和维生素。
    "调味":   dict(fiber_g=0.0, k_mg=1.0,   ca_mg=1.0,   fe_mg=0.0, vc_mg=0.0),
    # 兜底：未知分组按"中性混合餐"给
    "_default": dict(fiber_g=1.5, k_mg=180.0, ca_mg=45.0, fe_mg=1.0, vc_mg=8.0),
}

# ---------------------------------------------------------------------------
# 第 2 级：关键词细分（子串命中即改写，按顺序首个命中生效）
# ---------------------------------------------------------------------------
# 每条：(关键词元组, 画像, 说明)。关键词一律小写 ASCII 或中文原样子串匹配。
KEYWORD_PROFILE = [
    # ---- 深色叶菜：钾/钙/铁/维C/纤维都高一个量级 ----
    (("菠菜", "油麦菜", "小白菜", "油菜", "芥蓝", "空心菜", "苋菜", "茼蒿",
      "韭菜", "西兰花", "西兰", "菠菜", "生菜", "芹菜", "紫甘蓝", "羽衣甘蓝"),
     dict(fiber_g=3.4, k_mg=520.0, ca_mg=150.0, fe_mg=2.4, vc_mg=42.0), "深色叶菜"),
    # ---- 菌藻：纤维与钾高、铁中等 ----
    (("木耳", "香菇", "金针菇", "杏鲍菇", "口蘑", "海带", "紫菜", "裙带"),
     dict(fiber_g=4.0, k_mg=380.0, ca_mg=90.0, fe_mg=2.2, vc_mg=3.0), "菌藻类"),
    # ---- 全谷物 / 粗粮：纤维 3 倍于精制主食 ----
    (("糙米", "杂粮", "五谷", "全麦", "燕麦", "荞麦", "黑米", "玉米", "红薯",
      "紫薯", "山药", "南瓜", "芋头", "藜麦", "小米", "绿豆", "红豆", "薏米"),
     dict(fiber_g=4.2, k_mg=300.0, ca_mg=35.0, fe_mg=1.6, vc_mg=6.0), "全谷粗粮"),
    # ---- 豆制品：钙与植物蛋白 ----
    (("豆腐", "豆干", "腐竹", "千张", "豆皮", "毛豆", "黄豆", "豆花", "素鸡"),
     dict(fiber_g=1.6, k_mg=270.0, ca_mg=160.0, fe_mg=2.4, vc_mg=1.0), "豆制品"),
    # ---- 乳制品：钙的主来源 ----
    (("牛奶", "酸奶", "奶酪", "芝士", "拿铁", "卡布", "摩卡", "奶盖", "炼乳",
      "奶油", "黄油"),
     dict(fiber_g=0.0, k_mg=340.0, ca_mg=250.0, fe_mg=0.2, vc_mg=1.5), "乳制品"),
    # ---- 动物肝脏 / 血制品：铁与维C 极高 ----
    (("肝", "猪血", "鸭血", "血豆腐"),
     dict(fiber_g=0.0, k_mg=280.0, ca_mg=12.0, fe_mg=11.0, vc_mg=15.0), "肝脏血制品"),
    # ---- 红肉：铁高于禽鱼 ----
    (("牛肉", "羊肉", "猪肉", "排骨", "五花", "里脊", "肉末", "肉丸", "香肠",
      "火腿", "培根", "腊肉"),
     dict(fiber_g=0.0, k_mg=300.0, ca_mg=12.0, fe_mg=2.6, vc_mg=0.0), "红肉"),
    # ---- 柑橘 / 高维C 水果 ----
    (("橙", "橘", "柑", "柠檬", "猕猴桃", "奇异果", "草莓", "山楂", "柚子",
      "番石榴", "枣"),
     dict(fiber_g=2.6, k_mg=260.0, ca_mg=30.0, fe_mg=0.4, vc_mg=48.0), "高维C水果"),
    # ---- 坚果种子：纤维/钾/铁都高 ----
    (("坚果", "核桃", "杏仁", "腰果", "花生", "瓜子", "芝麻", "开心果", "榛子"),
     dict(fiber_g=3.2, k_mg=280.0, ca_mg=60.0, fe_mg=1.8, vc_mg=0.5), "坚果种子"),
    # ---- 油炸 / 酥皮：纤维低、钠高（热量与钠已在原表里，这里只补纤维微量）----
    (("炸", "油条", "薯条", "鸡排", "春卷", "酥"),
     dict(fiber_g=1.0, k_mg=180.0, ca_mg=25.0, fe_mg=0.9, vc_mg=0.5), "油炸面点"),
    # ---- 汤 / 粥：一份里干货少 ----
    (("汤", "粥", "羹", "糊"),
     dict(fiber_g=0.8, k_mg=150.0, ca_mg=18.0, fe_mg=0.5, vc_mg=3.0), "汤粥"),
    # ---- 茶 / 咖啡 / 白水：几乎不计 ----
    (("茶", "咖啡", "水", "苏打", "可乐", "雪碧", "芬达", "气泡"),
     dict(fiber_g=0.0, k_mg=40.0, ca_mg=4.0, fe_mg=0.0, vc_mg=0.0), "清饮"),
]

# ---------------------------------------------------------------------------
# 第 3 级：逐项覆盖（key -> 画像）。只放"画像明显不对"的具体条目。
# ---------------------------------------------------------------------------
_OVERRIDES = {
    "Milk": dict(fiber_g=0.0, k_mg=380.0, ca_mg=300.0, fe_mg=0.1, vc_mg=1.0),
    "Yogurt": dict(fiber_g=0.0, k_mg=340.0, ca_mg=240.0, fe_mg=0.1, vc_mg=1.0),
    "SoyMilk": dict(fiber_g=0.6, k_mg=300.0, ca_mg=40.0, fe_mg=1.5, vc_mg=0.0),
    "OrangeJuice": dict(fiber_g=0.5, k_mg=500.0, ca_mg=25.0, fe_mg=0.2, vc_mg=60.0),
    "SpinachOnly": dict(fiber_g=3.4, k_mg=520.0, ca_mg=150.0, fe_mg=2.4, vc_mg=42.0),
}


def overrides() -> dict:
    """返回逐项覆盖表（只读副本语义：调用方不要原地改）。"""
    return dict(_OVERRIDES)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def _blank() -> dict:
    return {k: 0.0 for k in NUTRIENT_KEYS}


def profile_for(key: str, label: str, group: str, carb_g: float = 0.0,
                protein_g: float = 0.0, fat_g: float = 0.0,
                kcal: float = 0.0) -> dict:
    """给一项食物算养分画像（每份）。命中顺序：覆盖表 -> 关键词 -> 分组。"""
    if key in _OVERRIDES:
        return dict(_OVERRIDES[key])
    text = "%s %s" % (str(label or ""), str(key or ""))
    for words, prof, _why in KEYWORD_PROFILE:
        for w in words:
            if w and w in text:
                return dict(prof)
    prof = GROUP_PROFILE.get(str(group or ""), GROUP_PROFILE["_default"])
    return dict(prof)


# ---------------------------------------------------------------------------
# 垃圾食品判定（供"垃圾食品成瘾"用，见 body/habits.py）
# ---------------------------------------------------------------------------
# 判据不是"某个食物的名字"，而是三条可核对的营养学特征任一条成立：
#   1) 高油：脂肪供能比 > 45% 且 一份热量 >= 180 kcal
#   2) 高钠：钠 > 30 mmol/份（约 700 mg 食盐当量）且 热量 >= 150 kcal
#   3) 高糖：可利用碳水 >= 25 g 且 蛋白 < 3 g（几乎纯糖）
# 另外分组属于"零食/甜点"且热量 >= 150 kcal 的一律计入 —— 与
# 中国居民膳食指南 2022 对"限制添加糖/油炸零食"的表述同向。
_JUNK_EXPLICIT = {
    "Coke", "CokeZero", "Sprite", "Fanta", "IcedTea", "EnergyDrink", "RedBull",
    "MilkTea", "PearlMilkTea", "InstantCoffee", "Chocolate", "IceCream",
    "Pizza", "薯条", "炸鸡", "炸鸡排", "油条", "春卷", "蛋糕", "蛋挞", "泡面",
}


def is_junk(key: str, group: str, kcal: float, na_mmol: float,
            carb_g: float, protein_g: float, fat_g: float) -> bool:
    """该食物是否计入"垃圾食品"口径（高油/高钠/高糖三选一）。"""
    if key in _JUNK_EXPLICIT:
        return True
    kcal = max(0.0, float(kcal or 0.0))
    if kcal <= 0.0:
        return False
    fat_kcal = 9.0 * max(0.0, float(fat_g or 0.0))
    if fat_kcal >= 0.45 * kcal and kcal >= 180.0:
        return True
    if float(na_mmol or 0.0) > 30.0 and kcal >= 150.0:
        return True
    if float(carb_g or 0.0) >= 25.0 and float(protein_g or 0.0) < 3.0 and kcal >= 150.0:
        return True
    return str(group or "") in ("零食", "甜点") and kcal >= 150.0
