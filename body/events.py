"""事件总线：把离散行为与连续生理量分开。"""
from dataclasses import dataclass, field
from typing import Any, Callable, List


SEVERITY = {"info": 0, "notice": 1, "warn": 2, "alert": 3, "critical": 4}


@dataclass
class Event:
    t: float
    kind: str          # meal / drink / urination / sleep_start / sleep_end / alert
    severity: str      # info / notice / warn / alert / critical
    text: str
    data: dict = field(default_factory=dict)

    @property
    def level(self):
        return SEVERITY.get(self.severity, 0)

    def __str__(self):
        h = int(self.t // 3600) % 24
        m = int(self.t % 3600 // 60)
        return f"[{h:02d}:{m:02d}] {self.severity.upper():8} {self.text}"


class EventBus:
    """极简发布订阅。事件同时进环形缓冲，供 AI 按需回溯。"""

    def __init__(self, history_limit: int = 2000):
        self._subs: List[Callable[[Event], None]] = []
        self.history: List[Event] = []
        self.limit = history_limit

    def subscribe(self, fn: Callable[[Event], None]):
        self._subs.append(fn)
        return fn

    def emit(self, ev: Event):
        self.history.append(ev)
        if len(self.history) > self.limit:
            del self.history[: len(self.history) - self.limit]
        for fn in self._subs:
            fn(ev)

    def recent(self, n: int = 20, min_severity: str = "info") -> List[Event]:
        lvl = SEVERITY.get(min_severity, 0)
        return [e for e in self.history if e.level >= lvl][-n:]
