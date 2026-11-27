"""麦麦身体 —— 让麦麦有一副持续运转的身体。

设计要点：
1. 单例 BodyEngine 由后台 asyncio 任务驱动，dt=10 仿真秒一步（实测 30 秒以上数值发散）。
2. WebUI 的 query 只读内存快照，不做仿真 —— 宿主对单次 WebUI API 有 10 秒预算
   与每插件 2 并发的硬限制，仿真必须活在后台任务里。
3. 存档是一份 BodyState 的 JSON 快照 + 落盘墙钟时间，启动时按墙钟差值追帧。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import API, Command, HomeCard, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .body import BodyEngine, BodyState, EventBus, brief, build_html, describe
from .body.history import build_history_payload
from .body.translate import feeling_bar_pct, feeling_word
from .body.food import (
    _FOOD_EXTRA_ADDED,
    ALL_FOODS,
    DRINK_POOL,
    FOOD_ALIASES,
    FOODS,
    MEAL_POOLS,
    SNACK_POOL,
    combo_summary,
    combo_compact,
    decompose_menu,
    is_plain_food,
    parse_menu,
)
from .body.translate import (NORMAL_BAND, FEELING_LEVELS, drunk_level, eat_phase, phase,
                               recent_meal_text)
from .body.nlp import analyze as _nlp_analyze
# v1.4.0 新子系统：只读展示 + 习惯判定。四个模块与 engine 同构（纯函数推进
# state），插件侧只需要调用它们的 judge/registry_audit，不重复实现业务逻辑。
from .body import blood as _blood
from .body import immune as _immune
from .body import nutrition as _nutrition
from .body import habits as _habits
from .config import LifeBodyConfig
from .link import LifeLink

PLUGIN_VERSION = "1.4.0"
H2S = 3600.0
SAVE_FILE = "body_state.json"

# 体感冲突校正的注入文本（v1.2.5）。只陈述事实，不写禁止式指令 ——
# 模型需要知道"胃空但不饿"，不需要被逐条告知"不能说什么"。
# 早期版本 79 字、含三层语义，实测评估认为会稀释注意力，故压到 15 字。
CONFLICT_NOTE_TEXT = "【身体】刚消化完，胃空但不饿。"

# ---- 工具可见性（PR #8）----
# feeding 关闭时，body_eat 的说明书没有用途，不该每次请求都占常驻工具表。
# 宿主对非 visible 的工具按 deferred 处理：名字与短描述仍会出现在一条
# <system-reminder> 里（可被 tool_search 发现），但参数 schema 不再进提示词，
# 实测单次请求省约 427 字符（约 250 token）。


def _parse_feeding_enabled(text: str):
    """从 config.toml 文本里读 [feeding].enabled。

    返回 True / False，读不出时为 None（调用方按「开着」处理）。
    优先用 tomllib（3.11+），没有则退化成只认该段里 enabled 的轻量解析 ——
    这里只关心一个布尔值，不值得为它引入依赖。
    """

    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - 配置损坏按未知处理
            return None
        section = data.get("feeding")
        if isinstance(section, dict) and isinstance(section.get("enabled"), bool):
            return bool(section["enabled"])
        # 非布尔（例如写成字符串 "false"）视为不可信，交给调用方按「开着」处理，
        # 不擅自猜制作者意图 —— 宿主也会按默认值把它纠正回来。
        return None

    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            in_section = line.replace(" ", "").lower() == "[feeding]"
            continue
        if in_section and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "enabled":
                token = value.strip().lower()
                if token in ("true", "false"):
                    return token == "true"
                return None
    return None


def _body_eat_visibility() -> tuple:
    """返回 (喂食是否开启, 判定依据)。读不到一律按开启算，行为与现状一致。

    为什么读文件而不是读 self.config：@Tool 的 visibility 在**类定义期**求值，
    那时还没有插件实例与配置对象。宿主首次安装会先生成 config.toml 再热重载，
    所以重载后能读到；故障场景（文件缺失 / 损坏 / 无该段）全部回退到「可见」。
    """

    try:
        path = Path(__file__).with_name("config.toml")
        if not path.is_file():
            return True, "无 config.toml"
        parsed = _parse_feeding_enabled(path.read_text(encoding="utf-8"))
        if parsed is None:
            return True, "config.toml 无 [feeding] 段或无法解析"
        return parsed, "config.toml 已读取"
    except Exception:  # noqa: BLE001 - 任何异常都按开启处理，不影响本体
        return True, "读取异常"


_FEEDING_ENABLED_AT_IMPORT, _BODY_EAT_VISIBILITY_REASON = _body_eat_visibility()
# 非 "visible" 即交给宿主按 deferred 处理（宿主只认字符串 "visible"）。
_BODY_EAT_VISIBILITY = "visible" if _FEEDING_ENABLED_AT_IMPORT else None


# 生理波形缓冲区的「超前起点」（秒）。
# 前端 webui/organs.html 用 WAVE_WINDOW_SEC=8 的窗口显示波形，屏幕右缘是
# 「现在」。所以后端生成的样本必须从「现在-8s」开始，前端才能按绝对时间
# 去索引数据池 —— 这样每次重新拉取时窗口接得上，不会跳帧。
# 缓冲区总长由前端传入的 seconds（20s）决定，覆盖 [现在-8s, 现在+12s]，
# 前端在 10s 时预取下一批，正好留 2s 余量。
_WAVE_LEAD_SEC = 8.0

# 体征展示定义：(字段, 中文名, 单位, 小数位, 正常区间, 偏低词, 偏高词)
VITALS = [
    ("hr", "心率", "bpm", 0, (60.0, 85.0), "偏慢", "偏快"),
    ("sbp", "收缩压", "mmHg", 0, (100.0, 140.0), "偏低", "偏高"),
    ("dbp", "舒张压", "mmHg", 0, (60.0, 90.0), "偏低", "偏高"),
    ("sao2", "血氧", "%", 1, (95.0, 100.0), "偏低", "正常"),
    ("glucose", "血糖", "mmol/L", 1, (3.9, 6.1), "偏低", "偏高"),
    ("temp", "体温", "℃", 2, (36.3, 37.2), "偏低", "偏高"),
    ("rr", "呼吸", "次/分", 0, (12.0, 20.0), "偏慢", "偏快"),
    ("map", "平均动脉压", "mmHg", 0, (70.0, 105.0), "偏低", "偏高"),
]

# (字段, 展示名)。展示名要保证「数值越高 = 该词越正向」，否则会出现
# 「饥饿：90（不饿）」这种标签与值打架的读法。饥饿/口渴两项因此改名为
# 饱腹/水分 —— 引擎内部字段不变，只是界面用词跟着数值方向走。
FEELINGS = [
    ("energy", "精力"), ("hunger", "饱腹"), ("thirst", "水分"),
    ("stress", "压力"), ("pain", "疼痛"), ("depression", "心境"),
    # 2026-11-18：补上 APK 有的正性情绪一极，否则六行里只有"心境"单向。
    ("euphoria", "欣快"), ("dysphoria", "烦躁"),
]

# 每个分组默认展示的「关键字段」（v1.3.1）。
#
# 背景：内分泌补齐后内脏页涨到 160 个字段 / 29 组，一屏全是数字 ——
# 用户的原话是「完善之后感觉刷屏了」。折叠不是藏信息：卡片默认只给每条轴
# 最该看的那 3~4 个量，其余点「展开其余 N 项」就能看到全量。
#
# 选择原则（不是随手挑的）：
#   · 有诊断意义的最终指标优先（心率、血压、血糖、血钙…），推导量与中间量靠后；
#   · 激素组给「轴的两端」——释放激素与效应激素各取代表，中间的转化物折叠；
#   · 受体组默认只给 2~3 个最常关注的，其余折叠（受体是查证用的，不是日常读的）。
# 未列在此处的分组：字段数 <= COLLAPSE_MIN 时全量展示，不出现展开按钮。
KEY_FIELDS = {
    "cardiovascular": ("hr", "sbp", "dbp", "map"),
    "respiratory": ("rr", "sao2", "paco2"),
    "metabolic": ("glucose", "insulin", "glycogen", "lactate"),
    "renal": ("gfr", "urine_total", "bladder", "na", "k", "osmol", "ph"),
    "digestive": ("gastric_volume", "chyme_carb", "gut_glucose", "duodenum_volume", "bile"),
    "central": ("crh", "acth", "tsh", "fsh", "lh", "adh"),
    "thyroid": ("t4", "t3", "calcitonin"),
    "parathyroid": ("pth", "ca"),
    "adrenal": ("cortisol", "aldosterone", "adrenaline", "norepinephrine"),
    "pancreas": ("glucagon", "amylin", "somatostatin"),
    "gut_hormones": ("gastrin", "cck", "gip", "pyy", "ghrelin"),
    "kidney_endocrine": ("renin", "ang2", "calcitriol"),
    "heart": ("anp",),
    "gonads": ("estrogen", "estradiol", "progesterone", "testosterone"),
    "pineal_skin": ("melatonin", "vitd25"),
    "vascular": ("prostacyclin", "thromboxane"),
    "neural": ("dopamine", "serotonin", "adenosine"),
    "receptors_central": ("r_crh1", "r_gnrhr", "r_melatonin"),
    "receptors_thyroid": ("r_tshr", "r_tr", "r_pthr", "r_vdr"),
    "receptors_adrenal": ("r_gcr", "r_mr", "r_alpha1", "r_beta1", "r_m2"),
    "receptors_pancreas": ("r_insulin", "r_glucagon"),
    "receptors_gut": ("r_cck1", "r_h2", "r_glp1"),
    "receptors_kidney": ("r_at1", "r_avpr2", "r_anp"),
    "receptors_gonads": ("r_er", "r_pr"),
    "receptors_neural": ("r_d1", "r_d2", "r_5ht1a", "r_a1"),
    "receptors_vascular": ("r_ip", "r_tp"),
    "thermo": ("temp", "sweat", "shiver", "thermal"),
    "drugs": ("bac", "caffeine"),
    "behavior": ("hour", "circ", "meals", "health"),
}

# 字段数不超过这个值的分组不做折叠（全部直接展示）。
COLLAPSE_MIN = 4

# 内脏细则分组：(组名, 副标题, [(字段, 中文名, 单位, 小数位)])
ORGAN_GROUPS = [
    ("cardiovascular", "心血管", "泵血与压力", [
        ("hr", "心率", "bpm", 0), ("sv", "每搏输出量", "mL", 1), ("co", "心输出量", "L/min", 2),
        ("svr", "体循环阻力", "dyn·s·cm⁻⁵", 0), ("map", "平均动脉压", "mmHg", 1),
        ("sbp", "收缩压", "mmHg", 1), ("dbp", "舒张压", "mmHg", 1), ("pp", "脉压", "mmHg", 1),
        ("blood_vol", "循环血量", "mL", 0),
    ]),
    ("respiratory", "呼吸", "气体交换", [
        ("rr", "呼吸频率", "次/分", 1), ("tidal", "潮气量", "mL", 0),
        ("sao2", "血氧饱和度", "%", 2), ("paco2", "动脉二氧化碳", "mmHg", 1),
    ]),
    ("metabolic", "代谢", "糖脂与能量", [
        ("glucose", "血糖", "mmol/L", 2), ("insulin", "胰岛素", "相对单位", 2),
        ("glycogen", "肝糖原", "g", 1), ("atp", "细胞能量", "0-1", 3),
        ("lactate", "血乳酸", "mmol/L", 2), ("fat_reserve", "待氧化脂肪", "g", 1),
        ("na_intake_24h", "膳食钠吸收", "mmol/天", 1),
    ]),
    ("renal", "肾脏", "滤过与体液", [
        ("gfr", "肾小球滤过率", "mL/min", 1), ("urine_total", "累计尿量", "mL", 0),
        ("bladder", "膀胱充盈", "mL", 0), ("na", "血钠", "mmol/L", 2),
        ("k", "血钾", "mmol/L", 2), ("osmol", "血浆渗透压", "mOsm/kg", 1),
        ("ph", "血液 pH", "", 3), ("body_water", "总体水", "L", 2), ("na_mass", "可交换钠", "mmol", 0),
    ]),
    ("digestive", "消化道", "胃与肠吸收", [
        ("gastric_volume", "胃内容物", "mL", 1), ("chyme_carb", "待排空碳水", "g", 2),
        ("chyme_protein", "待排空蛋白", "g", 2), ("chyme_fat", "待排空脂肪", "g", 2),
        ("chyme_water", "胃内自由水", "mL", 0), ("chyme_na", "待吸收钠", "mmol", 1),
        ("gut_glucose", "肠内待入血葡萄糖", "mmol", 2), ("gut_amino", "肠内待入血氨基酸", "g", 2),
        ("duodenum_volume", "十二指肠内容物", "mL", 1),
        ("jejunum_volume", "空肠内容物", "mL", 1),
        ("ileum_residue", "回肠残余", "g", 2),
        ("bile", "胆汁", "0-1", 2),
    ]),
    # ------------------------------------------------------------------
    # 内分泌（v1.3.1 起按器官分科）
    # ------------------------------------------------------------------
    # 旧版这里只有一个 "内分泌 · 激素轴" 分组，9 个字段。v1.3.1 把 APK
    # HormoneType 枚举补齐后字段涨到 50 余个，再塞一组就没法读了。
    # 分组口径与 body/endocrine/organs/*.py 一一对应：一个器官一个分组，
    # 便于「哪条激素不对 -> 直接去改哪个文件」。
    ("central", "下丘脑 · 垂体", "释放激素与促激素", [
        ("crh", "CRH", "", 3), ("trh", "TRH", "", 3), ("gnrh", "GnRH", "", 3),
        ("acth", "ACTH", "", 3), ("tsh", "TSH", "", 3),
        ("fsh", "FSH", "", 3), ("lh", "LH", "", 3),
        ("prolactin", "催乳素", "", 3), ("oxytocin", "催产素", "", 3),
        ("adh", "抗利尿激素", "", 3),
    ]),
    ("thyroid", "甲状腺", "T4/T3 与降钙素", [
        ("t4", "T4 甲状腺素", "", 3), ("t3", "T3", "", 3),
        ("calcitonin", "降钙素", "", 3),
    ]),
    ("parathyroid", "甲状旁腺 · 钙磷", "PTH 与血钙", [
        ("pth", "PTH", "", 3), ("ca", "血钙", "mmol/L", 3),
    ]),
    ("adrenal", "肾上腺", "皮质与髓质", [
        ("cortisol", "皮质醇", "", 3), ("aldosterone", "醛固酮", "", 3),
        ("dhea", "DHEA", "", 3), ("androstenedione", "雄烯二酮", "", 3),
        ("adrenaline", "肾上腺素", "", 3), ("norepinephrine", "去甲肾上腺素", "", 3),
    ]),
    ("pancreas", "胰岛", "糖代谢的内分泌侧", [
        # 胰岛素不重复列出：它已经在上面的「代谢」组显示（糖代谢闭环的一部分）。
        # 同一字段在两个分组各显示一次，用户会以为是两个不同的量。
        ("glucagon", "胰高血糖素", "", 3),
        ("amylin", "淀粉素", "", 3), ("somatostatin", "生长抑素", "", 3),
    ]),
    # 这 7 条一直算在 engine._digest 里；ORGAN_GROUPS 是硬编码表，
    # 不列出来 WebUI 就看不到（2026-11-11 用户反馈「看不到小肠腺体的激素」即此）。
    ("gut_hormones", "胃肠腺体", "胃与小肠分泌", [
        ("gastrin", "胃泌素", "G 细胞·蛋白", 3),
        ("secretin", "促胰液素", "S 细胞·酸", 3),
        ("cck", "缩胆囊素", "I 细胞·脂肪", 3),
        ("gip", "抑胃肽 GIP", "K 细胞·碳水", 3),
        ("pyy", "酪酪肽 PYY", "回肠·饱腹刹车", 3),
        ("glp1", "胰高血糖素样肽-1", "回肠·饱腹", 3),
        ("ghrelin", "胃饥饿素", "胃底·促食欲", 3),
        ("neurotensin", "神经降压素", "回肠·脂肪", 3),
        ("histamine", "组胺", "ECL·胃泌素驱动", 3),
    ]),
    ("kidney_endocrine", "肾脏内分泌", "RAAS 与活性维生素 D", [
        ("renin", "肾素", "", 3), ("angiotensinogen", "血管紧张素原", "", 3),
        ("ang1", "血管紧张素 I", "", 3), ("ang2", "血管紧张素 II", "", 3),
        ("calcitriol", "活性维生素 D", "", 3),
    ]),
    ("heart", "心脏内分泌", "心房钠尿肽", [
        ("anp", "ANP", "", 3),
    ]),
    ("gonads", "生殖腺", "性激素组分", [
        ("estrogen", "雌激素（总）", "", 3), ("estradiol", "雌二醇 E2", "", 3),
        ("estrone", "雌酮 E1", "", 3), ("estriol", "雌三醇 E3", "", 3),
        ("progesterone", "孕激素", "", 3), ("testosterone", "睾酮", "", 3),
        ("androstadienone", "雄甾二烯酮", "", 3),
    ]),
    ("pineal_skin", "松果体 · 皮肤", "褪黑素与维生素 D 前体", [
        ("melatonin", "褪黑素", "", 3), ("vitd25", "25-羟基维生素 D", "", 3),
    ]),
    ("vascular", "血管内皮 · 血小板", "局部脂类介质", [
        ("prostacyclin", "前列环素 PGI2", "", 3), ("thromboxane", "血栓素 A2", "", 3),
        ("bradykinin", "缓激肽", "", 3), ("leukotriene", "半胱氨酰白三烯", "", 3),
    ]),
    ("neural", "神经胺类与递质", "中枢与自主神经", [
        ("dopamine", "多巴胺", "", 3), ("serotonin", "5-羟色胺", "", 3),
        ("adenosine", "腺苷", "", 3), ("acetylcholine", "乙酰胆碱", "", 3),
        ("anandamide", "内源性大麻素", "", 3),
    ]),
    # ---- 受体占据（0-1），同样按器官分组 ----
    ("receptors_central", "受体 · 中枢与垂体", "0-1", [
        ("r_crh1", "CRH1", "", 3), ("r_trhr", "TRHR", "", 3),
        ("r_gnrhr", "GnRHR", "", 3), ("r_melatonin", "褪黑素受体", "", 3),
    ]),
    ("receptors_thyroid", "受体 · 甲状腺与钙磷", "0-1", [
        ("r_tshr", "TSHR", "", 3), ("r_tr", "甲状腺激素核受体", "", 3),
        ("r_pthr", "PTHR", "", 3), ("r_vdr", "维生素 D 受体", "", 3),
    ]),
    ("receptors_adrenal", "受体 · 肾上腺与自主神经", "0-1", [
        ("r_gcr", "糖皮质激素受体", "", 3), ("r_mr", "盐皮质激素受体", "", 3),
        ("r_acthr", "ACTH 受体", "", 3),
        ("r_alpha1", "血管 α1", "", 3), ("r_alpha2", "α2 自身受体", "", 3),
        ("r_beta1", "心脏 β1", "", 3), ("r_beta2", "β2", "", 3),
        ("r_m2", "迷走 M2", "", 3),
    ]),
    ("receptors_pancreas", "受体 · 胰岛", "0-1", [
        # v1.3.1：r_insulin 不再是常数。v1.3.0 里全引擎从不更新它（恒 0.30），
        # 因此 v1.2.5 把它从本表移除；本版由登记表按 Hill 真实计算，故回归。
        ("r_insulin", "胰岛素受体", "", 3), ("r_glucagon", "胰高血糖素受体", "", 3),
        ("r_ss", "生长抑素受体", "", 3),
    ]),
    ("receptors_gut", "受体 · 胃肠", "0-1", [
        ("r_cck1", "CCK1", "", 3), ("r_cck2", "CCK2", "", 3),
        ("r_secretin", "促胰液素受体", "", 3), ("r_glp1", "GLP-1 受体", "", 3),
        ("r_h1", "组胺 H1", "", 3), ("r_h2", "组胺 H2", "", 3),
        ("r_npy", "NPY 受体", "", 3),
    ]),
    ("receptors_kidney", "受体 · 肾脏与容量", "0-1", [
        ("r_at1", "血管紧张素 AT1", "", 3), ("r_avpr2", "加压素 V2", "", 3),
        ("r_anp", "ANP 受体", "", 3),
    ]),
    ("receptors_gonads", "受体 · 生殖腺", "0-1", [
        ("r_er", "雌激素受体", "", 3), ("r_pr", "孕激素受体", "", 3),
        ("r_lhr", "LH 受体", "", 3), ("r_fshr", "FSH 受体", "", 3),
    ]),
    ("receptors_neural", "受体 · 神经与认知", "0-1", [
        ("r_d1", "多巴胺 D1", "", 3), ("r_d2", "多巴胺 D2", "", 3),
        ("r_5ht1a", "5-HT1A", "", 3), ("r_5ht2a", "5-HT2A", "", 3),
        ("r_5ht2c", "5-HT2C", "", 3),
        ("r_a1", "腺苷 A1", "", 3), ("r_a2", "腺苷 A2", "", 3),
        ("r_cb1", "大麻素 CB1", "", 3), ("r_cb2", "大麻素 CB2", "", 3),
        ("r_gaba", "GABA A", "", 3), ("r_nmda", "NMDA", "", 3),
    ]),
    ("receptors_vascular", "受体 · 内皮与血小板", "0-1", [
        ("r_ip", "前列环素 IP", "", 3), ("r_tp", "血栓素 TP", "", 3),
        ("r_b1", "缓激肽 B1", "", 3), ("r_b2", "缓激肽 B2", "", 3),
        ("r_cylslt1", "CysLT1", "", 3), ("r_cylslt2", "CysLT2", "", 3),
    ]),
    ("thermo", "体温调节", "", [
        ("temp", "核心体温", "℃", 2), ("sweat", "出汗", "0-1", 3),
        ("shiver", "寒战", "0-1", 3), ("thermal", "冷热体感", "50=舒适", 1),
    ]),
    ("drugs", "嗜好品", "乙醇与咖啡因", [
        ("bac", "血乙醇浓度", "g/L", 3), ("caffeine", "血浆咖啡因", "mg", 1),
    ]),
    ("behavior", "行为与节律", "", [
        ("hour", "仿真时刻", "时", 2), ("circ", "昼夜相位", "0-1", 3),
        ("meals", "累计进食", "次", 0), ("drinks", "累计饮水", "次", 0),
        ("urinations", "累计排尿", "次", 0), ("health", "健康度", "0-100", 1),
    ]),
]

# webui_history 返回的序列清单。顺序即图例顺序，增删要同步前端与 body/history.py
# 的预算推算（每点 1 个元素，序列越多能装的点越少，自动换算）。
HISTORY_SERIES_KEYS = (
    "hr", "sbp", "dbp", "glucose", "temp", "energy", "hunger", "thirst",
    "gastric", "bac", "na",
    # v1.2.5：情绪序列（供 WebUI 情绪曲线）
    "stress", "depression", "euphoria", "dysphoria", "mood_valence", "mood_tone",
    # v1.3.1：睡眠深度（0=清醒 1=深睡），与 stages 配合画夜间分期图
    "sleep_depth",
)


def _r(value: Any, digits: int) -> float:
    """安全取整，非数值一律回落 0。"""

    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _clock(hour: float) -> str:
    """把 0-24 的小时数格式化成 HH:MM。"""

    total = int(round(max(0.0, min(23.999, float(hour))) * 60.0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _tone(key: str, value: float) -> str:
    """按正常区间给体征配色档位。"""

    for field, _label, _unit, _digits, span, _low, _high in VITALS:
        if field != key:
            continue
        lo, hi = span
        if value < lo:
            return "warn" if value >= lo * 0.85 else "crit"
        if value > hi:
            return "warn" if value <= hi * 1.15 else "crit"
        return "good"
    return "good"


FEELING_HIGH_BAD = {"hunger": (40.0, 70.0), "thirst": (40.0, 70.0), "stress": (40.0, 70.0),
                     "pain": (10.0, 40.0), "depression": (25.0, 55.0)}


def _feeling_tone(key: str, value: float) -> str:
    """体感配色：精力越高越好，其余（饥饿/口渴/压力/疼痛/心境）越高越糟。"""

    if key == "energy":
        if value >= 60.0:
            return "good"
        return "warn" if value >= 35.0 else "crit"
    warn_at, crit_at = FEELING_HIGH_BAD.get(key, (70.0, 90.0))
    if value >= crit_at:
        return "crit"
    if value >= warn_at:
        return "warn"
    return "good"


def _vital_text(key: str, value: float) -> str:
    """体征的中文判读，优先用引擎自带的分档词。"""

    table = FEELING_LEVELS.get(key)
    if table:
        for upper, word in table:
            if value < upper:
                return word
        return table[-1][1]
    return ""


class LifeBodyPlugin(MaiBotPlugin):
    """麦麦身体插件。"""

    config_model = LifeBodyConfig

    def __init__(self) -> None:
        """初始化运行期容器。"""

        super().__init__()
        self._engine: BodyEngine | None = None
        self._bus: EventBus | None = None
        self._task: asyncio.Task | None = None
        self._samples: deque[dict[str, Any]] = deque(maxlen=2200)
        self._events: deque[dict[str, Any]] = deque(maxlen=800)
        # 跨插件写接口的 request_id 幂等窗口（防重复记账）
        self._drink_seen: deque[str] = deque(maxlen=256)
        self._meal_seen: deque[str] = deque(maxlen=256)
        self._last_step_wall = 0.0
        # 情绪自评（v1.2.5）：上次判定的墙钟时刻，用于防抖
        self._emotion_last_at = 0.0
        self._emotion_hits = 0
        self._last_save_wall = 0.0
        self._started_at = 0.0
        self._debt = 0.0
        self._seed = 20261106
        self._ready = False
        self._step_error = ""
        self._day_base: tuple[int, int, int, int, float] | None = None
        self._last_silence_note = -1.0e9
        self._shared_visit = 0
        self._share_armed = True
        self._link: LifeLink | None = None
        self._link_task: asyncio.Task | None = None
        self._weather_task: asyncio.Task | None = None
        # 联动进餐的去重键。必须存在插件自己身上、不能只存在 LifeLink 里：
        # on_load 的顺序是 _restore() 早于 LifeLink 构造，那时 self._link 还是
        # None，往 self._link 上恢复键值会静默失败，于是每次重启当天已经吃过
        # 的饭窗都会被重新触发一遍（2026-11-09 复现：08:00/08:31/09:02 连吃三份）。
        self._link_meal_key: str = ""
        # v1.4.0：是否把生活习惯写进提示词（配置 habits.inject_prompt）
        self._habits_inject = True

    @staticmethod
    def _history_limit(value: Any) -> int:
        """把历史容量归一化为可用于 deque 的正整数。"""
        try:
            limit = int(value)
        except (TypeError, ValueError, OverflowError):
            return 1
        return max(1, limit)

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """启动仿真。"""

        cfg = self.config
        self._migrate_link_provider(cfg)
        self._seed = int(cfg.sim.seed)
        self._engine = BodyEngine(enable_behaviour=True, **self._rescue_kwargs())
        self._engine.rng.seed(self._seed) if hasattr(self._engine, "rng") else None
        self._bus = self._engine.bus if getattr(self._engine, "bus", None) is not None else EventBus()
        self._engine.bus = self._bus
        self._samples = deque(maxlen=self._history_limit(cfg.sim.history_limit))
        self._events = deque(maxlen=800)
        self._bus.subscribe(self._on_event)
        self._started_at = time.time()

        # LifeLink 必须早于 _restore() 构造：_restore 要走联动侧的恢复钩子
        # （联动进餐去重键等）。旧顺序是先 _restore 再建 link，导致
        # "self._link is not None" 恒假，所有联动侧状态每次都恢复失败。
        self._link = LifeLink(self.ctx, cfg.link)

        # 启动时必须立刻按配置决定"三餐交不交给麦生活日程"（引擎的 _meals_external）。
        # 旧实现只在 on_config_update 里调 _sync_link_mode，on_load 从不调用，
        # 于是引擎一直停在默认 _meals_external=False：内置 7.5/12.5/18.5 饭点照跑，
        # 与日程的 08:00/12:00/18:00 各吃一遍（服务器实测每顿都成对出现）。
        # 必须早于 _catch_up()：补算离线那段时间也要按新口径走，否则补算时会凭空多吃几顿。
        self._sync_link_mode()

        await self._restore()
        self._catch_up()
        # 补算完再按系统钟对齐：补算只负责让生理量按真实离线时长演进，
        # 钟点最终必须钉在真实时间上（否则离线多久，钟就偏多久）。
        self._sync_wall_clock()
        if self._day_base is None:
            # 新身体从第 1 天零点起；老存档缺基准时同样归零，最多漏记当天已发生的几次行为
            self._day_base = (self._body_sim_day(), 0, 0, 0, 0.0)
        self._seed_samples()

        self._last_step_wall = time.time()
        self._last_save_wall = time.time()
        self._task = asyncio.create_task(self._ticker())
        if cfg.link.enabled:
            self._link_task = asyncio.create_task(self._linker())
        # 天气独立于联动：联动关了天气也要照常更新（外面冷不冷跟麦生活无关）
        if bool(getattr(cfg.link, "weather_enabled", True)):
            self._weather_task = asyncio.create_task(self._weather_loop())
        self._ready = True
        # 食物条数写进启动日志：食物库扩展是「静默装配」的，没有这行
        # 就只能靠面板去数，出问题时（扩展没装上/版本错）看不出来。
        self._get_logger().info(
            "麦麦身体已启动：仿真第 %.1f 天 %s，心率 %.0f，食物 %d 种%s",
            self._body_sim_day(), _clock(self._s.hour), self._s.hr,
            len(FOODS), "" if _FOOD_EXTRA_ADDED > 0 else "（扩展未装）",
        )
        # 喂食关闭时 body_eat 不在常驻工具表里（PR #8）。不写这一行，
        # 用户只会看到"工具没了"，却不知道为什么。
        if not _FEEDING_ENABLED_AT_IMPORT:
            self._get_logger().info(
                "body_eat 不进常驻工具表：聊天喂食已关闭（判定依据：%s）。"
                "需要它在场时，打开「允许聊天喂食」并重载插件。",
                _BODY_EAT_VISIBILITY_REASON,
            )

    async def on_unload(self) -> None:
        """停止仿真并落盘。"""

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._task = None
        if self._link_task is not None:
            self._link_task.cancel()
            try:
                await self._link_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._link_task = None
        if self._weather_task is not None:
            self._weather_task.cancel()
            try:
                await self._weather_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载期不抛
                pass
            self._weather_task = None
        await self._save()
        self._ready = False

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载：只调整节奏与容量，不重置身体。"""

        del scope, config_data, version
        self._migrate_link_provider(self.config)
        history_limit = self._history_limit(self.config.sim.history_limit)
        if self._samples.maxlen != history_limit:
            self._samples = deque(self._samples, maxlen=history_limit)
        if self._engine is not None:
            for key, value in self._rescue_kwargs().items():
                setattr(self._engine, key, value)

        # 联动总开关热切换：开则起任务，关则停任务并把注入值归零。
        self._sync_link_mode()

    # ---- v1.3.2：provider 默认值迁移 ----
    @staticmethod
    def _ver_tuple(text: str) -> tuple:
        """把 "1.3.1"/"1.3.1-dev" 这类版本串压成 (1,3,1)，便于比较。"""
        parts: list[int] = []
        for chunk in str(text).replace("-", ".").split(".")[:3]:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)

    def _migrate_link_provider(self, cfg: Any) -> None:
        """把旧配置里的 provider="life" 当作「出厂默认」，在内存里按 auto 处理。

        背景：v1.3.1 及更早的 provider 默认值是 life；v1.3.2 改成 auto（自动识别
        麦生活 / 麦织）。老用户的 config.toml 里写着 `provider = "life"` —— 那多半
        不是人选，而是当年生成的默认值。照字面执行的话，「装哪个都能自动认出来」
        对这些升级过来的用户永远不生效。

        处理：只在 config_version 低于 1.3.2、且 provider 恰为 life 时，于**内存**
        改成 auto（不写回 config.toml，不动用户文件）；显式选过 weave 的保持原样。
        """
        try:
            ver = str(getattr(getattr(cfg, "plugin", None), "config_version", "") or "")
            raw = str(getattr(getattr(cfg, "link", None), "provider", "") or "").strip().lower()
            if raw not in ("life", "麦生活"):
                return
            if not ver or self._ver_tuple(ver) >= (1, 3, 2):
                return
            cfg.link.provider = "auto"
            self._get_logger().info(
                "联动引擎按 auto 处理：旧配置（%s）的 provider=life 视为出厂默认，未改写文件", ver)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不能拖垮加载
            self._get_logger().debug("provider 迁移跳过：%s", exc)

    def _sync_link_mode(self) -> None:
        """按当前配置决定联动任务是否运行、引擎是否接受外部作息。"""

        if self._engine is None:
            return
        want = bool(self.config.link.enabled)
        self._engine.link_enabled = want
        self._engine._meals_external = bool(
            want and self.config.link.sync_meal
        )

        # 月经周期配置注入（默认关闭）
        cycle_cfg = getattr(self.config, "cycle", None)
        if cycle_cfg is not None:
            self._engine.cycle_enabled = bool(getattr(cycle_cfg, "enabled", False))
            self._engine.cycle_length_days = max(
                18.0, min(45.0, float(getattr(cycle_cfg, "cycle_length", 28.0)))
            )
            self._engine.cycle_pain_enabled = bool(
                getattr(cycle_cfg, "pain_enabled", True)
            )
            self._engine.MENSTRUAL_LENGTH = max(
                1.0, min(10.0, float(getattr(cycle_cfg, "period_length", 5.0)))
            )
            # 现实日历锚点：填了「上次月经第一天」就按日历算周期第几天，
            # 留空/写错/写未来都退回「从启用那刻累加」的旧行为。
            self._engine.set_cycle_anchor(
                self._parse_cycle_anchor(getattr(cycle_cfg, "last_period_date", ""))
            )

        # 运动配置注入
        ex_cfg = getattr(self.config, "exercise", None)
        # 年龄目前是引擎内置常量（18 岁），不暴露给使用者 —— 见
        # docs/issues/issue_年龄无法改变心率上下限.md
        if ex_cfg is not None:
            self._engine.exercise_enabled = bool(getattr(ex_cfg, "enabled", True))
            self._engine.exercise_max_minutes = max(
                5.0, float(getattr(ex_cfg, "max_minutes", 45.0)))
            self._engine.exercise_interval_minutes = max(
                3.0, float(getattr(ex_cfg, "interval_minutes", 15.0)))
            self._engine.exercise_min_energy = max(
                10.0, min(95.0, float(getattr(ex_cfg, "min_energy", 55.0))))
            self._engine.exercise_min_sleep_hours = max(
                0.0, float(getattr(ex_cfg, "min_sleep_hours", 6.0)))

        # 睡眠配置注入。联动开启时，唤醒与拦截都以麦生活的日程为准，
        # 这两个开关自动失效（配置里也写明了会失效）。
        sleep_cfg = getattr(self.config, "sleep", None)
        if sleep_cfg is not None:
            self._engine.sleep_start_hour = float(getattr(sleep_cfg, "start_hour", 23.0))
            self._engine.sleep_end_hour = float(getattr(sleep_cfg, "end_hour", 7.0))
            self._engine.wake_hold_minutes = max(
                0.0, float(getattr(sleep_cfg, "wake_hold_minutes", 10.0))
            )
            self._engine.wake_on_message = bool(
                getattr(sleep_cfg, "wake_on_message", True)
            ) and not want
            # 强制睡眠：连续清醒过久会睡过去。设 0 表示关闭。
            _fsa = float(getattr(sleep_cfg, "force_sleep_after_hours", 20.0))
            self._engine.force_sleep_after_hours = _fsa if _fsa > 0.0 else 9999.0
            self._engine.force_sleep_hours = max(
                1.0, float(getattr(sleep_cfg, "force_sleep_hours", 6.0)))
            self._engine.force_sleep_wakeable = bool(
                getattr(sleep_cfg, "force_sleep_wakeable", True))
        # ---- v1.4.0 四套新系统配置注入 ----
        b_cfg = getattr(self.config, "blood", None)
        if b_cfg is not None:
            self._engine.blood_enabled = bool(getattr(b_cfg, "enabled", True))
            self._engine.bleed_accident_per_day = max(
                0.0, float(getattr(b_cfg, "accident_per_day", 0.03)))
            self._engine.bleed_accident_max_ml = max(
                0.0, float(getattr(b_cfg, "accident_max_ml", 350.0)))
            self._engine.menstrual_gain = max(
                0.0, float(getattr(b_cfg, "menstrual_gain", 1.0)))
        i_cfg = getattr(self.config, "immune", None)
        if i_cfg is not None:
            self._engine.immune_enabled = bool(getattr(i_cfg, "enabled", True))
            self._engine.infection_per_day = max(
                0.0, float(getattr(i_cfg, "infection_per_day", 0.012)))
            self._engine.fever_gain = max(0.0, float(getattr(i_cfg, "fever_gain", 1.0)))
            self._engine.immune_sensitivity = max(
                0.0, float(getattr(i_cfg, "immune_sensitivity", 1.0)))
        n_cfg = getattr(self.config, "nutrition", None)
        if n_cfg is not None:
            self._engine.nutrition_enabled = bool(getattr(n_cfg, "enabled", True))
            self._engine.height_cm = max(120.0, min(220.0, float(getattr(n_cfg, "height_cm", 162.0))))
            self._engine.reference_weight_kg = max(
                30.0, min(120.0, float(getattr(n_cfg, "reference_weight_kg", 55.0))))
        h_cfg = getattr(self.config, "habits", None)
        if h_cfg is not None:
            self._engine.habits_enabled = bool(getattr(h_cfg, "enabled", True))
            self._engine.relax_stress_max = max(
                0.0, min(90.0, float(getattr(h_cfg, "relax_stress_max", 35.0))))
            self._engine.aerobic_week_min = max(
                0.0, float(getattr(h_cfg, "aerobic_week_min", 150.0)))
            self._engine.strength_week_sessions = max(
                0.0, float(getattr(h_cfg, "strength_week_sessions", 2)))
            self._habits_inject = bool(getattr(h_cfg, "inject_prompt", True))
        if ex_cfg is not None:
            self._engine.auto_exercise_per_week = max(
                0.0, float(getattr(ex_cfg, "auto_per_week", 4.0)))

        if not want:
            # 关掉联动时立刻回到中性（-1 = 无外部意见），避免残留值继续压低心率。
            # 注意这里不能写 0.0：0.0 是"外部明确说清醒"，只在联动开着时才有意义。
            self._engine.link_sleep = -1.0
            self._engine.link_activity = 0.0
        if want and self._link_task is None and self._ready:
            self._link_task = asyncio.create_task(self._linker())
        elif not want and self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None

    def _rescue_kwargs(self) -> dict[str, Any]:
        """把急救与排泄配置翻译成 BodyEngine 的构造/热更参数。"""

        cfg = self.config.rescue
        # 老配置文件里没有 [excretion] 段时，config.excretion 仍由默认值填充，
        # 所以这里不需要额外兜底（PluginConfigBase 会实例化默认子配置）。
        exc = getattr(self.config, "excretion", None)
        # 2026-11-17：threshold 必须小于 restore_health。写反了会让急救在第一次
        # 出院后**永久失效** —— 健康停在 restore_health，永远爬不回阈值以上，
        # 引擎的 _rescue_armed 就再也上不了膛（见 engine._rescue）。这里直接钳制，
        # 并在日志里说明，避免配置错了却毫无提示。
        _thr = max(0.0, min(99.0, float(cfg.threshold)))
        _restore = max(0.0, min(100.0, float(cfg.restore_health)))
        if _restore <= _thr:
            _fixed = min(100.0, _thr + 1.0)
            try:
                self._get_logger().warning(
                    "rescue.restore_health（%.0f）不高于 threshold（%.0f），已按 %.0f 处理；"
                    "否则首次出院后急救不会再触发", _restore, _thr, _fixed)
            except Exception:  # noqa: BLE001 - 日志失败不影响配置生效
                pass
            _restore = _fixed

        return {
            "rescue_enabled": bool(cfg.enabled),
            "rescue_threshold": _thr,
            "rescue_critical_seconds": max(0.0, float(cfg.critical_seconds)),
            "rescue_stay_hours": max(0.0, float(cfg.stay_hours)),
            "rescue_restore_health": _restore,
            "enable_void": bool(getattr(exc, "show_urination", True)) if exc else True,
            "enable_stool": bool(getattr(exc, "show_stool", True)) if exc else True,
        }


    def _last_night_view(self, nights: Any) -> dict[str, Any]:
        """把最近一夜的聚合结果整理成前端可直接用的形状。

        2026-11-26 新增。空的/异常的 nights 一律返回 {}，让前端各字段按
        "—" 显示，不在前端做 None 判断。
        """
        if not isinstance(nights, (list, tuple)) or not nights:
            return {}
        night = nights[-1]
        if not isinstance(night, dict):
            return {}
        try:
            return {
                # 起止时刻（仿真秒）：前端用它把夜间分期图裁到这一夜的区间，
                # 否则一天里的午睡会被当成"最近一段睡眠"画进图里。
                "start_t": round(float(night.get("start_t", 0.0) or 0.0), 1),
                "end_t": round(float(night.get("end_t", 0.0) or 0.0), 1),
                "hours": round(float(night.get("hours", 0.0) or 0.0), 2),
                "bout_h": round(float(night.get("bout_h", 0.0) or 0.0), 2),
                "quality": round(float(night.get("quality", 0.0) or 0.0), 0),
                "efficiency": round(float(night.get("efficiency", 0.0) or 0.0), 3),
                "light_h": round(float(night.get("light_h", 0.0) or 0.0), 2),
                "deep_h": round(float(night.get("deep_h", 0.0) or 0.0), 2),
                "rem_h": round(float(night.get("rem_h", 0.0) or 0.0), 2),
                "wakes": int(night.get("wakes", 0) or 0),
                "bouts": int(night.get("bouts", 0) or 0),
                "cycles": int(night.get("cycles", 0) or 0),
            }
        except (TypeError, ValueError):
            return {}


    def _sleep_view(self) -> dict[str, Any]:
        """睡眠面板：四态分期、整夜结构、质量评分、睡眠债、近 7 日汇总。

        2026-11-16 起「时长」按连续睡眠区间（bout）口径给出：
          total_h   —— 当前这一觉（或最近结束的一觉）的实际睡眠时长，
                       不含中途清醒；短于 min_sleep_bout_hours 的午睡
                       不会被写成"昨晚睡了 X 小时"。
          week_*    —— 近 7 日汇总（PSQI 式回顾窗口），用户要求的
                       "能看近一周的睡眠质量"就是这四个字段。
        """

        s = self._s
        stage_zh = {"awake": "清醒", "light": "浅睡", "deep": "深睡", "rem": "快速眼动"}
        # 时长口径：优先用当前 bout 的账本（睡到一半也能看到真实累计），
        # bout 未开始（刚睡完已结算）则回落到最近一次合格 bout 的时长。
        bout_light = float(getattr(s, "sleep_bout_light_sec", 0.0) or 0.0)
        bout_deep = float(getattr(s, "sleep_bout_deep_sec", 0.0) or 0.0)
        bout_rem = float(getattr(s, "sleep_bout_rem_sec", 0.0) or 0.0)
        bout_total = bout_light + bout_deep + bout_rem
        if bout_total > 0.0:
            total = bout_total
            light_s, deep_s, rem_s = bout_light, bout_deep, bout_rem
        else:
            # 2026-11-27：账本为 0 时回落到「最近一次合格 bout」的快照。
            #
            # 此前回落到 dur_light/dur_deep/dur_rem，而这三个累计量只在
            # 「新一段 bout 入睡」时清零，午睡分支从不碰它 —— 于是午睡结束
            # 后面板把午睡时长当成这一觉的结构显示。服务器实测：dur_light=1560s、
            # dur_deep=690s，面板显示 0.4h / 0.2h，正是 13:38-14:49 那场午睡。
            # 快照只在合格 bout 结算时写入，午睡永远进不来。
            snap = getattr(s, "sleep_last_struct", None)
            snap_total = 0.0
            if isinstance(snap, dict):
                snap_total = (float(snap.get("light_s", 0.0) or 0.0)
                              + float(snap.get("deep_s", 0.0) or 0.0)
                              + float(snap.get("rem_s", 0.0) or 0.0))
            if snap_total > 0.0:
                light_s = float(snap.get("light_s", 0.0) or 0.0)
                deep_s = float(snap.get("deep_s", 0.0) or 0.0)
                rem_s = float(snap.get("rem_s", 0.0) or 0.0)
                total = snap_total
            else:
                # 连快照都没有（全新身体 / 极老存档）才退回 dur_*。
                total = s.dur_light + s.dur_deep + s.dur_rem
                light_s, deep_s, rem_s = s.dur_light, s.dur_deep, s.dur_rem
        hours = total / H2S

        def pct(x: float) -> float:
            return round(100.0 * x / total, 1) if total > 0 else 0.0

        bouts = list(getattr(s, "sleep_bouts", None) or [])
        return {
            "stage": s.sleep_stage,
            "stage_text": stage_zh.get(s.sleep_stage, s.sleep_stage),
            "depth": _r(s.sleep_depth, 2),
            "sleeping": bool(s.sleeping),
            "cycle_index": int(s.stage_index),
            "cycle_pos": _r(s.cycle_pos, 2),
            "debt_h": _r(s.sleep_debt, 2),
            "quality": _r(s.sleep_quality, 0),
            "total_h": _r(hours, 2),
            "light_h": _r(light_s / H2S, 2),
            "deep_h": _r(deep_s / H2S, 2),
            "rem_h": _r(rem_s / H2S, 2),
            "light_pct": pct(light_s),
            "deep_pct": pct(deep_s),
            "rem_pct": pct(rem_s),
            # ---- 连续睡眠区间口径（2026-11-16）----
            "bout_active": bool(float(getattr(s, "sleep_bout_start_t", 0.0) or 0.0) > 0.0),
            "last_sleep_hours": _r(getattr(s, "last_sleep_hours", 0.0), 2),
            "counts_as_night": bool(
                hours >= float(getattr(self._engine, "min_sleep_bout_hours", 4.0) or 0.0)
            ) if hours > 0 else False,
            "min_bout_hours": _r(getattr(self._engine, "min_sleep_bout_hours", 4.0), 1),
            # ---- 近 7 日汇总（7 日滑动聚合，不是 PSQI：PSQI 官方窗口是 1 个月）----
            "week_nights": int(getattr(s, "sleep_week_nights", 0) or 0),
            "week_hours": _r(getattr(s, "sleep_week_hours", 0.0), 2),
            # 2026-11-26：「最近一夜」的聚合视图 —— 同一夜的多段 bout 已合账，
            # 用户要看的是「这一整夜」，不是被中途醒来切开的碎片。
            "last_night": self._last_night_view(getattr(s, "sleep_nights", None)),
            "week_quality": _r(getattr(s, "sleep_week_quality", 0.0), 0),
            "week_efficiency": _r(getattr(s, "sleep_week_efficiency", 0.0), 3),
            "week_skipped": int(getattr(s, "sleep_week_skipped", 0) or 0),
            "bouts": bouts[-7:],
            # 夜间唤醒统计（2026-11-14）
            "wake_count": int(s.wake_count),
            "wake_by_message": int(s.wake_by_message),
            "wake_by_thirst": int(s.wake_by_thirst),
            "wake_by_bladder": int(s.wake_by_bladder),
            "wake_pending": bool(s.wake_pending),
            "last_wake_reason": str(s.last_wake_reason or ""),
            "awake_h": _r(s.dur_awake / H2S, 2),
            # 强制睡眠（连续清醒过久的微睡眠）
            "forced_sleep": bool(getattr(s, "forced_sleep", False)),
            "microsleep_count": int(getattr(s, "microsleep_count", 0) or 0),
            "awake_hours": _r(getattr(s, "awake_hours", 0.0), 1),
        }

    def _parse_cycle_anchor(self, raw: Any):
        """把配置里的「上次月经第一天」解析成 date。

        空字符串、格式不对、日期在未来，一律返回 None（= 不锚定）。
        之所以不抛异常：配置写错不该让整个插件起不来，也不该让周期停在
        某个奇怪相位上 —— 退回旧的累加行为，并在日志里说清楚为什么。
        """
        text = str(raw or "").strip()
        if not text:
            self._cycle_anchor_note = "未填日期，周期从启用那一刻自己累加"
            return None
        # 容忍常见写法：2026/01/01、2026.1.5、2026年1月1日、20260101。
        # 只认一种分隔符对使用者太苛刻（面板里也没地方写格式提示之外的东西）。
        norm = text
        for sep in ("年", "月", "/", ".", " ", "\t"):
            norm = norm.replace(sep, "-")
        norm = norm.replace("日", "")
        norm = "-".join(p for p in norm.split("-") if p.strip())
        day = None
        for fmt, candidate in ((("%Y-%m-%d"), norm),
                               (("%Y%m%d"), norm.replace("-", ""))):
            try:
                day = datetime.strptime(candidate, fmt).date()
                break
            except ValueError:
                continue
        if day is None:
            self._cycle_anchor_note = f"日期「{text}」没认出来，已退回从启用那一刻算"
            self._get_logger().warning(
                "cycle.last_period_date 不是合法日期（%r），按未设置处理；"
                "周期退回「从启用那刻开始累加」", text)
            return None
        today = datetime.now().date()
        if day > today:
            self._cycle_anchor_note = f"日期 {day.isoformat()} 在今天之后，已退回从启用那一刻算"
            self._get_logger().warning(
                "cycle.last_period_date 在未来（%s > 今天 %s），按未设置处理",
                day.isoformat(), today.isoformat())
            return None
        self._cycle_anchor_note = f"相位按现实日历，从 {day.isoformat()} 起算"
        return day

    def _cycle_view(self) -> dict[str, Any]:
        """月经面板：双相位、激素水平、经期状态、疼痛。"""

        s = self._s
        anchor = getattr(self._engine, "cycle_anchor_date", None)
        phase_zh = {"follicular": "卵泡期", "ovulation": "排卵期", "luteal": "黄体期"}
        uterus_zh = {"menstrual": "行经期", "proliferative": "增殖期", "secretory": "分泌期"}
        return {
            "day": _r(s.menstrual_day, 1),
            "length": _r(s.cycle_length, 0),
            "phase": s.menstrual_phase,
            "phase_text": phase_zh.get(s.menstrual_phase, "-"),
            "uterus_phase": s.uterus_phase,
            "uterus_text": uterus_zh.get(s.uterus_phase, "-"),
            "menstruating": bool(s.menstruating),
            "flow": _r(s.menstrual_flow, 2),
            "estrogen": _r(s.estrogen, 2),
            "progesterone": _r(s.progesterone, 2),
            "pain": _r(s.pain, 1),
            "follicle_maturing": bool(s.follicle_maturing),
            "luteal_maturing": bool(s.luteal_maturing),
            # 第几天是按什么算的：calendar=现实日历锚点，sim=从启用那刻累加
            "day_source": "calendar" if anchor else "sim",
            "anchor_date": anchor.isoformat() if anchor else "",
            # 一句人话，说明相位依据；日期填错时这里也会说清楚
            "anchor_note": getattr(self, "_cycle_anchor_note", ""),
        }

    def _link_view(self) -> dict[str, Any]:
        """联动与喂食状态，给 WebUI 展示用。"""

        cfg = self.config
        link = self._link
        view: dict[str, Any] = {
            "enabled": bool(cfg.link.enabled),
            "sync_activity": bool(cfg.link.sync_activity),
            "sync_meal": bool(cfg.link.sync_meal),
            "inject_state": bool(cfg.link.inject_state),
            "sync_clock": bool(cfg.link.sync_clock),
            "share_state": bool(cfg.link.share_state),
            "feeding": bool(cfg.feeding.enabled),
            # -1 表示"麦生活没给睡眠信息"，WebUI 上显示为空更准确
            "sleep": (None if getattr(self._engine, "link_sleep", -1.0) < 0.0
                      else _r(getattr(self._engine, "link_sleep"), 2)) if self._engine else None,
            "activity": _r(getattr(self._engine, "link_activity", 0.0), 2) if self._engine else 0.0,
            "meals_external": bool(getattr(self._engine, "_meals_external", False)) if self._engine else False,
            "clock_offset": _r(getattr(self._engine, "clock_offset", 0.0), 2) if self._engine else 0.0,
            # 时钟来源：system=内置系统钟（默认），sim=纯仿真钟（会漂移）
            "clock_source": str(getattr(self._engine, "_wall_clock_source", "sim")),
            "follow_system_clock": bool(getattr(self.config.sim, "follow_system_clock", True)),
            "ok": False,
            "phase": "",
            "summary": "",
            "last_error": "",
            "provider": "",
            "plugin_id": "",
        }
        if link is not None:
            try:
                status = link.status()
            except Exception:  # noqa: BLE001 - 展示层不得因联动异常而失败
                status = {}
            view.update({
                "ok": bool(status.get("last_ok")),
                "phase": str(status.get("phase") or ""),
                "summary": str(status.get("summary") or ""),
                "last_error": str(status.get("last_error") or ""),
                # 当前联动的是哪个日程插件，WebUI 据此显示「麦织 / 麦生活」。
                # provider 是实际生效值；provider_mode 用来区分「自动识别」与「手填」。
                "provider": str(status.get("provider") or ""),
                "provider_mode": str(status.get("provider_mode") or ""),
                "provider_auto": bool(status.get("provider_auto")),
                "provider_resolved": bool(status.get("provider_resolved")),
                "plugin_id": str(status.get("plugin_id") or ""),
                # v1.3.1：麦织/麦生活的"未来安排、醒来时刻、心情参考、打扰判断"
                "next_plan": status.get("next_plan"),
                "sleep_until": str(status.get("sleep_until") or ""),
                "mood_ref": status.get("mood_ref"),
                "energy_ref": status.get("energy_ref"),
                "behavior": status.get("behavior"),
            })
        return view

    def _hospital_view(self) -> dict[str, Any]:
        """濒死与急救的对外视图。"""

        s = self._s
        silent_hours = max(0.0, float(self.config.rescue.silent_hours))
        in_hospital = s.hospital_until > s.t
        silent, why = self._silence_window() if in_hospital else (False, "")
        return {
            "critical": bool(s.critical),
            "critical_seconds": _r(max(0.0, s.t - s.critical_since), 0) if s.critical else 0.0,
            "in_hospital": bool(in_hospital),
            "visits": int(s.hospital_visits),
            "reason": str(s.hospital_reason or ""),
            "hours_left": _r((s.hospital_until - s.t) / H2S, 2) if in_hospital else 0.0,
            "silent_hours": _r(silent_hours, 2),
            "silent": bool(silent),
            "silent_note": why,
            "rescue_enabled": bool(self.config.rescue.enabled),
        }

    def _silence_window(self) -> tuple[bool, str]:
        """住院静默窗口：(是否静默, 说明)。"""

        cfg = self.config.rescue
        hours = float(cfg.silent_hours)
        if hours <= 0.0:
            return False, ""
        s = self._s
        if s.hospital_since <= 0.0:
            return False, ""
        if s.hospital_until <= s.t:
            # 已经出院
            return False, ""
        silent_until = s.hospital_since + min(hours, max(0.0, float(cfg.stay_hours))) * H2S
        if s.t >= silent_until:
            return False, ""
        left = (silent_until - s.t) / H2S
        return True, f"住院静默中（还剩 {left:.1f} 小时）"

    # ===== 引擎驱动 =====

    @property
    def _s(self) -> BodyState:
        """当前身体状态。"""

        assert self._engine is not None
        return self._engine.s

    def _sim_day_at(self, t: float) -> int:
        """任意仿真时刻属于第几天（从 1 起）。口径见下面的 _body_sim_day。

        v1.3.1 抽出来：原先只有 _body_sim_day() 用正确口径（t/3600 + clock_offset），
        而 _on_event 另写了一份 t//86400 —— 于是事件列表里的「第 N 天」永远比状态
        时钟早一天（实测 t=164420 的餐事件被标成第 2 天，实际是第 3 天）。
        一个公式只留一处实现，就不会再有两套真相。
        """
        total_h = float(t) / H2S + float(getattr(self._engine, "clock_offset", 0.0) or 0.0)
        return int(total_h // 24.0) + 1

    def _body_sim_day(self) -> int:
        """仿真进行到第几天，从 1 起。

        按「午夜翻页」算，不按 t//86400 算：钟点 = t/3600 + clock_offset，
        只有相位为 0 时两者才重合。跟随系统钟后相位必然非 0，
        用 t//86400 会让"今天"在中午清零。
        """

        assert self._engine is not None
        return self._sim_day_at(self._s.t)

    def _counters(self) -> tuple[int, int, int, int, float]:
        """计数器快照：(仿真天, 进食, 饮水, 排尿, 累计尿量)。"""

        s = self._s
        return (
            self._body_sim_day(), int(s.meals), int(s.drinks),
            int(s.urinations), float(s.urine_total),
        )

    def _sync_day_base(self) -> None:
        """每步检查是否跨天；跨天瞬间把当时的计数器记为当天基准。

        在步进循环里调用，检测延迟不超过一个 dt（默认 10 仿真秒），
        因此基准值就是零点那一刻的累计量，而不是第一次查询时的值。
        """

        snap = self._counters()
        if self._day_base is None:
            # 首次运行：把"当天零点前"的累计量反推为基准，避免把历史算进今天
            self._day_base = (snap[0], 0, 0, 0, 0.0) if snap[0] == 1 else snap
            return
        if self._day_base[0] != snap[0]:
            self._day_base = snap

    def _today(self) -> dict[str, float]:
        """今日累计量：以当天零点为基准的差值。"""

        self._sync_day_base()
        snap = self._counters()
        base = self._day_base or snap
        return {
            "meals": max(0, snap[1] - base[1]),
            "drinks": max(0, snap[2] - base[2]),
            "urinations": max(0, snap[3] - base[3]),
            "urine_total_l": round(max(0.0, snap[4] - base[4]) / 1000.0, 2),
        }

    def _data_dir(self) -> Path:
        """插件数据目录，不存在时创建。"""

        path = self.ctx.paths.data_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _weather_loop(self) -> None:
        """独立的天气轮询。与联动解耦：联动关了也照常更新室外环境。"""

        interval = max(300.0, float(getattr(self.config.link, "weather_refresh_seconds", 1800.0)))
        # 启动后先拉一次，避免头半小时没有天气数据
        try:
            await self._refresh_outdoor()
        except Exception:
            pass
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._ready:
                    continue
                await self._refresh_outdoor()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 天气失败不影响身体
                self._get_logger().debug("天气刷新失败: %s: %s", type(exc).__name__, exc)

    async def _refresh_outdoor(self) -> None:
        """拉一次室外天气并写进状态。任何失败静默降级。"""

        link = self._link
        if link is None:
            return
        cfg = self.config
        tz = "Asia/Shanghai"
        env_cfg = getattr(cfg, "environment", None)
        if env_cfg is not None:
            tz = str(getattr(env_cfg, "timezone", "") or tz)
        # 室内外判断要用当前场景地点
        loc = str(getattr(link, "_location_hint", "") or "")
        try:
            # 走 provider 感知的取场景：麦生活仍是 get_current_scene，
            # 麦织改由 get_life_status 取（它没有 get_current_scene）
            scene = await link.current_scene()
            cur = (scene or {}).get("current") or {}
            loc = str(cur.get("location") or "") or loc
            link._location_hint = loc
        except Exception:
            pass

        outdoor = await link.fetch_outdoor(timezone=tz, location=loc)
        if not outdoor:
            return
        s = self._s
        s.env_temp_c = float(outdoor.get("temp_c") or 22.0)
        s.env_humidity = float(outdoor.get("humidity") or 50.0)
        s.env_feels_c = float(outdoor.get("feels_c") or 22.0)
        s.env_load = float(outdoor.get("load") or 0.0)
        s.env_indoor = bool(outdoor.get("indoor", True))
        s.env_updated_at = float(outdoor.get("fetched_at") or 0.0)
        s.env_wind_ms = float(outdoor.get("wind_ms") or 0.0)

    async def _linker(self) -> None:
        """联动后台循环：定时拉麦生活状态，喂给引擎。

        与 _ticker 分开跑：联动是网络/跨进程调用，慢也不能拖住仿真；
        拉失败时保留上一次的值，不把睡眠状态突变成 0。
        """

        interval = max(10.0, float(self.config.link.interval_seconds))
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._ready or self._link is None:
                    continue
                if not self.config.link.enabled:
                    continue
                try:
                    await self._link_cycle()
                except Exception as exc:  # noqa: BLE001 - 联动失败不能拖垮插件
                    self._get_logger().warning(
                        "麦麦身体联动失败：%s: %s", type(exc).__name__, exc
                    )
        except asyncio.CancelledError:
            raise

    async def _link_cycle(self) -> None:
        """一次联动周期：拉状态、写引擎、按日程触发进餐。"""

        link = self._link
        engine = self._engine
        if link is None or engine is None:
            return

        data = await link.pull()
        if not data:
            # 麦生活掉线/超时：超过两个拉取周期还拿不到状态，就把注入值退回中性。
            # 早前一直保留上次的值，半夜断线会让她"停在深睡"里直到重启。
            interval = max(10.0, float(self.config.link.interval_seconds))
            stale_after = interval * 2.0
            last_ok = float(getattr(link, "_last_ok", 0.0) or 0.0)
            if last_ok and (time.time() - last_ok) > stale_after:
                if self._engine is not None and self._engine.link_sleep != -1.0:
                    self._engine.link_sleep = -1.0
                    self._engine.link_activity = 0.0
                    self._engine.link_wake_hour = -1.0
                    self._get_logger().warning(
                        "麦生活联动已失联 %.0f 秒，睡眠/活动注入值退回中性", time.time() - last_ok
                    )
            return

        cfg = self.config.link
        if cfg.sync_activity:
            # 显式取 -1 兜底：拿不到睡眠阶段时保持"无外部意见"，
            # 不能退化成 0.0（那等于替麦生活宣布"她醒着"）。
            raw_sleep = data.get("sleep")
            engine.link_sleep = -1.0 if raw_sleep is None else float(raw_sleep)
            engine.link_activity = float(data.get("activity") or 0.0)

            # 2026-11-22：麦织的 current 为空时仍可能"实际在睡"（刚道晚安、
            # 临时唤醒还没续上日程）。evaluate_behavior 的结论比 current 准，
            # 用它兜底把身体也置为睡着 —— 只补"睡着"，不反推"醒着"。
            _beh = data.get("behavior") or {}
            if (engine.link_sleep <= 0.0 and isinstance(_beh, dict)
                    and str(_beh.get("reason") or "") == "sleeping"):
                engine.link_sleep = 1.0

            # 醒来时刻：麦织给 sleep_until（带时区的 ISO），换成本地小时。
            # 没给就退回内置窗口（-1）。只在睡着时由引擎采用，见 _in_sleep_window。
            engine.link_wake_hour = -1.0
            _iso = str(data.get("sleep_until") or "")
            if _iso and engine.link_sleep > 0.0:
                try:
                    _dt = datetime.fromisoformat(_iso)
                    engine.link_wake_hour = _dt.hour + _dt.minute / 60.0
                except ValueError:
                    engine.link_wake_hour = -1.0

        # ---- 日程运动（2026-11-14）----
        # 麦生活的 kind 没有 exercise，只能从 summary 认。
        # 认出来之后还要过一遍身体否决：外面暴晒/刮风/正在痛经就不跑。
        ex_cfg = getattr(self.config, "exercise", None)
        if ex_cfg is not None and bool(getattr(ex_cfg, "enabled", True)):
            if not engine.s.exercise_intent:
                try:
                    # 同上：麦织没有 get_current_scene，旧写法在麦织下必然抛异常
                    # 并被下面的 except 静默吞掉，导致"日程写了跑步身体不跑"。
                    scene = await link.current_scene()
                    cur = (scene or {}).get("current") or {}
                    summary = str(cur.get("summary") or "")
                    kind = str(cur.get("kind") or "")
                    level = link.detect_exercise(summary)
                    if level and kind not in ("sleep", "nap"):
                        ok = engine.request_exercise(level, 30.0)
                        if ok:
                            self._get_logger().info(
                                "日程运动已接受：%s（%s）", summary, level)
                        elif engine.s.exercise_refused:
                            self._get_logger().info(
                                "日程运动被身体否决：%s → %s",
                                summary, engine.s.exercise_refused)
                except Exception:
                    pass

        if cfg.sync_meal:
            slot, key, summary, items = await link.meal_trigger(self._s.hour)
            if slot and summary and len(items) < 2:
                # 摘要里只认出一样（或一样都没有）时交给 utils 模型补全：
                # 「番茄炒蛋」关键词只能认出 Egg，模型能补出 Rice；
                # 「准备晚饭」这种只有动作的，模型会按正餐补主食。
                items, src = await decompose_menu(summary, limit=6, llm=self.ctx.llm)
                if items and src == "llm":
                    self._get_logger().info(
                        "日程摘要经 utils 拆出食材：%s（原文：%s）",
                        combo_summary(items),
                        summary,
                    )
            if slot:
                # 去重键落到插件自己身上并立刻随下一次自动存档落盘：
                # 只在 LifeLink 内存里时，重启后同一饭窗会被再吃一遍。
                if key:
                    self._link_meal_key = key
                if not engine._link_meal:
                    engine._link_meal = slot
                    # 日程点名的具体食物一起送进去；为空则引擎按餐段随机配。
                    engine._link_meal_items = list(items) if items else None
                    if items:
                        self._get_logger().info(
                            "麦生活日程触发进食：%s，日程点名 %s（日程：%s）",
                            slot,
                            combo_summary(items),
                            summary or "-",
                        )
                    else:
                        self._get_logger().info(
                            "麦生活日程触发进食：%s（日程：%s）",
                            slot,
                            summary or "-",
                        )

        # 内置时钟优先：两者都开时会互相拉扯（一个按系统时间写相位、
        # 一个按麦生活环境时间写相位），谁后跑谁生效，钟点会来回跳。
        if cfg.sync_clock and not bool(getattr(self.config.sim, "follow_system_clock", True)):
            delta = await link.clock_offset(self._s.hour)
            if delta:
                # s.hour 已经含 clock_offset，所以 delta 是"当前残差"，
                # 加回去即是正确的一次对齐；下一轮残差归零就不再调整（有 0.1h 死区）。
                engine.clock_offset = (engine.clock_offset + delta) % 24.0
                self._get_logger().info(
                    "麦麦身体时钟对齐：相位移动 %+.2f 小时，当前 %.1f 点",
                    delta,
                    self._s.hour,
                )

        if cfg.share_state:
            await self._share_body_state()

    async def _share_body_state(self) -> None:
        """住院或首次进入濒危时，把这件事交给麦生活作为一次主动分享契机。

        每件事只分享一次，靠 (事件类型, 计数) 去重；出院后不补发，
        避免身体好转了还在聊昨天那场急救。
        """

        link = self._link
        engine = self._engine
        if link is None or engine is None:
            return
        s = self._s

        # 住院：按 hospital_visits 去重
        if s.hospital_until > s.t and s.hospital_visits > self._shared_visit:
            self._shared_visit = s.hospital_visits
            left = (s.hospital_until - s.t) / H2S
            ok = await link.share(
                topic=f"我刚刚被送去医院急救了，原因是{s.hospital_reason or '身体撑不住'}",
                motive=f"身体状态真的出了问题，想让对方知道，大概还要 {left:.1f} 小时才能出院",
                weight=0.8,
            )
            if ok:
                self._get_logger().info("麦麦身体已向麦生活提交住院分享契机")
            return

        # 濒危但还没送医：按 critical_since 去重
        if s.critical and not self._share_armed:
            return
        if s.critical and self._share_armed and s.health <= float(engine.rescue_threshold) + 5.0:
            self._share_armed = False
            await link.share(
                topic=f"我现在身体很不对劲，{engine._rescue_reason()}",
                motive="说不清具体怎么了，但感觉需要被关心一下",
                weight=0.5,
            )
        elif not s.critical:
            # 恢复后重新上膛，下一次濒危才能再分享
            self._share_armed = True

    async def _ticker(self) -> None:
        """后台步进循环：按墙钟差值推进仿真，绝不做重计算。"""

        interval = 1.0
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._ready:
                    continue
                try:
                    self._advance_wall()
                except Exception as exc:  # noqa: BLE001 - 单步失败不能杀死 ticker
                    self._step_error = f"{type(exc).__name__}: {exc}"
                    self._get_logger().warning("麦麦身体步进失败：%s", self._step_error)
                if self.config.sim.autosave_seconds > 0 and (
                    time.time() - self._last_save_wall >= float(self.config.sim.autosave_seconds)
                ):
                    await self._save()
        except asyncio.CancelledError:
            raise

    def _sync_wall_clock(self) -> None:
        """内置时钟：把身体钟点对齐到系统时间。

        与麦生活的 sync_clock 是两条独立通路。开着内置时钟时以本机系统
        时间为准，联动那边的时钟同步会被让位（见 _link_cycle），
        否则两个源会互相拉扯，谁后写谁生效。
        """

        engine = self._engine
        if engine is None:
            return
        if not bool(getattr(self.config.sim, "follow_system_clock", True)):
            engine.follow_wall_clock = False
            engine._wall_clock_source = "sim"
            return
        engine.follow_wall_clock = True
        before = engine.s.hour
        after = engine.sync_wall_clock()
        engine._wall_clock_source = "system"
        # 首次对齐或大跨度校正时记一条日志，方便排查"时间怎么突然对了"
        if abs(((after - before + 12.0) % 24.0) - 12.0) > 0.5:
            self._get_logger().info(
                "麦麦身体时钟已对齐系统时间：%s -> %s",
                _clock(before), _clock(after),
            )

    def _advance_wall(self) -> None:
        """按墙钟推进仿真：攒够一个 dt 就走一步。"""

        cfg = self.config.sim
        now = time.time()
        elapsed = max(0.0, now - self._last_step_wall)
        self._last_step_wall = now
        self._debt += elapsed * max(0.01, float(cfg.speed))

        dt = max(1.0, min(20.0, float(cfg.dt)))
        steps = 0
        limit = 600
        # 内置时钟：先按系统墙钟校正相位，再走步。放在循环前，
        # 保证本轮每一步的饭点/睡眠判断都落在真实钟点上。
        self._sync_wall_clock()
        while self._debt >= dt and steps < limit:
            self._engine.step(dt)
            self._debt -= dt
            steps += 1
            self._sync_day_base()
            self._maybe_sample()
        if steps >= limit:
            self._debt = 0.0

    def _catch_up(self) -> None:
        """启动时补齐离线期间的身体运转。"""

        assert self._engine is not None
        cfg = self.config.sim
        if self._offline_wall <= 0.0:
            # 没有有效基准（1970 或未初始化）就不补算，否则会一次性补满 catch_up_max_hours
            return
        gap_wall = max(0.0, time.time() - self._offline_wall)
        gap_sim = gap_wall * max(0.01, float(cfg.speed))
        if bool(getattr(cfg, "follow_system_clock", True)):
            # 跟随系统钟时不能截断：截掉的那段生理演化永远补不回来，
            # 而钟点又已被拉回真实时间，两者会永久错开。步数另有 20000 上限兜底。
            sim_gap = gap_sim
        else:
            sim_gap = min(gap_sim, float(cfg.catch_up_max_hours) * H2S)
        if sim_gap <= 0.0:
            return
        dt = max(1.0, min(20.0, float(cfg.dt)))
        steps = int(sim_gap // dt)
        for _ in range(min(steps, 20000)):
            self._engine.step(dt)

    def _maybe_sample(self) -> None:
        """每 60 仿真秒记一个采样点。"""

        s = self._s
        if self._samples and s.t - self._samples[-1]["t"] < 59.0:
            return
        self._samples.append(self._sample_point())

    def _seed_samples(self) -> None:
        """把当前状态作为第一个采样点。"""

        if not self._samples:
            self._samples.append(self._sample_point())

    def _sample_point(self) -> dict[str, Any]:
        """构造一个采样点。"""

        s = self._s
        return {
            "t": _r(s.t, 1),
            "clock": _clock(s.hour),
            "day": self._body_sim_day(),
            "hr": _r(s.hr, 1),
            "sbp": _r(s.sbp, 1),
            "dbp": _r(s.dbp, 1),
            "glucose": _r(s.glucose, 2),
            "temp": _r(s.temp, 2),
            "energy": _r(s.energy, 1),
            "hunger": _r(s.hunger, 1),
            "thirst": _r(s.thirst, 1),
            "gastric": _r(s.gastric_volume, 1),
            "bac": _r(s.bac, 3),
            "na": _r(s.na, 2),
            "sao2": _r(s.sao2, 1),
            # v1.2.5：情绪进历史采样，WebUI 才能画趋势曲线。
            # 之前只采体征，情绪只能看"此刻"的一条横条，看不出起伏。
            "stress": _r(getattr(s, "stress", 0.0), 1),
            "depression": _r(getattr(s, "depression", 0.0), 1),
            "euphoria": _r(getattr(s, "euphoria", 0.0), 1),
            "dysphoria": _r(getattr(s, "dysphoria", 0.0), 1),
            "mood_valence": _r(getattr(s, "mood_valence", 0.0), 1),
            # v1.3.1：心境底色（慢变量）也进采样，WebUI 情绪曲线要看得见它
            "mood_tone": _r(getattr(s, "mood_tone", 0.0), 1),
            # v1.3.1：分期是连续量，进采样而不是进事件流 ——
            # WebUI 的「夜间分期图」就是拿这一列画的（见 _now_js 的 renderHypnogram）。
            "sleep_stage": str(getattr(s, "sleep_stage", "awake") or "awake"),
            "sleep_depth": _r(getattr(s, "sleep_depth", 0.0), 2),
        }

    # 不落库的事件种类（v1.3.1）：这些是高频的中间状态，不是"事件"。
    # sleep_stage：一夜几十条，且分期已改由采样承载（见 webui_history 的 stages）；
    # 留着它只会把时间线刷满，让进食/排尿/醒来这些真正的节点被淹掉。
    _NOISE_EVENT_KINDS = frozenset({"sleep_stage"})

    def _on_event(self, ev: Any) -> None:
        """把引擎事件转存为 WebUI 可读的字典。"""

        if str(getattr(ev, "kind", "")) in self._NOISE_EVENT_KINDS:
            return
        self._events.append({
            "t": _r(getattr(ev, "t", 0.0), 1),
            # 与状态时钟（_sample_point 用 s.hour）必须用同一个相位：
            # s.hour = (t/H2S + clock_offset) % 24，事件侧漏加 clock_offset
            # 会让「最近发生的事」整条时间轴偏移 offset 小时，跟日程怎么比都对不上。
            "clock": _clock(
                (getattr(ev, "t", 0.0) / H2S + float(getattr(self._engine, "clock_offset", 0.0) or 0.0))
                % 24.0
            ),
            # v1.3.1 修：原为 int(t // 86400) + 1，与状态时钟口径不符（见 _sim_day_at）。
            "day": self._sim_day_at(float(getattr(ev, "t", 0.0) or 0.0)),
            "kind": str(getattr(ev, "kind", "")),
            "level": str(getattr(ev, "severity", "info")),
            "text": str(getattr(ev, "text", "")),
        })

    # ===== 存档 =====

    async def _save(self) -> None:
        """把身体快照与墙钟时间写入数据目录。"""

        if self._engine is None:
            return
        self._last_save_wall = time.time()
        payload = {
            "version": PLUGIN_VERSION,
            "saved_wall": self._last_save_wall,
            "seed": self._seed,
            "state": self._s.snapshot(),
            "samples": list(self._samples)[-600:],
            "events": list(self._events)[-200:],
            "day_base": list(self._day_base) if self._day_base else None,
            # 身体时钟的"上一次钟点"必须一起存。否则重启后引擎里恒为 0.0，
            # 第一步就会判定"跨过了 07:30"，凭空多吃一份早餐（见 2026-11-08 11:44/11:54/12:03 连吃）。
            "prev_hour": float(getattr(self._engine, "_prev_hour", 0.0)),
            # 时钟相位也要存：新身体从早上 8 点起步（不是 0 点），这个相位
            # 是 s.hour 与 s.t 的唯一换算系数。不存的话，重启后新引擎按 8 点
            # 起步、再套上存档里的 s.t，钟点会整整偏 8 小时，昼夜曲线全错位。
            "clock_offset": float(getattr(self._engine, "clock_offset", 0.0)),
            # 联动进餐的去重键也要存：它只在内存里时，重启后当天的餐段会被
            # 再触发一次，又变成"重启就多吃一顿"。
            "link_meal_key": str(self._link_meal_key or ""),
        }
        target = self._data_dir() / SAVE_FILE
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            self._get_logger().warning("麦麦身体存档失败：%s", exc)

    async def _restore(self) -> None:
        """读取存档；缺失或结构异常时从默认身体开始。"""

        # 基准取"现在"：无存档或存档损坏时不该补算离线时间，身体从零开始走
        self._offline_wall = time.time()
        target = self._data_dir() / SAVE_FILE
        if not target.exists():
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("存档根节点必须是对象")

            raw = payload.get("state")
            if raw is not None and not isinstance(raw, dict):
                raise TypeError("state 必须是对象")
            known = set(BodyState.__dataclass_fields__.keys())
            restored = {k: v for k, v in (raw or {}).items() if k in known}
            if restored:
                seed_state = BodyState(**restored)
                assert self._engine is not None
                self._engine.s = seed_state
                # 2026-11-26：旧存档只有 sleep_bouts、没有 sleep_nights。
                # 不重建的话，WebUI 的「最近一夜」会一直显示「—」，
                # 直到下一夜结算（最长要等十几个小时）。这里按 night_key
                # 把已有 bouts 分组回填，面板立刻有数；幂等，可重复调用。
                if not getattr(seed_state, "sleep_nights", None) and getattr(seed_state, "sleep_bouts", None):
                    try:
                        n = self._engine.rebuild_nights_from_bouts()
                        self._get_logger().info(
                            "睡眠夜间聚合已从 %d 段 bout 回填为 %d 夜",
                            len(seed_state.sleep_bouts), n)
                    except Exception as exc:  # noqa: BLE001 - 回填失败不该影响启动
                        self._get_logger().debug("夜间回填跳过：%s", exc)
                # 2026-11-27：结构快照回填。旧存档没有 sleep_last_struct，
                # 而 dur_* 此刻可能正残留着午睡的时长（就是这个 bug 的现场）。
                # 用最近一段合格 bout 回填，面板重载后立刻显示真实结构，
                # 不必等到当晚下一次结算。
                if (not getattr(seed_state, "sleep_last_struct", None)
                        and getattr(seed_state, "sleep_bouts", None)):
                    try:
                        last = seed_state.sleep_bouts[-1]
                        if isinstance(last, dict):
                            seed_state.sleep_last_struct = {
                                "light_s": round(float(last.get("light_h", 0.0) or 0.0) * H2S, 1),
                                "deep_s": round(float(last.get("deep_h", 0.0) or 0.0) * H2S, 1),
                                "rem_s": round(float(last.get("rem_h", 0.0) or 0.0) * H2S, 1),
                                "awake_s": 0.0,
                                "hours": round(float(last.get("hours", 0.0) or 0.0), 2),
                                "quality": round(float(last.get("quality", 0.0) or 0.0), 1),
                                "end_t": float(last.get("end_t", 0.0) or 0.0),
                            }
                    except Exception as exc:  # noqa: BLE001 - 回填失败不该影响启动
                        self._get_logger().debug("结构快照回填跳过：%s", exc)
                # 2026-11-27：夜归属规则升级到 v2（按入睡时刻 + 结算时固化 key）。
                # 旧规则用「醒来时刻 + 当时的 clock_offset」现算，而 clock_offset
                # 可变（sync_clock 每次同步重写、重启按墙钟重对齐），于是同一段
                # bout 的归属会随时间改变 —— 服务器实测 key=17 的夜跨度 33.6h，
                # 把 d17 夜和 d18 夜并成了一夜（hours=16.11、cycles=5）。
                # 这里按 bouts 整体重建一次；重建幂等，做完标记为 2。
                if int(getattr(seed_state, "sleep_night_rule", 1) or 1) < 2:
                    try:
                        n = self._engine.rebuild_nights_from_bouts()
                        seed_state.sleep_night_rule = 2
                        self._get_logger().info(
                            "睡眠夜归属已按 v2 规则重建：%d 段 bout -> %d 夜",
                            len(getattr(seed_state, "sleep_bouts", None) or []), n)
                    except Exception as exc:  # noqa: BLE001 - 重建失败不该影响启动
                        self._get_logger().debug("夜归属重建跳过：%s", exc)

            saved_wall = payload.get("saved_wall")
            self._offline_wall = float(saved_wall or 0.0)

            # 钟点基准：优先用存档值；老存档没有这个字段时退化为"当前钟点"。
            # 两种做法都保证重启不会把已经过去的饭点当成刚跨过。
            assert self._engine is not None
            clock_offset = payload.get("clock_offset")
            if isinstance(clock_offset, (int, float)) and 0.0 <= float(clock_offset) < 24.0:
                self._engine.clock_offset = float(clock_offset)
            else:
                # 老存档（本次改动之前）都是从 0 点相位跑出来的，
                # 不能套用新身体默认的 8 点相位，否则整条昼夜曲线偏 8 小时。
                self._engine.clock_offset = 0.0

            prev_hour = payload.get("prev_hour")
            if isinstance(prev_hour, (int, float)) and 0.0 <= float(prev_hour) < 24.0:
                self._engine._prev_hour = float(prev_hour)
            else:
                self._engine._prev_hour = self._engine.s.hour

            stored_key = payload.get("link_meal_key")
            if isinstance(stored_key, str):
                self._link_meal_key = stored_key
                # 同步到联动客户端，让本轮的去重判断直接接着上次的键走。
                if self._link is not None:
                    self._link._last_meal_key = stored_key

            samples = payload.get("samples") or []
            if not isinstance(samples, list):
                raise TypeError("samples 必须是数组")
            for point in samples:
                if isinstance(point, dict) and "t" in point:
                    self._samples.append(point)

            events = payload.get("events") or []
            if not isinstance(events, list):
                raise TypeError("events 必须是数组")
            for item in events:
                if isinstance(item, dict) and "text" in item:
                    self._events.append(item)

            base = payload.get("day_base")
            if base is not None:
                if not isinstance(base, list) or len(base) != 5:
                    raise TypeError("day_base 必须是长度为 5 的数组")
                self._day_base = (
                    int(base[0]), int(base[1]), int(base[2]), int(base[3]), float(base[4]),
                )
        except (OSError, ValueError, TypeError, KeyError, AssertionError) as exc:
            self._get_logger().warning("麦麦身体存档结构异常，重新开始：%s", exc)
            self._offline_wall = time.time()
            self._samples.clear()
            self._events.clear()
            self._day_base = None
            if self._engine is not None:
                self._engine.s = BodyState()

    # ===== WebUI API =====

    # ===== 住院静默 =====

    @HookHandler(
        "chat.receive.before_process",
        name="hospital_silence",
        description="住院急救期间不回应任何消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hospital_silence(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        """住院静默窗口内吞掉入站消息，直接中止处理链。"""

        del kwargs
        if self._engine is None or not self._ready:
            return None
        try:
            silent, why = self._silence_window()
        except Exception as exc:  # noqa: BLE001 - 钩子异常不能打断主链
            self._get_logger().debug("住院静默判断失败：%s", exc)
            return None
        if not silent:
            return None

        # 事件按 10 分钟节流，避免每条消息都记一笔
        s = self._s
        if s.t - self._last_silence_note >= 600.0:
            self._last_silence_note = s.t
            self._events.append({
                "t": _r(s.t, 1),
                "clock": _clock(s.hour),
                "day": self._body_sim_day(),
                "kind": "quiet",
                "level": "notice",
                "text": f"住院期间没有回应消息（{s.hospital_reason or '急救中'}）",
            })
        self._get_logger().debug("住院静默，忽略消息：%s", why)
        return {"action": "abort", "modified_kwargs": {"message": message} if message is not None else {}}

    @API(name="webui_now", description="麦麦此刻的身体状态：体征、体感、胃内容物与今日行为计数", version="1", public=False)
    async def webui_now(self, **kwargs: Any) -> dict[str, Any]:
        """返回实时身体快照。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            vitals = []
            for field, label, unit, digits, _span, _low, _high in VITALS:
                value = float(getattr(s, field, 0.0))
                vitals.append({
                    "key": field, "label": label, "unit": unit,
                    "value": _r(value, digits),
                    "text": _vital_text(field, value),
                    "tone": _tone(field, value),
                })
            feelings = []
            for field, label in FEELINGS:
                value = float(getattr(s, field, 0.0))
                feelings.append({
                    "key": field, "label": label,
                    "value": _r(value, 0),
                    # bar：条填充百分比，统一「越满越舒服」。饥饿/口渴/压力/
                    # 疼痛/心境这些"数值越高越糟"的项会被取反，于是六行
                    # 条方向一致 —— 短=缺，长=好（2026-11-16 可读性修正）。
                    "bar": round(feeling_bar_pct(field, value), 1),
                    # text：与 bar 同向措辞。反向维度用 feeling_word
                    # 的专用词表（"不饿/不太饿/饿了"），避免出现
                    # "条很短却写着不饿"这种自相矛盾。
                    "text": feeling_word(field, value),
                    "tone": _feeling_tone(field, value),
                })
            return {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "version": PLUGIN_VERSION,
                "clock": {
                    "time": _clock(s.hour),
                    "day": self._body_sim_day(),
                    "phase": phase(s.hour),
                    "t": _r(s.t, 1),
                    "speed": _r(self.config.sim.speed, 2),
                    "dt": _r(self.config.sim.dt, 1),
                    "alive": True,
                    "health": _r(s.health, 1),
                },
                "hospital": self._hospital_view(),
                "link": self._link_view(),
                "speech": {"describe": describe(s), "brief": brief(s)},
                "vitals": vitals,
                "feelings": feelings,
                "gut": {
                    "gastric_ml": _r(s.gastric_volume, 1),
                    "phase": eat_phase(s.gastric_volume),
                    "last_meal_items": str(s.last_meal_items or ""),
                    "last_drink_items": str(s.last_drink_items or ""),
                    "bac": _r(s.bac, 3),
                    "bac_text": drunk_level(s.bac) if s.bac > 0.10 else "",
                    "caffeine": _r(s.caffeine, 1),
                },
                "sleep": self._sleep_view(),
                "cycle": self._cycle_view(),
                "cycle_enabled": bool(getattr(self.config.cycle, "enabled", False)),
                "today": self._today(),
                "events": list(self._events)[-12:][::-1],
                "stock": list(self._samples)[-40:],
                "error": self._step_error,
            }
        except Exception as exc:  # noqa: BLE001 - 宿主只认异常
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_history", description="体征曲线：窗口内的采样序列（按宿主 JSON 预算降采样）", version="1", public=False)
    async def webui_history(self, hours: int = 12, **kwargs: Any) -> dict[str, Any]:
        """返回指定小时窗内的采样序列。

        宿主对插件 WebUI API 的返回值有硬上限（≤20000 个 JSON 元素、≤12 层、
        ≤512 KiB，见宿主 src/plugin_runtime/webui_schema.py）。18 条序列 + 3 个
        对齐列约 21 元素/点，24 小时窗口就是 1440 点 ≈ 3 万元素 —— 身体连续
        跑满约 16 小时后本接口必然 502，前端只显示
        「JSON 超过 20000 个元素」。载荷因此统一交给
        body.history.build_history_payload 按预算等间隔降采样。
        """

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            window = max(1, min(72, int(hours)))
            span = window * H2S
            now_t = self._s.t
            points = [p for p in self._samples if now_t - float(p.get("t", 0.0)) <= span]
            return build_history_payload(
                points,
                HISTORY_SERIES_KEYS,
                now=self._sample_point(),
                hours=window,
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_events", description="行为事件流：进食、饮水、排尿与告警", version="1", public=False)
    async def webui_events(self, limit: int = 60, kind: str = "", **kwargs: Any) -> dict[str, Any]:
        """返回最近的引擎事件。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            size = max(1, min(400, int(limit)))
            wanted = str(kind or "").strip()
            items = [e for e in self._events if not wanted or e.get("kind") == wanted]
            counts: dict[str, int] = {}
            for item in self._events:
                key = str(item.get("kind") or "")
                counts[key] = counts.get(key, 0) + 1
            return {"items": items[-size:][::-1], "counts": counts, "total": len(self._events)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_waveforms", description="生理波形：ECG 心电与 EEG 脑电的实时合成数据", version="1", public=False)
    async def webui_waveforms(self, seconds: float = 10.0, **kwargs: Any) -> dict[str, Any]:
        """返回 ECG 与 EEG 的波形点数组。

        波形是**按当前生理状态实时合成**的，不落库 —— 真实采样率下
        一分钟就是上万点，存档会爆炸。前端每次刷新重新取。

        时间约定（前端依赖，改这里必须同步改 webui/organs.html）：
          - 相位用**墙钟**推进，不用仿真时间（dt=10s，拿它当相位会每步跳一次）。
          - 返回 t0 = 第一个采样点的 epoch 秒，缓冲区覆盖 [t0, t0+seconds]。
          - t0 固定取 「现在 - _WAVE_LEAD_SEC(8s)」，于是缓冲区覆盖
            [现在-8s, 现在+12s]：前端以 8 秒窗口显示「到此刻为止」，
            10 秒后预取下一批时正好接上，看不出接缝。

        2026-11-14 修：旧实现没有时间参数，generate_ecg/generate_eeg 每次都从
        t=0 开始生成，连续两次调用返回的点完全相同 —— 视觉上就是一段波形
        无限循环重播。现在实测连续两次调用结果不同，且跨批次重叠区逐点一致。

        注意：这是仿真波形，不是医学数据。
        """

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            from .body import ecg as _ecg, eeg as _eeg

            win = max(3.0, min(30.0, float(seconds)))

            # ---- 时间基准：用墙钟，不用仿真步 ----
            # 仿真 dt=10 秒一步，如果拿仿真时间当波形相位，扫描头会每 10 秒
            # 跳一次。波形要像监护仪一样「现在这一刻也在走」，就必须用墙钟。
            # t_off 是缓冲区第一个采样点对应的 epoch 秒；前端用
            # (Date.now()/1000 - t0) 算出自己该显示哪一段。
            t_off = time.time() - _WAVE_LEAD_SEC

            # ---- ECG：由心率与每搏输出量驱动 ----
            ecg_data = _ecg.generate_ecg(
                hr=float(getattr(s, "hr", 72.0)),
                sv=float(getattr(s, "sv", 82.0)),
                rr=float(getattr(s, "rr", 16.0)),
                seconds=win,
                t_offset=t_off,
            )

            # ---- EEG：由睡眠阶段驱动；情绪接口预留未接线 ----
            stage = str(getattr(s, "sleep_stage", "awake") or "awake")
            if not bool(getattr(s, "sleeping", False)):
                stage = "awake"
            # 情绪通路：字段还没建，取默认中性。
            # 将来 BodyState 加上 emotion_arousal / emotion_valence 后，
            # 这里会自动开始读取，无需改本接口。
            arousal = float(getattr(s, "emotion_arousal", 0.0) or 0.0)
            valence = float(getattr(s, "emotion_valence", 0.0) or 0.0)
            eeg_data = _eeg.generate_eeg(
                stage=stage,
                seconds=win,
                arousal=arousal,
                valence=valence,
                t_offset=t_off,
            )

            return {
                "ecg": ecg_data,
                "eeg": eeg_data,
                # 缓冲区第一个采样点的 epoch 秒。前端靠它把数据池对齐到墙钟，
                # 从而在每次重新拉取时接上上一批的尾巴，而不是每 16 秒跳回开头。
                "t0": round(t_off, 6),
                "lead": _WAVE_LEAD_SEC,
                "clock": {
                    "time": _clock(s.hour),
                    "day": self._body_sim_day(),
                    "phase": phase(s.hour),
                },
                "disclaimer": "仿真波形，非医学数据，不可用于诊断",
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_organs", description="内脏细则：心血管、呼吸、代谢、肾脏、消化道、内分泌、受体、体温与嗜好品", version="1", public=False)
    async def webui_organs(self, **kwargs: Any) -> dict[str, Any]:
        """返回分组后的全部器官字段。"""

        del kwargs
        if not self._ready:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            groups = []
            for gid, title, subtitle, fields in ORGAN_GROUPS:
                rows = []
                for field, label, unit, digits in fields:
                    rows.append({
                        "field": field, "label": label, "unit": unit,
                        "value": _r(getattr(s, field, 0.0), digits),
                    })
                groups.append({
                    "id": gid, "title": title, "subtitle": subtitle, "rows": rows,
                    # v1.3.1：默认展示哪些字段由后端决定（见 KEY_FIELDS），
                    # 前端只负责「关键 + 可展开」，不在 JS 里硬编码字段名。
                    "key": [f for f in KEY_FIELDS.get(gid, ())
                            if any(r["field"] == f for r in rows)],
                })
            return {
                "clock": {"time": _clock(s.hour), "day": self._body_sim_day(), "phase": phase(s.hour)},
                "groups": groups,
                "field_count": len(BodyState.__dataclass_fields__),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_foods", description="食物库：312 种可吃食物的份量与成分（23 项 FoodType + 288 项扩展 + 1 项画像），以及三餐候选池", version="1", public=False)
    async def webui_foods(self, **kwargs: Any) -> dict[str, Any]:
        """返回只读食物表。

        2026-11-27（v1.4.0）：三处改动，都是展示层，引擎侧的 Food.group 不动
        （营养画像按 group 查表，改数据会影响估算）。

        1. 补上 v1.4.0 新增的 5 个营养素字段 —— 数据早就算好了，接口一直没往外给。
        2. 分组口径归一：肉蛋鱼 → 肉蛋、碳水 → 主食。这两组是早期 23 项基础食物的
           老组名，与后加的 288 项并存，页面上会出现两个语义重复的 tab。
        3. 食糜（Chyle）不进列表。它是消化中间物、不可选，只用来算胃排空，
           却在食物库里占了一个只有 1 项的 tab。
        """

        del kwargs
        group_alias = {"肉蛋鱼": "肉蛋", "碳水": "主食"}
        try:
            foods = []
            for item in ALL_FOODS.values() if isinstance(ALL_FOODS, dict) else ALL_FOODS:
                group = group_alias.get(item.group, item.group)
                if group == "消化中间物":
                    continue
                foods.append({
                    "key": item.key, "label": item.label, "group": group,
                    "portion": item.portion, "volume_ml": _r(item.volume_ml, 0),
                    "kcal": _r(item.kcal, 0), "carb_g": _r(item.carb_g, 1),
                    "protein_g": _r(item.protein_g, 1), "fat_g": _r(item.fat_g, 1),
                    "na_mmol": _r(item.na_mmol, 1), "alcohol_g": _r(item.alcohol_g, 1),
                    "caffeine_mg": _r(item.caffeine_mg, 0),
                    "fiber_g": _r(item.fiber_g, 1), "k_mg": _r(item.k_mg, 0),
                    "ca_mg": _r(item.ca_mg, 0), "fe_mg": _r(item.fe_mg, 1),
                    "vc_mg": _r(item.vc_mg, 1),
                    "selectable": bool(item.selectable),
                })
            def _pool_labels(pool: Any) -> list[str]:
                """候选池既可能是组合列表，也可能是单个食物 key，统一翻成中文。"""

                out: list[str] = []
                for entry in pool or []:
                    keys = [entry] if isinstance(entry, str) else list(entry)
                    # 2026-11-27（v1.4.0）：面板上用「米饭+黄焖鸡+紫菜汤」这种紧凑写法。
                    # 一屏要塞五个池子，"+ + +" 的空格纯属浪费横向空间。
                    text = combo_compact(keys)
                    if not text:
                        text = "、".join(FOODS[k].label for k in keys if k in FOODS)
                    out.append(text or str(entry))
                return out

            def _dedup(items: list[str]) -> list[str]:
                """饮品池靠重复来加权重（水×3、小口水×2），面板上别显示成「水、水、水」。"""
                seen: set[str] = set()
                out2: list[str] = []
                for it in items:
                    if it not in seen:
                        seen.add(it)
                        out2.append(it)
                return out2

            pools = {slot: _dedup(_pool_labels(pool)) for slot, pool in MEAL_POOLS.items()}
            pools["drink"] = _dedup(_pool_labels(DRINK_POOL))
            pools["snack"] = _dedup(_pool_labels(SNACK_POOL))
            return {"foods": foods, "pools": pools, "count": len(foods)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    # ===== v1.4.0：血液 / 免疫 / 营养 / 习惯 的只读接口 =====

    @staticmethod
    def _panel_row(field: str, label: str, unit: str, digits: int, value: float,
                   lo: float = None, hi: float = None, ref: str = "") -> dict[str, Any]:
        """拼一行指标。lo/hi 只用来决定配色（warn/good），不做任何计算。"""
        tone = "good"
        if lo is not None and value < lo:
            tone = "warn"
        if hi is not None and value > hi:
            tone = "warn"
        return {"field": field, "label": label, "unit": unit, "value": value,
                "ref": ref, "tone": tone}

    @API(name="webui_blood", description="血液与免疫：血容量分账、血红蛋白与红细胞压积、铁与血小板、白细胞三分类与炎症指标", version="1", public=False)
    async def webui_blood(self, **kwargs: Any) -> dict[str, Any]:
        """返回血液与免疫两组面板（只读）。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            def g(name: str, digits: int = 1) -> float:
                return _r(getattr(s, name, 0.0), digits)

            row = self._panel_row
            groups = [
                {"id": "volume", "title": "血容量", "subtitle": "血浆量与红细胞量分开记账",
                 "rows": [
                     row("blood_vol", "循环血量", "mL", 0, g("blood_vol"), ref="约 4000"),
                     row("plasma_vol", "血浆量", "mL", 0, g("plasma_vol"), ref="约 2400"),
                     row("rbc_mass", "红细胞量", "mL", 0, g("rbc_mass"), ref="约 1600"),
                     row("hct", "红细胞压积", "%", 1, g("hct"), 36.0, 46.0, "36~46"),
                     row("hb", "血红蛋白", "g/L", 1, g("hb"), 120.0, 150.0, "120~150"),
                 ]},
                {"id": "oxygen", "title": "携氧", "subtitle": "血红蛋白决定血氧含量",
                 "rows": [
                     row("cao2", "动脉血氧含量", "mL/dL", 1, g("cao2"), 16.0, 20.5, "17~20"),
                     row("do2", "氧输送", "mL/min", 0, g("do2"), 700.0, 1400.0, "700~1400"),
                     row("o2_deficit", "氧输送缺口", "0-1", 2, g("o2_deficit"), None, 0.05, "0 为正常"),
                 ]},
                {"id": "marrow", "title": "造血与铁", "subtitle": "促红素驱动、铁储备与吸收",
                 "rows": [
                     row("epo", "促红素驱动", "相对", 2, g("epo"), None, None, "1.0 为基线"),
                     row("retic_pct", "网织红细胞", "%", 1, g("retic_pct"), 0.5, 2.5, "0.5~2.5"),
                     row("iron_store", "储存铁", "mg", 0, g("iron_store"), 200.0, 1000.0, "200~1000"),
                     row("fe_intake_24h", "膳食铁", "mg/天", 1, g("fe_intake_24h"), 12.0, None, "≥18"),
                     row("fe_absorb_frac", "铁吸收分数", "0-1", 3, g("fe_absorb_frac"), 0.05, 0.40, "5%~40%"),
                 ]},
                {"id": "clot", "title": "止血", "subtitle": "血小板与凝血",
                 "rows": [
                     row("platelet", "血小板", "10^9/L", 0, g("platelet"), 150.0, 350.0, "150~350"),
                     row("clot", "凝血进程", "0-1", 2, g("clot"), None, 0.5, "0 为无出血"),
                     row("bleed_rate", "出血速率", "mL/min", 2, g("bleed_rate"), None, 0.0, "0 为无出血"),
                     row("menstrual_loss_ml", "本次经血", "mL", 0, g("menstrual_loss_ml"), None, None, "10~80"),
                 ]},
                {"id": "wbc", "title": "白细胞", "subtitle": "三类分开计数（细菌升中性粒、病毒升淋巴）",
                 "rows": [
                     row("wbc", "白细胞总数", "10^9/L", 2, g("wbc"), 4.0, 10.0, "4~10"),
                     row("neut", "中性粒细胞", "10^9/L", 2, g("neut"), 2.0, 7.5, "2.0~7.5"),
                     row("lymph", "淋巴细胞", "10^9/L", 2, g("lymph"), 1.0, 4.0, "1.0~4.0"),
                     row("mono", "单核细胞", "10^9/L", 2, g("mono"), 0.2, 0.8, "0.2~0.8"),
                 ]},
                {"id": "inflam", "title": "炎症与免疫张力", "subtitle": "IL-6 → CRP 两级延迟",
                 "rows": [
                     row("il6", "白细胞介素-6", "pg/mL", 1, g("il6"), None, 7.0, "<7"),
                     row("crp", "C 反应蛋白", "mg/L", 1, g("crp"), None, 10.0, "<10"),
                     row("infection", "感染负荷", "0-1", 2, g("infection"), None, 0.1, "0 为健康"),
                     row("immune_tone", "免疫张力", "0-1", 2, g("immune_tone"), 0.8, 1.35, "1.0 为基线"),
                     row("immunity_days", "感染后抵抗期", "天", 1, g("immunity_days"), None, None, ""),
                 ]},
            ]
            notes = []
            if str(getattr(s, "infection_type", "") or ""):
                notes.append("当前感染：%s / %s" % (
                    str(getattr(s, "infection_type", "")),
                    str(getattr(s, "infection_site", "") or "未定位")))
            if str(getattr(s, "bleed_site", "") or "") and float(getattr(s, "bleed_rate", 0.0) or 0.0) > 0.0:
                notes.append("出血中：%s（已 %.0f mL）" % (
                    str(getattr(s, "bleed_site", "")),
                    float(getattr(s, "bleed_done_ml", 0.0) or 0.0)))
            if str(getattr(s, "anemia", "none") or "none") != "none":
                notes.append("贫血分级：%s" % str(getattr(s, "anemia", "")))
            return {
                "ok": True,
                "clock": {"time": _clock(s.hour), "day": self._body_sim_day(),
                          "phase": phase(s.hour)},
                "groups": groups,
                "notes": notes,
                "temp": _r(getattr(s, "temp", 36.8), 2),
                "fever_drive": _r(getattr(s, "fever_drive", 0.0), 2),
                "sickness": _r(getattr(s, "sickness", 0.0), 2),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_nutrition", description="膳食营养结构与生活习惯：供能比、纤维与四种微量营养素、膳食质量分，以及 16 项生活习惯的判定结果", version="1", public=False)
    async def webui_nutrition(self, **kwargs: Any) -> dict[str, Any]:
        """返回营养面板与习惯判定（只读）。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "error": "身体尚未就绪"}
        try:
            s = self._s
            def g(name: str, digits: int = 1) -> float:
                return _r(getattr(s, name, 0.0), digits)

            row = self._panel_row
            rec = _nutrition.RECOMMEND
            groups = [
                {"id": "today", "title": "今日摄入", "subtitle": "跨零点清零",
                 "rows": [
                     row("nut_kcal", "能量", "kcal", 0, g("nut_kcal", 0),
                         None, None, "按需求量"),
                     row("nut_protein_g", "蛋白质", "g", 1, g("nut_protein_g"), None, None, "约 55"),
                     row("nut_fat_g", "脂肪", "g", 1, g("nut_fat_g"), None, None, ""),
                     row("nut_carb_g", "碳水", "g", 1, g("nut_carb_g"), None, None, ""),
                     row("nut_fiber_g", "膳食纤维", "g", 1, g("nut_fiber_g"),
                         rec["fiber_g"], None, "≥25"),
                     row("nut_na_mg", "钠", "mg", 0, g("nut_na_mg"), None,
                         rec["na_mg"], "<2000"),
                 ]},
                {"id": "micro", "title": "矿物质与维生素", "subtitle": "四项关键（类别画像估算）",
                 "rows": [
                     row("nut_k_mg", "钾", "mg", 0, g("nut_k_mg"), rec["k_mg"], None, "≥2000"),
                     row("nut_ca_mg", "钙", "mg", 0, g("nut_ca_mg"), rec["ca_mg"], None, "≥800"),
                     row("nut_fe_mg", "铁", "mg", 1, g("nut_fe_mg"), rec["fe_mg"], None, "≥18"),
                     row("nut_vc_mg", "维生素 C", "mg", 0, g("nut_vc_mg"), rec["vc_mg"], None, "≥100"),
                 ]},
                {"id": "structure", "title": "膳食结构", "subtitle": "供能比与质量分",
                 "rows": [
                     row("diet_score", "膳食质量分", "0-100", 1, g("diet_score"), 70.0, None, "≥70 为良好"),
                     row("protein_share", "蛋白供能比", "%", 1, _r(g("protein_share") * 100.0, 1),
                         10.0, 20.0, "10~20%"),
                     row("fat_share", "脂肪供能比", "%", 1, _r(g("fat_share") * 100.0, 1),
                         20.0, 30.0, "20~30%"),
                     row("carb_share", "碳水供能比", "%", 1, _r(g("carb_share") * 100.0, 1),
                         50.0, 65.0, "50~65%"),
                     row("junk_share", "垃圾食品占比", "%", 1, _r(g("junk_share") * 100.0, 1),
                         None, 30.0, "<30%"),
                 ]},
                {"id": "body", "title": "体重与能量平衡", "subtitle": "由累计能量盈亏派生（不参与生理计算）",
                 "rows": [
                     row("weight_kg", "体重", "kg", 1, g("weight_kg"), None, None, "参考值 55"),
                     row("bmi", "BMI", "", 1, g("bmi"), 18.5, 23.9, "18.5~23.9"),
                     row("energy_need_kcal", "估算需求", "kcal", 0, g("energy_need_kcal", 0), None, None, ""),
                     row("energy_balance_kcal", "累计能量盈亏", "kcal", 0,
                         g("energy_balance_kcal", 0), None, None, "0 为持平"),
                 ]},
            ]
            active = {h["id"]: h for h in _habits.judge(
                s, relax_stress_max=getattr(self._engine, "relax_stress_max", 35.0),
                aerobic_week_min=getattr(self._engine, "aerobic_week_min", 150.0),
                strength_week_sessions=getattr(self._engine, "strength_week_sessions", 2.0))}
            habits = []
            for hid, label, polarity, desc, proxy in _habits.HABITS:
                hit = active.get(hid)
                habits.append({
                    "id": hid, "label": label, "polarity": polarity,
                    "desc": desc, "proxy": proxy,
                    "active": hit is not None,
                    "strength": round(float(hit.get("strength", 0.0)), 3) if hit else 0.0,
                    "evidence": (hit or {}).get("evidence", ""),
                })
            return {
                "ok": True,
                "clock": {"time": _clock(s.hour), "day": self._body_sim_day(),
                          "phase": phase(s.hour)},
                "groups": groups,
                "score": _r(getattr(s, "diet_score", 0.0), 1),
                "flags": [x for x in str(getattr(s, "diet_flags", "") or "").split(",") if x],
                "habits": habits,
                "habits_active": [h["id"] for h in habits if h["active"]],
                "recommend": {k: v for k, v in _nutrition.RECOMMEND.items()},
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    @API(name="webui_intervene", description="对麦麦的身体做一次干预：喝水、进食、开始或停止运动", version="1", public=False)
    async def webui_intervene(self, kind: str = "drink", **kwargs: Any) -> dict[str, Any]:
        """执行一次行为干预。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"success": False, "error": "身体尚未就绪"}
        try:
            action = str(kind or "").strip()
            if action == "drink":
                self._engine._drink()
            elif action == "eat":
                self._engine._eat(self._engine._meal_slot(self._s.hour))
            elif action == "snack":
                self._engine._eat("snack")
            elif action == "exercise_on":
                self._s.exercising = True
            elif action == "exercise_off":
                self._s.exercising = False
            else:
                return {"success": False, "error": f"未知干预：{action}"}
            self._maybe_sample()
            return {"success": True, "action": action, "brief": brief(self._s),
                    "describe": describe(self._s)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    # ===== 给其他插件的公开 API（2026-11-15，醉酒插件请求）=====
    # 说明：@API(public=True) 才会被别的插件用 api.call 调到。
    # 只读接口不做任何副作用；写入接口复用 engine.feed，不新增生理模型。

    @API(name="drunk_snapshot", description="只读：醉酒相关的身体快照（血乙醇、睡眠、迟钝度、最近吃喝）", version="1", public=True)
    async def drunk_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        """只读快照，供醉酒/暧昧类插件做表达判断。纯读，不推进仿真、不落盘。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "version": 1, "error": "身体尚未就绪"}

        s = self._s
        bac = float(getattr(s, "bac", 0.0) or 0.0)
        # 迟钝度是推导值：血乙醇越高越迟钝（0~1）。BodyState 没有独立字段，
        # 这里只做单调映射，不引入新的生理模型；对端拿不到时可自行忽略。
        sluggish = min(1.0, max(0.0, bac / 0.8))
        return {
            "ok": True,
            "version": 1,
            "world_t": float(s.t),
            "wall_t": float(time.time()),
            "clock_offset_h": float(getattr(self._engine, "clock_offset", 0.0) or 0.0),
            "bac": round(bac, 4),
            "chyme_alcohol": round(float(getattr(s, "chyme_alcohol", 0.0) or 0.0), 3),
            "glucose": round(float(s.glucose), 2),
            "energy": round(float(s.energy), 2),
            "hunger": round(float(s.hunger), 2),
            "thirst": round(float(s.thirst), 2),
            "sleeping": bool(s.sleeping),
            "sleep_stage": str(getattr(s, "sleep_stage", "awake") or "awake"),
            "sleep_debt": round(float(getattr(s, "sleep_debt", 0.0) or 0.0), 2),
            # BodyState 目前没有断片模型：恒为 False，字段位保留给以后的版本。
            "passed_out": False,
            "passed_out_at": 0.0,
            "sluggish": round(sluggish, 3),
            "drinks": int(getattr(s, "drinks", 0) or 0),
            "last_drink_items": str(getattr(s, "last_drink_items", "") or ""),
            "last_meal_items": str(getattr(s, "last_meal_items", "") or ""),
            "last_meal_t": float(getattr(s, "last_meal_t", 0.0) or 0.0),
        }

    @API(name="log_drink", description="写入：把一次饮酒/饮水记进身体（幂等、支持份量）", version="1", public=True)
    async def log_drink(self, items: str = "", kind: str = "drink", portion: float = 1.0,
                        note: str = "", request_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """跨插件记账：她自己喝了一口、剧情里已经喝下，都走这里。

        复用 engine.feed（与 body_eat 工具同一条消化管线），不另算营养。
        认不出的词不报错，列进 unknown。
        """

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "version": 1, "error": "身体尚未就绪"}

        rid = str(request_id or "").strip()
        if rid and rid in self._drink_seen:
            return {"ok": True, "version": 1, "duplicate": True, "request_id": rid}

        try:
            p = float(portion)
        except (TypeError, ValueError):
            p = 1.0
        p = max(0.05, min(3.0, p))

        raw = [x.strip() for x in str(items or "").replace("，", ",").split(",") if x.strip()]
        resolved: list[str] = []
        unknown: list[str] = []
        for token in raw:
            if token in FOODS and getattr(FOODS[token], "selectable", True):
                resolved.append(token)
                continue
            guess = parse_menu(token, limit=4)
            if guess:
                resolved.extend(g for g in guess if g not in resolved)
            else:
                unknown.append(token)

        bac_before = float(getattr(self._s, "bac", 0.0) or 0.0)
        glucose_before = float(self._s.glucose)
        try:
            result = self._engine.feed(
                kind="drink", items=resolved or None, note=str(note or ""), portion=p
            )
        except Exception as exc:  # noqa: BLE001 - 跨插件接口不得把异常抛回去
            return {"ok": False, "version": 1, "error": f"{type(exc).__name__}: {exc}"[:200]}

        self._maybe_sample()
        if rid:
            self._drink_seen.append(rid)

        return {
            "ok": True,
            "version": 1,
            "request_id": rid,
            "accepted": list(result.get("items") or []),
            "unknown": unknown,
            "portion": p,
            "volume_ml": float(result.get("volume_ml") or 0.0),
            "kcal": float(result.get("kcal") or 0.0),
            "bac_before": round(bac_before, 4),
            "bac_after": round(float(getattr(self._s, "bac", 0.0) or 0.0), 4),
            "glucose_delta": round(float(self._s.glucose) - glucose_before, 2),
            "fallback": bool(result.get("fallback")),
        }

    @API(name="get_clock", description="只读：世界钟与宿主墙钟的换算", version="1", public=True)
    async def get_clock(self, **kwargs: Any) -> dict[str, Any]:
        """只读时钟。两套字段名同时给：醉酒插件用 world_t/offset_h，厨房用 offset_seconds/sim_time。"""

        del kwargs
        s = self._s if self._engine else None
        off_h = float(getattr(self._engine, "clock_offset", 0.0) or 0.0) if self._engine else 0.0
        rate = float(getattr(self.config.sim, "speed", 1.0) or 1.0)
        hour = float(getattr(s, "hour", 0.0) or 0.0) if s is not None else 0.0
        hh = int(hour) % 24
        mm = int(max(0.0, hour - int(hour)) * 60)
        return {
            "ok": True,
            "version": 1,
            "world_t": float(s.t) if s is not None else 0.0,
            "wall_t": float(time.time()),
            "offset_h": off_h,
            "speed": rate,
            # 厨房侧要的字段：同一份数据的另一种命名，只加不改
            "offset_seconds": round(off_h * 3600.0, 1),
            "sim_day": int(self._body_sim_day()) if self._engine else 1,
            "sim_time": "%02d:%02d" % (hh, mm),
            "rate": rate,
        }

    @API(name="get_state", description="只读：当前体征快照（供厨房等下游插件）", version="1", public=True)
    async def get_state(self, **kwargs: Any) -> dict[str, Any]:
        """只读体征。厨房做饭前后都用它判断「饿不饿 / 吃撑没有」。"""

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "version": 1, "error": "身体尚未就绪"}
        s = self._s
        hour = float(getattr(s, "hour", 0.0) or 0.0)
        hh = int(hour) % 24
        mm = int(max(0.0, hour - int(hour)) * 60)
        return {
            "ok": True,
            "version": 1,
            "sim_day": int(self._body_sim_day()),
            "clock": "%02d:%02d" % (hh, mm),
            "clock_offset_seconds": round(float(getattr(self._engine, "clock_offset", 0.0) or 0.0) * 3600.0, 1),
            "world_t": float(s.t),
            "hunger": round(float(s.hunger), 2),
            "thirst": round(float(s.thirst), 2),
            "energy": round(float(s.energy), 2),
            "health": round(float(s.health), 2),
            "glucose": round(float(s.glucose), 2),
            "bac": round(float(getattr(s, "bac", 0.0) or 0.0), 4),
            "gastric_volume_ml": round(float(s.gastric_volume), 1),
            "last_meal_items": str(getattr(s, "last_meal_items", "") or ""),
            "last_meal_ts": float(getattr(s, "last_meal_t", 0.0) or 0.0),
            "drinks": int(getattr(s, "drinks", 0) or 0),
            "sleeping": bool(s.sleeping),
            "stress": round(float(getattr(s, "stress", 0.0) or 0.0), 2),
            "depression": round(float(getattr(s, "depression", 0.0) or 0.0), 2),
            # 2026-11-18 v1.2.5：情绪三件套只读回传。
            # 此前只能写（adjust_mood）不能读，闭环断了一半 —— 外部写完
            # valence 拿不到当前状态。只加字段不改已有语义，符合契约。
            "euphoria": round(float(getattr(s, "euphoria", 0.0) or 0.0), 2),
            "dysphoria": round(float(getattr(s, "dysphoria", 0.0) or 0.0), 2),
            "mood_valence": round(float(getattr(s, "mood_valence", 0.0) or 0.0), 2),
            # ---- v1.4.0：血液 / 免疫 / 营养 / 习惯（只加字段，不改已有语义）----
            "blood_vol": round(float(getattr(s, "blood_vol", 0.0) or 0.0), 1),
            "hb": round(float(getattr(s, "hb", 0.0) or 0.0), 1),
            "hct": round(float(getattr(s, "hct", 0.0) or 0.0), 1),
            "anemia": str(getattr(s, "anemia", "none") or "none"),
            "platelet": round(float(getattr(s, "platelet", 0.0) or 0.0), 1),
            "iron_store": round(float(getattr(s, "iron_store", 0.0) or 0.0), 1),
            "wbc": round(float(getattr(s, "wbc", 0.0) or 0.0), 2),
            "neut": round(float(getattr(s, "neut", 0.0) or 0.0), 2),
            "lymph": round(float(getattr(s, "lymph", 0.0) or 0.0), 2),
            "crp": round(float(getattr(s, "crp", 0.0) or 0.0), 2),
            "infection": round(float(getattr(s, "infection", 0.0) or 0.0), 3),
            "infection_site": str(getattr(s, "infection_site", "") or ""),
            "fever_drive": round(float(getattr(s, "fever_drive", 0.0) or 0.0), 2),
            "diet_score": round(float(getattr(s, "diet_score", 0.0) or 0.0), 1),
            "diet_flags": str(getattr(s, "diet_flags", "") or ""),
            "weight_kg": round(float(getattr(s, "weight_kg", 0.0) or 0.0), 1),
            "bmi": round(float(getattr(s, "bmi", 0.0) or 0.0), 1),
            "habits": _habits.active_list(s),
        }

    @API(name="list_foods", description="只读：食物/饮品词表（含别名），供外部校验词能否解析", version="1", public=True)
    async def list_foods(self, **kwargs: Any) -> dict[str, Any]:
        """只读词表。同时给 items 与 foods 两套字段名，兼容两个下游插件。"""

        del kwargs
        items: list[dict[str, Any]] = []
        foods: list[dict[str, Any]] = []
        for item in (ALL_FOODS.values() if isinstance(ALL_FOODS, dict) else ALL_FOODS):
            row = {
                "key": item.key,
                "label": item.label,
                "group": item.group,
                "volume_ml": _r(item.volume_ml, 1),
                "kcal": _r(item.kcal, 1),
                "alcohol_g": _r(item.alcohol_g, 1),
                "selectable": bool(item.selectable),
            }
            items.append(row)
            foods.append({**row, "default_portion": str(getattr(item, "portion", "") or "")})
        aliases: dict[str, Any] = {}
        try:
            for entry in FOOD_ALIASES or []:
                word, keys = entry[0], list(entry[1])
                if not word or not keys:
                    continue
                aliases[str(word)] = keys[0] if len(keys) == 1 else keys
        except Exception:  # noqa: BLE001 - 别名表异常不得影响主接口
            aliases = {}
        return {
            "ok": True,
            "version": 1,
            "count": len(items),
            "items": items,
            "foods": foods,
            "aliases": aliases,
        }

    @API(name="log_meal", description="写入：外部插件代麦麦记账（按克数、幂等、部分成功）", version="1", public=True)
    async def log_meal(self, items: Any = None, kind: str = "meal", portion: float = 1.0,
                       note: str = "", request_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """跨插件记账：厨房做好了、或她自己吃了，都走这里。

        items 接受多种写法：
          - [{"key": "Tomato", "amount_g": 180}, {"label": "牛肉", "amount_g": 90}]
          - ["Tomato", "Rice"]
          - "番茄,米饭"
        amount_g 会按该食物 portion 字符串里的基准克数换算成比例；
        换不出来时退回 volume_ml 近似并标 estimated=True。
        """

        del kwargs
        empty = {"accepted": [], "rejected": [], "totals": {}, "delta": {}, "state": {}}
        if not self._ready or self._engine is None:
            return {"ok": False, "version": 1, "error": "身体尚未就绪", **empty}

        rid = str(request_id or "").strip()
        if rid and rid in self._meal_seen:
            return {"ok": True, "version": 1, "duplicate": True, "request_id": rid, **empty}

        try:
            p_total = max(0.05, min(3.0, float(portion)))
        except (TypeError, ValueError):
            p_total = 1.0
        feed_kind = str(kind or "meal").strip().lower()
        if feed_kind not in {"meal", "snack", "drink"}:
            feed_kind = "meal"

        entries = self._parse_meal_items(items)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        ratios: dict[str, float] = {}
        for entry in entries:
            token = str(entry.get("token") or "").strip()
            amount = entry.get("amount_g")
            key, label = self._resolve_food(token)
            if not key:
                rejected.append({"label": token, "reason": "unknown_food"})
                continue
            base = self._portion_base_grams(FOODS.get(key))
            ratio = 1.0
            estimated = amount is None
            if amount is not None:
                try:
                    ratio = max(0.05, min(20.0, float(amount) / max(1.0, base)))
                except (TypeError, ValueError):
                    ratio = 1.0
            ratio *= p_total
            ratios[key] = ratios.get(key, 0.0) + ratio
            accepted.append({
                "key": key, "label": label,
                "amount_g": amount,
                "ratio": round(ratio, 3),
                "estimated": estimated,
            })

        if not ratios:
            return {"ok": False, "version": 1, "error": "没有可记账的食材", **empty, "rejected": rejected}

        before_glucose = float(self._s.glucose)
        before_bac = float(getattr(self._s, "bac", 0.0) or 0.0)
        try:
            self._engine.feed(kind=feed_kind, items=list(ratios.keys()),
                              note=str(note or ""), portion=1.0, ratios=ratios)
        except Exception as exc:  # noqa: BLE001 - 跨插件接口不得把异常抛回去
            return {"ok": False, "version": 1,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "accepted": accepted, "rejected": rejected,
                    "totals": {}, "delta": {}, "state": {}}

        self._maybe_sample()
        if rid:
            self._meal_seen.append(rid)

        tot = self._engine._combo_totals_ratio(list(ratios.keys()), ratios)
        s = self._s
        return {
            "ok": True,
            "version": 1,
            "request_id": rid,
            "kind": feed_kind,
            "accepted": accepted,
            "rejected": rejected,
            "totals": {k: round(float(tot.get(k, 0.0) or 0.0), 2) for k in
                       ("kcal", "volume_ml", "carb_g", "protein_g", "fat_g",
                        "alcohol_g", "caffeine_mg", "water_ml", "na_mmol")},
            "delta": {
                "glucose_mmol": round(float(s.glucose) - before_glucose, 3),
                "bac_g_l": round(float(getattr(s, "bac", 0.0) or 0.0) - before_bac, 4),
            },
            "state": {
                "hunger": round(float(s.hunger), 1),
                "thirst": round(float(s.thirst), 1),
                "energy": round(float(s.energy), 1),
                "health": round(float(s.health), 1),
                "gastric_volume_ml": round(float(s.gastric_volume), 1),
                "glucose": round(float(s.glucose), 2),
            },
        }

    @API(name="adjust_mood", description="写入：小幅调整压力/心境（无 mood 字段，映射到 stress/depression）", version="1", public=True)
    async def adjust_mood(self, stress_delta: float = 0.0, stress_add: float = 0.0,
                          depression_delta: float = 0.0, depression_add: float = 0.0,
                          mood_delta: float = 0.0, valence: float = 0.0,
                          label: str = "", reason: str = "",
                          **kwargs: Any) -> dict[str, Any]:
        """两份请求的签名都收：厨房用 stress_delta/reason，醉酒用 stress_add/label。

        身体没有长期 mood 字段，只有 stress（压力）与 depression（累积应激）。
        mood_delta 按「心情好 = 压力降一半」保守映射，且每项夹在 ±20。

        2026-11-18 新增 valence：这是**瞬时**情绪场（-100..100），
        与 mood_delta 的区别是它会被引擎按 τ=6min（MOOD_TAU=360 s）自衰减回 0，
        因此适合"这一句话让我有点开心"这种一次性输入，而不是长期心境。
        NLP / 对话分析接的就是这个口子。
        """

        del kwargs
        if not self._ready or self._engine is None:
            return {"ok": False, "version": 1, "error": "身体尚未就绪"}

        def _num(v: Any) -> float:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return 0.0
            # 2026-11-18 修：float("nan")/float("inf") 不抛异常，会穿过后面的
            # min/max 钳位（NaN 被钳成 +100），一句话就能把情绪顶满。
            # 非有限值一律按 0 处理，等价于"这次请求不生效"。
            return f if math.isfinite(f) else 0.0

        ds = _num(stress_delta) or _num(stress_add)
        dd = _num(depression_delta) or _num(depression_add)
        dm = _num(mood_delta)
        if dm and not ds:
            ds = -dm * 0.5
        ds = max(-20.0, min(20.0, ds))
        dd = max(-20.0, min(20.0, dd))

        val = _num(valence)
        val = max(-100.0, min(100.0, val))

        s = self._s
        try:
            before_stress = float(getattr(s, "stress", 0.0) or 0.0)
            before_dep = float(getattr(s, "depression", 0.0) or 0.0)
            s.stress = max(0.0, min(100.0, before_stress + ds))
            s.depression = max(0.0, min(100.0, before_dep + dd))
            if val:
                # 累加而不是覆盖：一轮对话可能有多个分句各判一次情绪。
                before_val = float(getattr(s, "mood_valence", 0.0) or 0.0)
                s.mood_valence = max(-100.0, min(100.0, before_val + val))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "version": 1, "error": f"{type(exc).__name__}: {exc}"[:200]}

        return {
            "ok": True,
            "version": 1,
            "stress": round(float(s.stress), 2),
            "depression": round(float(s.depression), 2),
            "mood_valence": round(float(getattr(s, "mood_valence", 0.0) or 0.0), 2),
            "euphoria": round(float(getattr(s, "euphoria", 0.0) or 0.0), 2),
            "dysphoria": round(float(getattr(s, "dysphoria", 0.0) or 0.0), 2),
            "applied": {
                "stress_delta": round(ds, 2),
                "depression_delta": round(dd, 2),
                "mood_delta_mapped": round(dm, 2),
                "valence": round(val, 2),
            },
            "label": str(label or reason or "")[:80],
        }

    @staticmethod
    def _portion_base_grams(item: Any) -> float:
        """从食物条目的 portion 字符串里抠出基准克数/毫升数。

        「一碗 200 g 熟」→ 200；「250 mL 一杯」→ 250；「1 个」抠不出 → 退回 volume_ml。
        """

        try:
            text = str(getattr(item, "portion", "") or "")
        except Exception:  # noqa: BLE001
            text = ""
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|克|ml|mL|毫升)", text)
        if m:
            try:
                return max(1.0, float(m.group(1)))
            except (TypeError, ValueError):
                pass
        try:
            return max(1.0, float(getattr(item, "volume_ml", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 100.0

    @staticmethod
    def _resolve_food(token: str) -> tuple[str, str]:
        """把外部插件传来的词解析成食物 key；解析不出返回 ("", "")。"""

        t = str(token or "").strip()
        if not t:
            return "", ""
        if t in FOODS and getattr(FOODS[t], "selectable", True):
            return t, str(getattr(FOODS[t], "label", t))
        guess = parse_menu(t, limit=1)
        if guess:
            key = guess[0]
            return key, str(getattr(FOODS.get(key), "label", key))
        return "", ""

    def _parse_meal_items(self, items: Any) -> list[dict[str, Any]]:
        """把 items 归一成 [{"token": str, "amount_g": float|None}]。"""

        out: list[dict[str, Any]] = []
        raw = items
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",")]
        elif isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return out
        for entry in raw:
            if isinstance(entry, dict):
                token = str(entry.get("key") or entry.get("label") or entry.get("name") or "").strip()
                amount = entry.get("amount_g", entry.get("grams", entry.get("g")))
                try:
                    amount = float(amount) if amount is not None else None
                except (TypeError, ValueError):
                    amount = None
                out.append({"token": token, "amount_g": amount})
            else:
                out.append({"token": str(entry or "").strip(), "amount_g": None})
        return out

    # ===== 给 LLM 的工具 =====

    # ===== 联动：身体状态注入对话 =====

    def _conflict_note(self) -> str:
        """检测「胃空但不饿」与其他来源描述冲突时，生成一句校正。

        2026-11-19 新增。为什么只动自己：
          日程插件（麦生活）自己维护 hunger 与 health_note，它的 note 在
          hunger > 88 时写「有些饿，胃里空空的」。我们改不了别的插件，
          也不该要求它反向适配，只能在自己注入的这一段里把话说清。

        为什么只在冲突时说话：
          正常状态下这一段返回空串、不注入 —— 不冲突时补一句「我胃空但不饿」
          纯属占上下文，还会让模型频繁播报身体。

        判据（三者同时成立才算冲突）：
          (a) 开关打开；
          (b) 胃内容物低于 gastric_idle_ml（真的排空了）；
          (c) 饥饿值低于 hunger_floor（身体并不觉得饿）。
        反过来「胃空 + 确实饿」是一致的，不校正。
        """

        cfg = getattr(self.config, "conflict", None)
        if cfg is None or not bool(getattr(cfg, "enabled", False)):
            return ""

        s = self._s
        try:
            gastric = float(getattr(s, "gastric_volume", 0.0) or 0.0)
            hunger = float(getattr(s, "hunger", 0.0) or 0.0)
        except (TypeError, ValueError):
            return ""

        idle_ml = float(getattr(cfg, "gastric_idle_ml", 150.0) or 150.0)
        floor = float(getattr(cfg, "hunger_floor", 65.0) or 65.0)
        if gastric > idle_ml or hunger >= floor:
            return ""

        # 只在「离上一餐够久」时说：刚吃完就说胃空是自相矛盾的。
        _meal = recent_meal_text(s)
        if _meal:
            return ""

        # 只陈述事实，不写禁止式指令：模型需要知道"胃空但不饿"，
        # 不需要被告知"不能说什么"。79 字的长版会稀释注意力，且与
        # _body_prompt_note() 结尾那句"说到自己状态时请以此为准"重复。
        return CONFLICT_NOTE_TEXT

    def _body_prompt_note(self) -> str:
        """把此刻的身体压成一两句自然语言，供注入对话上下文。

        只取会影响说话的几项，不倾倒全部 66 个字段：注入的是给她看的提示，
        不是监看屏。健康、饮食与精力三项用词刻意保留口语感。
        """

        s = self._s
        parts: list[str] = []

        if s.health < 40:
            parts.append(f"身体很虚（健康 {s.health:.0f}/100）")
        elif s.health < 70:
            parts.append(f"身体有点乏（健康 {s.health:.0f}/100）")

        if s.glucose < 3.9:
            parts.append(f"血糖偏低（{s.glucose:.1f} mmol/L），反应会迟钝些")
        elif s.glucose > 7.8:
            parts.append(f"血糖偏高（{s.glucose:.1f} mmol/L）")

        # 最近一餐：这是最常被问到的一项，超过时限就当没这回事，免得半夜
        # 还被当成"刚吃完"。2026-11-19 起判定统一走 translate.recent_meal_text，
        # 与 describe() 用同一个函数，避免两处口径再次分叉。
        since_meal = (s.t - s.last_meal_t) / H2S
        _meal = recent_meal_text(s)
        if _meal:
            parts.append(_meal)

        fasted = since_meal
        if s.hunger > 65:
            parts.append(f"饿得厉害（{fasted:.0f} 小时没吃东西了）")
        elif s.hunger > 45:
            parts.append("有点饿")

        if s.thirst > 55:
            parts.append("口渴")

        if s.energy < 45:
            parts.append(f"精力不济（{s.energy:.0f}/100）")

        # 情绪：只在偏离正常带时才说。正常时一句"心情平稳"只会占注意力。
        # 2026-11-19 修：阈值不再各写一套，统一取 translate.NORMAL_BAND；
        # 且欣快/烦躁改成两个独立 if（原来是 if/elif 只报一个）——
        # 真实情绪允许"又兴奋又焦躁"，混在一起看比强行二选一更直观。
        if getattr(s, "euphoria", 0.0) > NORMAL_BAND["euphoria"][1]:
            parts.append(f"心情很好，有点兴奋（{s.euphoria:.0f}/100）")
        if getattr(s, "dysphoria", 0.0) > NORMAL_BAND["dysphoria"][1]:
            parts.append(f"有点烦躁（{s.dysphoria:.0f}/100）")
        if s.depression > NORMAL_BAND["depression"][1]:
            parts.append(f"情绪低落（{s.depression:.0f}/100）")

        if s.hospital_until > s.t:
            left = (s.hospital_until - s.t) / H2S
            parts.append(f"正在住院（{s.hospital_reason or '急救中'}，约 {left:.1f} 小时后出院）")

        # ---- v1.4.0：免疫 / 血液 / 生活习惯 ----
        # 只在"有话说"时出现，三行合起来也不到 40 字，不占注意力。
        sickness = float(getattr(s, "sickness", 0.0) or 0.0)
        infection = float(getattr(s, "infection", 0.0) or 0.0)
        if infection >= 0.20 or sickness >= 0.25:
            _site = str(getattr(s, "infection_site", "") or "身体不舒服")
            _temp = float(getattr(s, "temp", 36.8) or 36.8)
            parts.append(f"在生病（{_site}，体温 {_temp:.1f} ℃），没精神、也没什么胃口")
        hb = float(getattr(s, "hb", 0.0) or 0.0)
        if 0.0 < hb < 112.0:
            parts.append(f"有点贫血（血红蛋白 {hb:.0f} g/L），容易累、上楼会喘")
        if self._habits_inject:
            _labels = {h[0]: h[1] for h in _habits.HABITS}
            _ids = _habits.active_list(s)
            if _ids:
                _shown = [_labels.get(i, i) for i in _ids[:4]]
                parts.append("最近的生活状态：" + "、".join(_shown))
        # ---- 联动侧的"下一步 / 心情参考 / 是否适合打扰"（v1.3.1）----
        # 只有联动开启、且对方给了数据时才出现；一律不写进身体状态。
        _link = self._link
        if _link is not None and getattr(self.config.link, "enabled", False):
            _data = getattr(_link, "_cache", None) or {}
            _nxt = _data.get("next_plan") if isinstance(_data, dict) else None
            if isinstance(_nxt, dict) and str(_nxt.get("title") or ""):
                _start = str(_nxt.get("start") or "")
                _hhmm = ""
                try:
                    _dt = datetime.fromisoformat(_start)
                    _hhmm = f"{_dt.hour:02d}:{_dt.minute:02d} "
                except ValueError:
                    _hhmm = ""
                _place = str(_nxt.get("location") or "")
                _when = (_hhmm + "的") if _hhmm else ""
                parts.append(f"日程上{_when}下一步是「{_nxt.get('title')}」"
                             + (f"，在{_place}" if _place else ""))
            _beh = _data.get("behavior") if isinstance(_data, dict) else None
            if isinstance(_beh, dict) and str(_beh.get("decision") or "") == "defer":
                _why = {"sleeping": "在睡", "busy": "在忙", "farewell": "在道晚安"}.get(
                    str(_beh.get("reason") or ""), "不太方便")
                parts.append(f"日程那边说她{_why}，这会儿别打扰")
            _mood_ref = _data.get("mood_ref") if isinstance(_data, dict) else None
            try:
                if _mood_ref is not None and abs(float(_mood_ref)) >= 10.0:
                    _m = float(_mood_ref)
                    parts.append(("日程那边记的心情偏" + ("好" if _m > 0 else "差"))
                                 + f"（{_m:+.0f}/100，仅供参考）")
            except (TypeError, ValueError):
                pass

        if not parts:
            return ""
        return (
            "【你此刻的身体】"
            + "；".join(parts)
            + "。说到自己状态时请以此为准，不要夸大也不要隐瞒。"
        )

    @HookHandler(
        "chat.receive.before_process",
        name="body_wake_on_message",
        description="入站消息到达：睡着时把她叫醒，或按配置直接拦下消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_incoming_message(self, **kwargs: Any) -> dict[str, Any] | None:
        """消息到达时的睡眠联动。

        两件事：
          1. 睡着且开启了「消息唤醒」→ 叫醒她（唤醒后需连续安静若干分钟才重新入睡）
          2. 睡着且开启了「睡眠中不响应」→ 中止这条消息，不让它进入后续链路

        联动开启时这两项都交给麦生活的日程决定，本钩子不介入。
        """

        if not self._ready or self._engine is None:
            return None

        sleep_cfg = getattr(self.config, "sleep", None)
        if sleep_cfg is None:
            return None

        # 联动开启 → 以麦生活日程为准，本钩子不做任何拦截或唤醒
        link_on = bool(getattr(self.config.link, "enabled", False))

        s = self._s
        if not s.sleeping and not s.wake_pending:
            return None

        # 1) 拦截：睡眠中不响应消息
        if (not link_on) and bool(getattr(sleep_cfg, "block_while_asleep", False)):
            if s.sleeping:
                self._get_logger().info("睡眠中拦下一条消息（睡眠中不响应已开启）")
                return {
                    "action": "abort",
                    "reason": "麦麦正在睡觉，不处理消息",
                }

        # 2) 唤醒：来消息叫醒她
        if (not link_on) and bool(getattr(sleep_cfg, "wake_on_message", False)):
            if s.sleeping:
                self._engine.wake_by_message()
                self._get_logger().info("麦麦被消息唤醒，%.0f 分钟内保持清醒",
                                        float(getattr(sleep_cfg, "wake_hold_minutes", 10.0)))

        return None

    @HookHandler(
        "maisaka.replyer.after_response",
        name="body_emotion_nlp",
        description="分析麦麦自己说的话，把情绪写进身体（不消耗 token、不联网）",
        mode=HookMode.OBSERVE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_replyer_emotion(self, **kwargs: Any) -> None:
        """情绪自评入口（v1.2.5）。

        为什么挂在 replyer.after_response：
          这是「麦麦自己刚说完一句话」的唯一时机，且载荷里带 response 正文。
        为什么用 OBSERVE：
          只读不改，绝不碰回复内容 —— 判定失败也要让她正常说话。
        为什么异步且带防抖：
          连续短消息（"嗯""哦"）会在几秒内连着触发，不防抖会把情绪叠爆。
        """
        if not self._ready or self._engine is None:
            return None
        cfg = getattr(self.config, "emotion", None)
        if cfg is None or not bool(getattr(cfg, "enabled", False)):
            return None

        text = str(kwargs.get("response") or "")
        if not text:
            return None

        now = time.time()
        gap = max(0.0, float(getattr(cfg, "cooldown_seconds", 3.0) or 0.0))
        if now - self._emotion_last_at < gap:
            return None

        try:
            r = _nlp_analyze(
                text,
                scale=float(getattr(cfg, "scale", 1.0) or 1.0),
                min_chars=int(getattr(cfg, "min_chars", 4) or 4),
                max_valence=float(getattr(cfg, "max_valence", 60.0) or 60.0),
            )
        except Exception as exc:  # noqa: BLE001 - 判定失败不能影响聊天
            self._get_logger().debug("情绪判定异常：%s: %s", type(exc).__name__, exc)
            return None

        self._emotion_last_at = now
        if not r.get("hit"):
            return None

        s = self._s
        try:
            before = float(getattr(s, "mood_valence", 0.0) or 0.0)
            after = max(-100.0, min(100.0, before + float(r["valence"])))
            s.mood_valence = after
            self._emotion_hits += 1
            # 2026-11-18 修：第三项原先误传 label（与第一项重复），日志看不出
            # "哪句话被判成什么"，无法按文档建议复盘判定质量。改为回显原句片段。
            snippet = " ".join(text.split())[:24]
            self._get_logger().info(
                "情绪自评：%s -> valence %+.1f（原句「%s」，累计 %d 次）",
                r.get("label", "?"), float(r["valence"]), snippet, self._emotion_hits,
            )
        except Exception as exc:  # noqa: BLE001
            self._get_logger().debug("情绪写入异常：%s: %s", type(exc).__name__, exc)
        return None

    @HookHandler(
        "maisaka.planner.before_request",
        name="body_context",
        description="把当前身体状态与体感冲突校正注入对话上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_body_context(self, **kwargs: Any) -> dict[str, Any] | None:
        """在麦生活请求模型前注入身体状态。

        只改 items 里的 system text，不碰工具定义与历史，避免破坏载荷结构。
        开关关闭或没有值得说的内容时原样放行。
        """

        if not self._ready or self._engine is None:
            return None

        # 2026-11-19：两段注入的开关互相独立。
        #   note  —— 常规体征注入，受 link.enabled 且 link.inject_state 控制；
        #   fix   —— 体感冲突校正，只受 [conflict].enabled 控制。
        # 早前这里是一道共同的前置判断，若沿用会让「关掉体征注入」时
        # 连带关掉冲突校正，而后者恰恰是用来弥补没注入时的信息缺口的。
        note = ""
        if self.config.link.enabled and self.config.link.inject_state:
            note = self._body_prompt_note()
        fix = self._conflict_note()

        merged = "\n\n".join(x for x in (note, fix) if x)
        if not merged:
            return None
        note = merged

        items = kwargs.get("items")
        if not isinstance(items, list):
            # 载荷契约变了就放弃注入，宁可不说也不要改坏请求
            return None

        try:
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                if item.get("item_type") != "SystemMessageItem":
                    continue
                parts = item.get("parts")
                if not isinstance(parts, list):
                    item["parts"] = [{"type": "text", "text": note}]
                    return {"action": "continue", "modified_kwargs": kwargs}
                if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == "text":
                    parts[-1]["text"] = (parts[-1].get("text") or "") + "\n\n" + note
                else:
                    parts.append({"type": "text", "text": note})
                return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # noqa: BLE001 - 注入失败按未注入处理
            self._get_logger().warning("身体状态注入失败：%s: %s", type(exc).__name__, exc)
            return None

        return None

    @Tool(
        "body_state",
        description="查询麦麦此刻的身体状态：体征、体感、最近的活动与吃下去的东西",
        brief_description="查麦麦的身体",
        detailed_description=(
            "返回一段可以直接读的身体自述和一行紧凑摘要。"
            "适合在聊天里关心对方身体时调用；"
            "被问到「你吃了什么」「饿不饿」「身体怎么样」时都应该调它。"
        ),
        parameters=[
            ToolParameterInfo(name="detail", param_type=ToolParamType.BOOLEAN,
                              description="是否返回详尽版自述", required=False, default=True),
        ],
        # 2026-11-11：宿主 src/plugin_runtime/component_query.py 的
        # _get_tool_visibility() 对没写 visibility 的插件工具一律判 deferred，
        # deferred 不进模型常驻工具表，只能靠内置 tool_search 主动搜到。
        # 服务器日志里 body_eat 出现 0 次就是这么来的 —— 模型根本不知道有这工具。
        visibility="visible",
    )
    async def handle_body_state(self, detail: bool = True, **kwargs: Any) -> dict[str, Any]:
        """返回身体状态文本。"""

        del kwargs
        if not self._ready:
            return {"name": "body_state", "content": "身体还没启动。"}
        s = self._s
        text = describe(s, detail=bool(detail))
        return {"name": "body_state", "content": f"{text}\n（摘要：{brief(s)}）"}

    @Tool(
        "body_eat",
        # 2026-11-19 修（issue #12）：原描述开头是「记录一次刚刚发生的吃喝」，
        # 把「已经吃完」当成了调用前提。用户说「给你点份外卖」时，模型判定
        # 「还没发生」而不调用，于是投喂彻底失效（RPC 日志里 body_eat 出现 0 次）。
        # 现改为「下单即记账」，并点明不必等她说吃完。
        description=("记录一次投喂：用户给她点/买/递吃喝，或叫她吃、叫她喝时调用。"
                     "「给你点份外卖」「买了奶茶」都算，下单就记，不必等她吃完"),
        brief_description="记录新的吃喝",
        detailed_description=(
            "把一次真实发生的吃喝落进身体模型：血糖、胃排空、水分都会真的变化。"
            "在用户表示要给她吃喝的那一刻就调用：点外卖、买了、递给她、叫她吃"
            "（「给你点了外卖」「给你点份外卖」「陪我喝半杯白酒」）。"
            "**不必等她说已经吃完** —— 下单与递到手里都算投喂发生，先记上，"
            "身体会按真实消化过程自己推进。"
            "被问「你吃了什么」「喝水了吗」这类事后询问，只是把已有记录陈述出来，"
            "不要再调本工具——身体里的那件事要么来自日程自主进食，要么来自刚才那次投喂，"
            "已经记过了，再记一次就是凭空多出一份。"
            "kind 用 meal（一顿饭）/ snack（零食）/ drink（喝的）；"
            "items 写具体食物名；drink 留空只会按白水记，想记准就写明是什么。"
        ),
        parameters=[
            ToolParameterInfo(name="kind", param_type=ToolParamType.STRING,
                              description="meal 一顿饭 / snack 零食 / drink 喝的",
                              required=True, default="snack"),
            ToolParameterInfo(name="items", param_type=ToolParamType.STRING,
                              description=("具体食物，逗号分隔。中英文都认："
                                           "「米饭,红烧肉,汤」或 Rice,Pork,Soup；"
                                           "留空则按当前餐段自动配一份"),
                              required=False, default=""),
            ToolParameterInfo(name="note", param_type=ToolParamType.STRING,
                              description="这次吃喝的由来，如「用户点的外卖」",
                              required=False, default=""),
        ],
        # PR #8：feeding 关闭时交给宿主按 deferred 处理 —— 名字与短描述仍可被
        # tool_search 发现，但参数 schema 不再占每次请求的常驻工具表。
        # 注意：可见性在类定义期按 config.toml 求值，改完 [feeding] 需重载插件。
        visibility=_BODY_EAT_VISIBILITY,
    )
    async def handle_body_eat(self, kind: str = "snack", items: str = "",
                              note: str = "", **kwargs: Any) -> dict[str, Any]:
        """把一次吃喝落进身体。"""

        # 2026-11-19 修：原先这里在 trust_private_only 打开时**无条件拒绝**，
        # 理由是「宿主不提供来源标记」。实测不然：MaiBot 的 @Tool 处理器会收到
        # session_id / group_id / user_id / platform（同生态插件
        # xiaocutedog_maibot_private_chat_tool_plus 与 local_message-debounce 都这么判）。
        # 现按真实来源判定：有 group_id 或会话名含 _group_ 即群聊，否则为私聊
        # （与主程序一致：无群信息按私聊处理）。
        gid = str(kwargs.get("group_id") or "").strip()
        sid = str(kwargs.get("session_id") or kwargs.get("chat_id")
                  or kwargs.get("stream_id") or "").strip()
        is_group = bool(gid) or ("_group_" in sid) or sid.startswith("group:")
        del kwargs
        if self.config.feeding.trust_private_only and is_group:
            return {
                "name": "body_eat",
                "content": ("只在私聊里接受投喂：当前是群聊，这次没有记。"
                            "想让她在群里也能吃，把「仅私聊喂食」关掉即可。"),
            }
        if not self.config.feeding.enabled:
            return {"name": "body_eat", "content": "当前没有开启聊天喂食。"}
        if not self._ready or self._engine is None:
            return {"name": "body_eat", "content": "身体还没启动。"}

        feed_kind = str(kind or "snack").strip().lower()
        if feed_kind not in {"meal", "snack", "drink"}:
            return {
                "name": "body_eat",
                "content": "喂食类型无效，只支持 meal、snack 或 drink。",
            }

        keys = [k.strip() for k in str(items or "").replace("，", ",").split(",") if k.strip()]
        # 中英文都认：先按 key 精确匹配，认不出的交给 food.parse_menu 按中文
        # 别名解析（「米饭」-> Rice、「红烧肉」-> Pork）。模型更常写中文，
        # 旧实现只认英文 key，一句「点了黄焖鸡米饭」会直接报"食物库里没有"。
        resolved: list[str] = []
        unknown: list[str] = []
        for k in keys:
            if k in FOODS and FOODS[k].selectable:
                resolved.append(k)
                continue
            guess = parse_menu(k, limit=4)
            if guess:
                resolved.extend(g for g in guess if g not in resolved)
            else:
                unknown.append(k)
        # 认不出的（"红烧肉盖饭""番茄炒蛋"这种复合菜名），交给 utils 模型
        # 拆成基础食材。旧实现到这里直接报"食物库里没有"，模型在聊天里
        # 报一个菜名就整次喂食失败 —— 用户反馈"点了外卖没进进食记录"。
        # 只认出一样时也要拆：模型报「番茄炒蛋」，关键词只给 Egg，缺主食。
        # 但「香蕉」「牛奶」这类本身就是一样东西，不拆（拆了反而可能被模型改坏）。
        thin = len(resolved) == 1 and not is_plain_food(keys[0] if keys else "")
        if unknown or thin:
            merged: list[str] = list(resolved)
            # 认不出时按单个菜名拆；只认出一样时按整句拆，让模型补主语/主食。
            targets = unknown if unknown else ["、".join(keys)]
            for name in targets:
                got, src = await decompose_menu(name, limit=4, llm=self.ctx.llm)
                if src == "llm" and got:
                    for g in got:
                        if g not in merged:
                            merged.append(g)
                    self._get_logger().info(
                        "喂食菜名经 utils 拆解：%s -> %s", name, combo_summary(got)
                    )
                elif not merged:
                    # 一样都没认出来才整单拒；已经认出的部分不能作废。
                    return {
                        "name": "body_eat",
                        "content": (
                            f"食物库里没有「{name}」，模型也没拆出可用的食材。"
                            f"可以直接说基础食材，例如：米饭,猪肉,汤"
                        ),
                    }
            resolved = merged
        keys = resolved

        try:
            result = self._engine.feed(
                kind=feed_kind, items=keys or None, note=str(note or "")
            )
        except Exception as exc:  # noqa: BLE001
            return {"name": "body_eat", "content": f"没吃成：{type(exc).__name__}: {exc}"[:200]}

        if not result.get("ok"):
            return {"name": "body_eat", "content": str(result.get("reason") or "没吃成")}

        self._maybe_sample()
        # 回话给模型看的是中文菜名，不是内部 key（旧实现回 "Rice、Pork、Soup"，
        # 模型转述给用户时会显得像在对代码说话）。
        eaten = combo_summary(result.get("items") or []) or "、".join(result.get("items") or [])
        # drink 没点名具体饮品时引擎按白水记（不再随机抽），这里必须如实说
        # "记的是水"，否则模型会以为记上了自己说的那杯，身体与事实又脱节。
        if result.get("fallback"):
            return {
                "name": "body_eat",
                "content": (
                    f"记下了：{eaten}（你没说具体是哪种饮品，先按白水记）。"
                    f"想记准就再调一次并写明 items，例如「白酒」「可乐」。"
                ),
            }
        return {
            "name": "body_eat",
            "content": (
                f"吃下了：{eaten}。"
                f"现在血糖 {self._s.glucose:.1f} mmol/L，胃内容物 {self._s.gastric_volume:.0f} mL。"
            ),
        }

    @Command("body", description="查看麦麦当前的身体状态", pattern=r"^/body$")
    async def handle_body_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """聊天里用 /body 查身体：先出一张状态图，出不了图就退回文字。"""

        del kwargs
        if not self._ready:
            return False, "身体还没启动", True
        if await self._send_body_card(stream_id):
            return True, "已发送身体状态图", True
        await self.ctx.send.text(describe(self._s), stream_id)
        return True, "已发送身体状态", True

    async def _send_body_card(self, stream_id: str) -> bool:
        """把当前快照渲染成 PNG 并发出去。任何一步失败都返回 False，由调用方走文字。"""

        if not stream_id:
            return False
        try:
            html_text = build_html(
                self._s,
                cycle_enabled=bool(getattr(self.config.cycle, "enabled", False)),
            )
        except Exception as exc:  # 快照缺字段等构造期问题
            self.ctx.logger.warning("身体卡片 HTML 构建失败，退回文字: %s", exc)
            return False
        try:
            result = await self.ctx.render.html2png(
                html_text,
                selector="#card",
                viewport={"width": 780, "height": 900},
                device_scale_factor=2.0,
            )
        except Exception as exc:
            self.ctx.logger.warning("身体卡片渲染失败，退回文字: %s", exc)
            return False
        if not isinstance(result, dict):
            self.ctx.logger.warning("身体卡片渲染返回形态异常 %r，退回文字", type(result).__name__)
            return False
        if result.get("success") is False:
            self.ctx.logger.warning("身体卡片渲染被拒: %s，退回文字", result.get("error"))
            return False
        image_base64 = str(result.get("image_base64") or "").strip()
        if not image_base64:
            self.ctx.logger.warning("身体卡片渲染结果为空，退回文字")
            return False
        try:
            if await self.ctx.send.image(image_base64, stream_id):
                return True
        except Exception as exc:
            self.ctx.logger.warning("身体卡片发送失败，退回文字: %s", exc)
        return False

    @HomeCard(
        "lifebody_card",
        title="麦麦此刻的身体",
        description="实时体征与体感，点击进入「麦麦身体」工作区",
        content=[
            {"type": "markdown", "content": "身体正在后台以 10 秒一步的节奏持续运转。"},
            {"type": "actions", "actions": [{"label": "打开麦麦身体", "url": "/plugin-page?page=now"}]},
        ],
        width="medium",
        order=200,
    )
    async def home_card(self) -> None:
        """首页卡片占位，内容由 WebUI 拉取。"""

        return None


def create_plugin() -> LifeBodyPlugin:
    """创建插件实例。"""

    return LifeBodyPlugin()