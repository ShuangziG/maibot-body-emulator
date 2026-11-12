"""lifebody — 一具独立运行的赛博身体。

不依赖任何外部服务、插件或数据库。纯 Python 标准库实现。
字段范围限定在 LIFE.apk il2cpp 元数据能对上的生理量，判据见
apk_report/字段对照表.md。

快速开始：
    from lifebody import BodyEngine, describe

    body = BodyEngine()
    for _ in range(3600):
        body.step(1.0)
    print(describe(body.s))
"""
from .state import BodyState, DEFAULT_STATE, clamp
from .events import Event, EventBus, SEVERITY
from .engine import BodyEngine
from .translate import describe, brief, explain_change, FEELING_LEVELS, phase
from .card import build_html

__version__ = "0.3.0"
__all__ = ["BodyEngine", "BodyState", "DEFAULT_STATE", "clamp",
           "Event", "EventBus", "SEVERITY",
           "describe", "brief", "explain_change", "FEELING_LEVELS", "phase",
           "build_html"]
