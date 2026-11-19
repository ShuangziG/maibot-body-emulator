"""情绪自评（NLP）：分析麦麦自己说的话，不消耗任何模型 token。

组成：
  pxsenti/   嵌入式情感词典引擎（HowNet + DUTIR，零第三方依赖，约 0.75 MB）
  hook.py    判定入口：把一句话变成可写入身体的 valence 增量

为什么内置而不是外部依赖：插件必须自包含，宿主环境没有 _nlp_probe。
"""
from .hook import analyze, get_engine, DOMAIN_POS, DOMAIN_NEG

__all__ = ["analyze", "get_engine", "DOMAIN_POS", "DOMAIN_NEG"]
