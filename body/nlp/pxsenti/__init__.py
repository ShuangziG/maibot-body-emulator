# -*- coding: utf-8 -*-
"""pxsenti —— 零第三方依赖的中文情感分析。"""
from .pxsenti import (  # noqa: F401
    Sentiment, Emotion, get_engine, analyze, polarity, label,
)
__all__ = ["Sentiment", "Emotion", "get_engine", "analyze", "polarity", "label"]
