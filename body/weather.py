# -*- coding: utf-8 -*-
"""室外天气：给身体提供「外面到底多冷多热」。

为什么自己查而不复用麦生活：
  麦生活只用 weather_code + temperature 给日程生成提示词，请求参数里
  没有湿度（environment.py 的 current 只传 temperature_2m,weather_code），
  所以缓存里也不会有。而体感温度离开湿度算不准 —— 35℃ 干燥和 35℃ 潮湿
  是两种体验。这里直连 Open-Meteo 拿齐温湿度。

经纬度从麦生活的天气缓存里读（它已经把城市解析成坐标了），免去重复配置。
读不到就退回北京。

设计约束：
  1. 任何失败都必须降级为 None，绝不让网络问题影响身体仿真。
  2. 缓存时长与麦生活一致（默认 30 分钟），避免高频请求。
  3. 只用标准库 urllib，不引入新依赖。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

# 麦生活未配置城市时的兜底坐标（北京天安门附近）
DEFAULT_LAT = 39.9042
DEFAULT_LON = 116.4074
DEFAULT_CITY = "北京"

# WMO 天气代码 → 中文（与麦生活同一套码表，只取常见项）
WEATHER_ZH = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


def _fetch_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "maibot-body-emulator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_GEO_CACHE: dict = {}


def geocode_city(city: str) -> tuple[float, float] | None:
    """把城市名解析成经纬度。中文名直接可用（Open-Meteo 支持 language=zh）。

    带进程内缓存：城市不变就不会重复请求。失败返回 None。
    """
    name = str(city or "").strip()
    if not name:
        return None
    if name in _GEO_CACHE:
        return _GEO_CACHE[name]
    params = urllib.parse.urlencode({
        "name": name, "count": 1, "language": "zh", "format": "json",
    })
    url = "https://geocoding-api.open-meteo.com/v1/search?" + params
    try:
        raw = _fetch_json(url, timeout=8.0)
        results = raw.get("results") or []
        if not results:
            return None
        hit = results[0]
        lat = float(hit["latitude"])
        lon = float(hit["longitude"])
        _GEO_CACHE[name] = (lat, lon)
        return (lat, lon)
    except Exception:
        return None


def fetch_weather(lat: float, lon: float, timezone: str = "Asia/Shanghai",
                  cache: dict | None = None, refresh_seconds: float = 1800.0,
                  with_wind: bool = True) -> dict | None:
    """查一次实时天气。带内存缓存；任何异常都返回 None。

    返回结构：
      {"temp_c": 10.4, "humidity": 78.0, "weather_code": 2,
       "description": "多云", "fetched_at": 1794..., "stale": False}
    """
    now = time.time()
    if cache and (now - float(cache.get("fetched_at", 0))) < refresh_seconds:
        return cache

    current_fields = "temperature_2m,relative_humidity_2m,weather_code"
    if with_wind:
        current_fields += ",wind_speed_10m"
    params = urllib.parse.urlencode({
        "latitude": round(float(lat), 4),
        "longitude": round(float(lon), 4),
        "current": current_fields,
        "timezone": timezone or "Asia/Shanghai",
    })
    url = "https://api.open-meteo.com/v1/forecast?" + params
    try:
        raw = _fetch_json(url)
        cur = raw.get("current") or {}
        temp = cur.get("temperature_2m")
        if temp is None:
            return cache if cache else None
        code = int(cur.get("weather_code", -1) or -1)
        # Open-Meteo 的风速单位是 km/h，统一换成 m/s 给体感公式用
        wind_kmh = float(cur.get("wind_speed_10m") or 0.0)
        return {
            "temp_c": float(temp),
            "humidity": float(cur.get("relative_humidity_2m") or 0.0),
            "wind_ms": wind_kmh / 3.6,
            "wind_kmh": wind_kmh,
            "weather_code": code,
            "description": WEATHER_ZH.get(code, "天气未知"),
            "fetched_at": now,
            "stale": False,
        }
    except Exception:
        # 网络失败：返回旧缓存并标记过期，没有旧缓存就返回 None
        if cache:
            aged = dict(cache)
            aged["stale"] = True
            return aged
        return None


def feels_like(temp_c: float, humidity: float, wind_ms: float = 0.0) -> float:
    """体感温度（Heat Index / Wind Chill 的简化组合）。

    - 高温高湿：汗蒸发受阻，体感高于气温（Heat Index）
    - 低温有风：风寒效应，体感低于气温（Wind Chill）
    - 中间区：接近气温
    湿度按 0~100 给，风速单位 m/s（当前没接入风速，默认 0）。
    """
    t = float(temp_c)
    h = max(0.0, min(100.0, float(humidity)))

    if t >= 27.0:
        # Rothfusz 回归（华氏度口径），再转回摄氏度
        tf = t * 9.0 / 5.0 + 32.0
        hi = (-42.379 + 2.04901523 * tf + 10.14333127 * h
              - 0.22475541 * tf * h - 0.00683783 * tf * tf
              - 0.05481717 * h * h + 0.00122874 * tf * tf * h
              + 0.00085282 * tf * h * h - 0.00000199 * tf * tf * h * h)
        return (hi - 32.0) * 5.0 / 9.0

    if t <= 10.0:
        # 风寒：无风时就等于气温，风速项暂为 0
        if wind_ms <= 0.0:
            return t
        v = wind_ms * 3.6  # m/s -> km/h
        wc = (13.12 + 0.6215 * t - 11.37 * (v ** 0.16)
              + 0.3965 * t * (v ** 0.16))
        return wc

    return t


def outdoor_load(temp_c: float, humidity: float, wind_ms: float = 0.0) -> float:
    """室外的「冷热压迫」强度，0~1。0 表示舒适无害，1 表示极端。

    这是给引擎用的单一标量：不需要它知道体感温度怎么算，
    只要知道"外面有多难受"就够了。

    曲线刻意做成平方根式而非线性：体感从 32℃ 到 36℃ 只是"难受"，
    真正危险的是 40℃ 以上，所以前段涨得慢、后段才逼近 1。
    若用线性（/(fl-32)/8），30℃/80% 时体感 37.7℃ 会直接给 0.71，
    与实际体感不符 —— 那时人只是不适，远没到极限。
    """
    # 风寒只在低温时起作用（高温时风反而是缓解）
    fl = feels_like(temp_c, humidity, wind_ms if temp_c <= 10.0 else 0.0)
    if fl >= 30.0:
        # 30℃ 起算，44℃ 到顶；平方根曲线让前段平缓
        x = min(1.0, max(0.0, (fl - 30.0) / 14.0))
        return x ** 0.65
    if fl <= 8.0:
        # 8℃ 起算，-12℃ 到顶
        x = min(1.0, max(0.0, (8.0 - fl) / 20.0))
        return x ** 0.75
    return 0.0
