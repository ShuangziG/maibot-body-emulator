"""麦麦身体插件的配置模型。"""

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置版本，请勿手动修改",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )


class SimConfig(PluginConfigBase):
    """仿真参数。"""

    __ui_label__ = "仿真"
    __ui_icon__ = "activity"
    __ui_order__ = 1

    dt: float = Field(
        default=10.0,
        description="单步仿真秒数；实测 30 秒以上数值发散，不要超过 20",
        json_schema_extra={"label": "仿真步长（秒）"},
    )
    speed: float = Field(
        default=1.0,
        description="时间流速倍率，1.0 表示与现实同步",
        json_schema_extra={"label": "时间流速倍率"},
    )
    follow_system_clock: bool = Field(
        default=True,
        description="用系统时间校准身体钟点；不依赖麦生活，关掉后钟点只按仿真时长推进（会越来越偏）",
        json_schema_extra={"label": "跟随系统时钟"},
    )
    catch_up_max_hours: float = Field(
        default=8.0,
        description="启动时最多补算多少小时的离线时间",
        json_schema_extra={"label": "离线补算上限（小时）"},
    )
    autosave_seconds: float = Field(
        default=30.0,
        description="自动存档间隔（现实秒），0 表示不自动存档",
        json_schema_extra={"label": "自动存档间隔（秒）"},
    )
    history_limit: int = Field(
        default=2200,
        description="内存中保留的采样点数量，每 60 仿真秒一个点",
        json_schema_extra={"label": "采样点保留数量"},
    )
    seed: int = Field(
        default=20261106,
        description="随机种子，决定每天的食谱；改动会改变进食记录",
        json_schema_extra={"label": "随机种子"},
    )


# 设计说明（不上屏，仅维护者可见）：
# 睡眠窗口与唤醒行为都在这里调。夜里被消息唤醒后，需要连续安静一段时间
# 才会重新入睡（见 wake_hold_minutes），避免一条消息就把整夜睡眠打断。
class SleepConfig(PluginConfigBase):
    """睡眠。默认23:00-7:00，被吵醒需要重新入睡。"""

    __ui_label__ = "睡眠"
    __ui_icon__ = "moon"
    __ui_order__ = 2

    start_hour: float = Field(
        default=23.0,
        description="每晚几点开始该睡（0-24 的小时数，可带小数如 23.5 表示 23:30）",
        json_schema_extra={"label": "入睡时刻", "order": 0},
    )
    end_hour: float = Field(
        default=7.0,
        description="每晚几点醒来（0-24 的小时数）。窗口可以跨零点",
        json_schema_extra={"label": "醒来时刻", "order": 1},
    )
    wake_on_message: bool = Field(
        default=True,
        description=(
            "有人发消息就把她叫醒。关闭后消息不会打断睡眠；"
            "开启麦生活联动时不生效（以麦生活的日程为准）"
        ),
        json_schema_extra={"label": "消息唤醒", "order": 2},
    )
    wake_hold_minutes: float = Field(
        default=10.0,
        description=(
            "被消息唤醒后，要连续安静多少分钟才重新入睡。"
            "期间再来消息会重新计时；重新入睡一律从浅睡开始"
        ),
        json_schema_extra={"label": "重新入睡等待（分钟）", "order": 4},
    )
    block_while_asleep: bool = Field(
        default=False,
        description=(
            "睡着时完全不响应任何消息（消息被拦下，不进入后续处理）。"
            "开启麦生活联动时该开关无效——那种情况以麦生活的日程为准"
        ),
        json_schema_extra={"label": "睡眠中不响应消息", "order": 3},
    )


