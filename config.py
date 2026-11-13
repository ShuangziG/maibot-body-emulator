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
        default="1.0.0",
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
        json_schema_extra={"label": "启用联动"},
    )
    sync_activity: bool = Field(
        default=True,
        description=(
            "作息驱动身体：用麦生活的睡眠阶段与当前活动，"
            "调整心率、体温与血压的昼夜基线（睡着她就不会是白天的代谢水平）"
        ),
        json_schema_extra={"label": "同步作息"},
    )
    sync_meal: bool = Field(
        default=True,
        description=(
            "三餐跟麦生活日程走：麦生活日程里的 meal 时段开始时才吃饭，"
            "不再用本插件内置的 7:30/12:30/18:30 三个固定饭点"
        ),
        json_schema_extra={"label": "同步三餐"},
    )
    inject_state: bool = Field(
        default=False,
        description=(
            "身体状态影响对话：在麦生活请求模型前，把当前体征作为一段话注入上下文。"
            "这条会增加 token 消耗（每次请求多几十到一两百 token）"
        ),
        json_schema_extra={"label": "体征注入对话"},
    )
    sync_clock: bool = Field(
        default=False,
        description=(
            "时钟对齐：以麦生活的环境时间为准，避免两边对「现在几点」判断不一致。"
            "关闭时身体用自己的仿真时钟"
        ),
        json_schema_extra={"label": "时钟对齐"},
    )
    share_state: bool = Field(
        default=False,
        description=(
            "身体状态触发主动分享：住院、濒危这类大事发生时，把「我现在怎么了」"
            "作为一次主动分享契机交给麦生活。默认关，因为它会主动找你说话"
        ),
        json_schema_extra={"label": "状态触发主动分享"},
    )
    interval_seconds: float = Field(
        default=60.0,
        description=(
            "向麦生活取状态的间隔（现实秒）。跨进程调用有往返开销，不建议低于 30 秒"
        ),
        json_schema_extra={"label": "联动轮询间隔（秒）"},
    )
    host_plugin_id: str = Field(
        default="maibot-community.mai-life",
        description="麦生活插件的 ID，一般不用改",
        json_schema_extra={"label": "麦生活插件 ID"},
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
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    rescue: RescueConfig = Field(default_factory=RescueConfig)
    link: LinkConfig = Field(default_factory=LinkConfig)
    feeding: FeedingConfig = Field(default_factory=FeedingConfig)
    excretion: ExcretionConfig = Field(default_factory=ExcretionConfig)