# 设计说明（不上屏，仅维护者可见）：
# 双相位机，默认关闭。关闭时不推进周期、不产生疼痛，WebUI 与 /body 卡片
# 也不显示任何相关字段 —— 这套机制对不需要它的使用者来说是纯噪音。
# 依据 LIFE.apk 原作的双相位：MenstrualPhase 与 UterusPhase 两套并列枚举。
# 「上次月经第一天」是现实日历锚点，填了就按日历算相位，留空则从启用那刻累加。
class CycleConfig(PluginConfigBase):
    """月经周期。默认关闭，关闭时不产生任何效果。"""

    __ui_label__ = "月经周期"
    __ui_icon__ = "calendar-heart"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description=(
            "启用月经周期双相位机（卵泡/排卵/黄体 + 增殖/分泌/行经）。"
            "开启后会产生雌激素与孕激素的周期曲线，并可能引起痛经、PMS；"
            "关闭时这些都不发生，界面也不显示相关条目"
        ),
        json_schema_extra={"label": "启用月经周期", "order": 0, "group": "基本"},
    )
    cycle_length: float = Field(
        default=28.0,
        description="个人周期长度（天）。低于 18 或高于 45 会被限制到该范围",
        json_schema_extra={"label": "周期长度（天）", "order": 2, "group": "基本"},
    )
    period_length: float = Field(
        default=5.0,
        description="行经持续天数",
        json_schema_extra={"label": "行经天数", "order": 3, "group": "基本"},
    )
    pain_enabled: bool = Field(
        default=True,
        description=(
            "行经时是否产生痛经。关闭后仍有激素周期与相位变化，但不会有疼痛"
        ),
        json_schema_extra={"label": "产生痛经", "order": 1, "group": "基本"},
    )
    last_period_date: str = Field(
        default="",
        description=(
            "最近一次月经开始的那天——就是这次流血的第一天，填 2026-11-05 这种格式。\n"
            "填了之后，「今天是周期第几天」由 今天 − 这个日期 算出来，能和现实日历对上；"
            "改了立即重算，不用等下一个周期。\n"
            "不记得、不想记、或者不需要它跟日历对齐，就留空："
            "留空时周期从你启用这项功能的那一刻开始自己累加，功能一样能跑，只是不对日历"
        ),
        # 面板只渲染 placeholder（框内灰字）与 hint（下方小字），
        # description 不上屏 —— 所以格式示例必须写在 placeholder 里。
        json_schema_extra={
            "label": "上次月经开始日期（可留空）",
            "order": 4,
            "group": "现实日历（可选）",
            "placeholder": "2026-01-01",
            "hint": "填最近一次月经开始那天，如 2026-01-01 或 2026/01/01；留空则从启用那一刻开始累加",
        },
    )


# 设计说明（不上屏，仅维护者可见）：
# 麦生活的日程只有 8 种 kind（meal/work/study/travel/leisure/sleep/nap/rest），
# 没有 exercise —— 运动只能从日程摘要的自由文本里认（"去操场跑步"）。
# 认出来之后是否真的运动由身体自己判断：外面暴晒、刮风、正在痛经、
# 熬夜欠着睡眠债、精力不够，都不该硬跑。这是刻意的：麦生活的日程是 LLM
# 编的，不该直接支配身体。
class ExerciseConfig(PluginConfigBase):
    """运动。根据麦生活判断，是否真进行运动受具体天气和身体情况影响。"""

    __ui_label__ = "运动"
    __ui_icon__ = "activity"
    __ui_order__ = 4

    enabled: bool = Field(
        default=True,
        description="是否根据日程摘要自动进入运动状态（身体条件不合适时会拒绝）",
        json_schema_extra={"label": "启用日程运动", "order": 0, "group": "运动"},
    )
    max_minutes: float = Field(
        default=45.0,
        description="单次运动最长持续分钟数。真实运动不会连续跑几小时",
        json_schema_extra={"label": "单次最长（分钟）", "order": 1, "group": "运动"},
    )
    interval_minutes: float = Field(
        default=15.0,
        description=(
            "运动节奏：跑多久歇多久。真实跑步是间歇的，"
            "不是一口气 60 分钟不停。填 15 表示每跑约 15 分钟歇一段"
        ),
        json_schema_extra={"label": "间歇节奏（分钟）", "order": 2, "group": "运动"},
    )
    min_energy: float = Field(
        default=55.0,
        description=(
            "运动所需的最低有效精力（0-100）。有效精力 = 当前精力 − 睡眠债扣分。"
            "低于这个值就不运动 —— 熬夜后第二天不该去跑步。调低会让她更常运动"
        ),
        json_schema_extra={"label": "运动最低精力", "order": 3, "group": "运动"},
    )
    min_sleep_hours: float = Field(
        default=6.0,
        description="昨晚睡眠少于这个小时数时不运动（熬夜次日不晨跑）",
        json_schema_extra={"label": "最低睡眠时长（小时）", "order": 4, "group": "睡眠与强制入睡"},
    )
    force_sleep_after_hours: float = Field(
        default=20.0,
        description=(
            "连续清醒超过多少小时会强制睡过去（微睡眠）。"
            "依据：睡眠剥夺会使不可抗拒的睡眠发作频率上升。"
            "设为 0 关闭该机制"
        ),
        json_schema_extra={"label": "强制入睡阈值（小时）", "order": 5, "group": "睡眠与强制入睡"},
    )
    force_sleep_hours: float = Field(
        default=6.0,
        description="被强制睡着后持续多久才自然醒（补偿性睡眠）",
        json_schema_extra={"label": "强制睡眠时长（小时）", "order": 6, "group": "睡眠与强制入睡"},
    )
    force_sleep_wakeable: bool = Field(
        default=True,
        description=(
            "强制睡眠期间能否被消息叫醒。开启时被叫醒后精力会明显下降、"
            "像宿醉一样糊；关闭时消息叫不醒，只能等自然醒"
        ),
        json_schema_extra={"label": "强制睡眠可叫醒", "order": 7, "group": "睡眠与强制入睡"},
    )


class SpeechConfig(PluginConfigBase):
    """主动播报。"""

    __ui_label__ = "主动播报"
    __ui_icon__ = "message-circle"
    __ui_order__ = 2

    proactive_enabled: bool = Field(
        default=False,
        description="是否允许麦麦主动说出身体状态（默认关闭）",
        json_schema_extra={"label": "允许主动播报"},
    )
    min_interval_minutes: float = Field(
        default=120.0,
        description="两次主动播报之间的最小间隔（分钟）",
        json_schema_extra={"label": "播报最小间隔（分钟）"},
    )


class RescueConfig(PluginConfigBase):
    """濒死急救。默认关闭，开启后根据身体健康状态决定是否就医，就医过程中会影响回复功能。"""

    __ui_label__ = "急救"
    __ui_icon__ = "heart-pulse"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description=(
            "濒死时自动送医急救。默认关闭：关闭后身体不会死，也不触发送医，只会在濒死线上徘徊，"
            "靠自愈与休养恢复。需要自动送医再手动打开"
        ),
        json_schema_extra={"label": "启用自动送医"},
    )
    threshold: float = Field(
        default=25.0,
        description="健康低于多少算濒死（0~100）",
        json_schema_extra={"label": "濒死健康阈值"},
    )
    critical_seconds: float = Field(
        default=600.0,
        description="濒死持续多少仿真秒才触发送医；0 表示一跌破就送",
        json_schema_extra={"label": "濒死持续判定（秒）"},
    )
    stay_hours: float = Field(
        default=6.0,
        description="住院时长（仿真小时），期间身体被支持治疗拉回正常区间",
        json_schema_extra={"label": "住院时长（小时）"},
    )
    restore_health: float = Field(
        default=74.0,
        description="出院时被恢复到多少健康值（0~100）",
        json_schema_extra={"label": "出院恢复健康值"},
    )
    silent_hours: float = Field(
        default=0.0,
        description=(
            "住院期间是否完全不响应消息：填仿真小时数，例如 6 表示住院前 6 小时内"
            "对任何消息都不回复；0 表示不拦截，正常聊天"
        ),
        json_schema_extra={"label": "住院静默时长（小时）"},
    )


class LinkConfig(PluginConfigBase):
    """与麦生活（mai-life）插件日程联动功能，关闭时本插件独立运行，启用后需要重启一次MaiBot本体生效。"""

    __ui_label__ = "联动"
    __ui_icon__ = "link"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description=(
            "总开关：开启后才与「麦生活」插件联动。关闭时两边互不影响，"
            "身体按自己的时钟独立运行。默认关闭，避免在没装麦生活时反复调用失败"
        ),
        json_schema_extra={"label": "启用联动", "order": 0, "group": "同步开关"},
    )
    sync_activity: bool = Field(
        default=True,
        description=(
            "作息驱动身体：用麦生活的睡眠阶段与当前活动，"
            "调整心率、体温与血压的昼夜基线（睡着她就不会是白天的代谢水平）"
        ),
        json_schema_extra={"label": "同步作息", "order": 1, "group": "同步开关"},
    )
    sync_meal: bool = Field(
        default=True,
        description=(
            "三餐跟麦生活日程走：麦生活日程里的 meal 时段开始时才吃饭，"
            "不再用本插件内置的 7:30/12:30/18:30 三个固定饭点"
        ),
        json_schema_extra={"label": "同步三餐", "order": 2, "group": "同步开关"},
    )
    inject_state: bool = Field(
        default=False,
        description=(
            "身体状态影响对话：在麦生活请求模型前，把当前体征作为一段话注入上下文。"
            "这条会增加 token 消耗（每次请求多几十到一两百 token）"
        ),
        json_schema_extra={"label": "体征注入对话", "order": 3, "group": "同步开关"},
    )
    sync_clock: bool = Field(
        default=False,
        description=(
            "时钟对齐：以麦生活的环境时间为准，避免两边对「现在几点」判断不一致。"
            "关闭时身体用自己的仿真时钟"
        ),
        json_schema_extra={"label": "时钟对齐", "order": 4, "group": "同步开关"},
    )
    share_state: bool = Field(
        default=False,
        description=(
            "身体状态触发主动分享：住院、濒危这类大事发生时，把「我现在怎么了」"
            "作为一次主动分享契机交给麦生活。默认关，因为它会主动找你说话"
        ),
        json_schema_extra={"label": "状态触发主动分享", "order": 5, "group": "同步开关"},
    )
    interval_seconds: float = Field(
        default=60.0,
        description=(
            "向麦生活取状态的间隔（现实秒）。跨进程调用有往返开销，不建议低于 30 秒"
        ),
        json_schema_extra={"label": "联动轮询间隔（秒）", "order": 6, "group": "连接"},
    )
    host_plugin_id: str = Field(
        default="maibot-community.mai-life",
        description="麦生活插件的 ID，一般不用改",
        json_schema_extra={"label": "麦生活插件 ID", "order": 7, "group": "连接"},
    )
    weather_enabled: bool = Field(
        default=True,
        description=(
            "抓取室外天气（温度与湿度），用来驱动体温、出汗与体感。"
            "数据来自 Open-Meteo，每 30 分钟一次，断网时沿用上次结果"
        ),
        json_schema_extra={"label": "抓取室外天气", "order": 8, "group": "天气"},
    )
    weather_city: str = Field(
        default="北京",
        description=(
            "天气查询城市名，中文即可（北京 / 上海 / 南宁）。"
            "插件会自己把城市名解析成坐标，不需要填经纬度。"
            "留空或解析失败时退回北京"
        ),
        json_schema_extra={"label": "天气城市", "order": 9, "group": "天气"},
    )
    weather_refresh_seconds: float = Field(
        default=1800.0,
        description="天气刷新间隔（秒）。默认 30 分钟，不要设得太短以免频繁请求",
        json_schema_extra={"label": "天气刷新间隔（秒）", "order": 10, "group": "天气"},
    )


class FeedingConfig(PluginConfigBase):
    """聊天喂食。只影响是否能通过对话额外投喂，开启后调用utils模型理解食物成分。"""

    __ui_label__ = "聊天喂食"
    __ui_icon__ = "utensils"
    __ui_order__ = 5

    enabled: bool = Field(
        default=False,
        description=(
            "允许麦麦在聊天里被喂东西：你随口说「我点了外卖」「给你买了奶茶」，"
            "她可以把这件事真的吃进身体里，而不是只在嘴上答应。默认关闭，需要时手动打开"
        ),
        json_schema_extra={"label": "允许聊天喂食"},
    )
    trust_private_only: bool = Field(
        default=False,
        description="只在私聊里认喂食。群聊人多，容易变成表演赛，默认不限制但可以打开",
        json_schema_extra={"label": "仅私聊认喂食"},
    )


class ExcretionConfig(PluginConfigBase):
    """排泄。关掉某一项只影响「事件记录与聊天里看到的」，体液与残渣的守恒计算照常进行。"""

    __ui_label__ = "排泄"
    __ui_icon__ = "droplet"
    __ui_order__ = 6

    show_urination: bool = Field(
        default=True,
        description=(
            "是否记录排尿事件。关闭后不再产生「排尿」记录，"
            "但水的出入量仍按生理计算，不会影响脱水/渗透压等指标"
        ),
        json_schema_extra={"label": "记录排尿事件"},
    )
    show_stool: bool = Field(
        default=True,
        description=(
            "是否记录排便事件。关闭后不再产生「排便」记录，"
            "结肠残渣仍按摄入量累积，不会影响消化相关的数字"
        ),
        json_schema_extra={"label": "记录排便事件"},
    )


class LifeBodyConfig(PluginConfigBase):
    """麦麦身体插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    sim: SimConfig = Field(default_factory=SimConfig)
    sleep: SleepConfig = Field(default_factory=SleepConfig)
    cycle: CycleConfig = Field(default_factory=CycleConfig)
    exercise: ExerciseConfig = Field(default_factory=ExerciseConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    rescue: RescueConfig = Field(default_factory=RescueConfig)
    link: LinkConfig = Field(default_factory=LinkConfig)
    feeding: FeedingConfig = Field(default_factory=FeedingConfig)
    excretion: ExcretionConfig = Field(default_factory=ExcretionConfig)
