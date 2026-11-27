"""体征曲线的载荷裁剪：把采样点塞进宿主插件 API 的 JSON 预算。

为什么要单独有这个模块（2026-11-20 线上故障复盘）：
宿主 src/plugin_runtime/webui_schema.py::validate_json 对插件 WebUI API 的
返回值有三道硬限制 —— 元素总数 ≤ 20000、深度 ≤ 12、序列化后 ≤ 512 KiB。
超限时宿主直接把请求判成 502，浏览器侧只看到：

    数据异常：拉取体征曲线失败：ApiError: JSON 超过 20000 个元素

旧实现把窗口内每个采样点原样摊开：18 条序列 + labels/days/stages 三列，
约 21 个元素/点。now 页固定请求 24 小时 = 1440 点 ≈ 3.0 万元素，
所以身体只要连续跑满约 16 小时就必然越界 —— 本地跑得短才看不出来，
线上跑得久就"随机"报错，实际是确定性的容量越界。

修法不是把小时数写死成 12（12 小时同样会随序列增加而复发），而是
按宿主的元素算法反推能装多少点，均匀降采样，并把 sampled/stride
如实回传给前端。以后再加序列也不会重新炸。
"""

from __future__ import annotations

from typing import Any, Sequence

# --- 宿主硬限制（与 src/plugin_runtime/webui_schema.py 保持一致） ---
HOST_ELEMENT_LIMIT = 20000
HOST_MAX_DEPTH = 12
HOST_BYTE_LIMIT = 512 * 1024

# 自留余量：宿主还会在外面再包一层 {success, result}，且后续往载荷里
# 加几个字段时不必重算预算。
ELEMENT_BUDGET = 19000

# 每个采样点固定贡献的数组：labels / days / stages / times 四个对齐列。
# 2026-11-26 新增 times：夜间分期图要按「这一夜」的真实起止时刻截取区间，
# 光有 "HH:MM" 字符串无法跨天区分（同一个钟点每天都有一次）。
ALIGNED_ARRAYS = 4

# 降采样后至少保留的点数：再少曲线形状就失真了。
MIN_POINTS = 24


def json_element_count(value: Any) -> int:
    """按宿主的算法数元素：标量/dict/list 各记 1，dict 的键不计数。

    与宿主 validate_json 的计数口径逐字对齐，改动前先看那份实现。
    """

    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > HOST_MAX_DEPTH:
            raise ValueError(f"JSON 深度超过 {HOST_MAX_DEPTH} 层")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return count


def _round(value: Any, digits: int) -> float:
    """安全取整，非数值一律回落 0（与 plugin.py 的 _r 同口径）。"""

    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _pick_indices(total: int, want: int) -> list[int]:
    """在 total 个点里均匀取 want 个，首尾必保留。

    取整可能产生重复下标，去重后返回；调用方只关心"按顺序取到了哪些点"。
    """

    if total <= 0:
        return []
    if want >= total:
        return list(range(total))
    if want <= 1:
        return [total - 1]
    step = (total - 1) / float(want - 1)
    seen: set[int] = set()
    picked: list[int] = []
    for i in range(want):
        idx = min(total - 1, max(0, int(round(i * step))))
        if idx not in seen:
            seen.add(idx)
            picked.append(idx)
    return picked


def _payload(
    picked: Sequence[dict[str, Any]],
    keys: Sequence[str],
    *,
    now: dict[str, Any] | None,
    hours: int,
    source_count: int,
    limit: int,
) -> dict[str, Any]:
    """按前端既有字段名组装载荷；形状不变，只多了几个标注字段。"""

    total = len(picked)
    sampled = total < source_count
    return {
        "hours": int(hours),
        "count": total,
        # --- 新增：降采样标注，前端据此提示分辨率 ---
        "source_count": int(source_count),
        "point_limit": int(limit),
        "sampled": bool(sampled),
        "stride": round(source_count / float(total), 2) if total else 1.0,
        # --- 前端既有字段，名字与顺序都不要动 ---
        "labels": [str(p.get("clock", "")) for p in picked],
        "days": [int(p.get("day", 1) or 1) for p in picked],
        "stages": [str(p.get("sleep_stage") or "awake") for p in picked],
        # times：仿真时刻（秒）。前端用它把分期图裁到「这一夜」的区间里，
        # 否则一天里的午睡会被当成"最近一段睡眠"画上夜间分期图。
        "times": [_round(p.get("t", 0.0), 1) for p in picked],
        "series": {key: [_round(p.get(key, 0.0), 2) for p in picked] for key in keys},
        "now": now if isinstance(now, dict) else {},
    }


def point_budget(keys: Sequence[str], *, now: dict[str, Any] | None = None,
                 hours: int = 0, budget: int = ELEMENT_BUDGET) -> int:
    """在给定序列条数下，算出还能装多少个采样点。"""

    per_point = len(keys) + ALIGNED_ARRAYS
    if per_point <= 0:
        return MIN_POINTS
    # 空载荷的元素数就是与点数无关的固定开销。
    fixed = json_element_count(_payload((), keys, now=now, hours=hours,
                                        source_count=0, limit=0))
    return max(MIN_POINTS, (budget - fixed) // per_point)


def build_history_payload(
    points: Sequence[dict[str, Any]],
    keys: Sequence[str],
    *,
    now: dict[str, Any] | None = None,
    hours: int = 0,
    budget: int = ELEMENT_BUDGET,
) -> dict[str, Any]:
    """构造 webui_history 的返回值，保证不触发宿主的 20000 元素上限。

    points 已按时间窗筛好（升序）。超出预算时等间隔抽点：
    曲线形状、首尾、夜间分期都能保住，只是分辨率从 1 分钟放宽。
    """

    keys = [str(k) for k in keys]
    source_count = len(points)
    limit = point_budget(keys, now=now, hours=hours, budget=budget)
    picked = [points[i] for i in _pick_indices(source_count, min(source_count, limit))]
    payload = _payload(picked, keys, now=now, hours=hours,
                       source_count=source_count, limit=limit)

    # 兜底：万一以后往载荷里加了固定字段把预算吃穿，就继续对半砍，
    # 宁可变粗也不能让整个接口报错（那会让页面整块空掉）。
    while len(picked) > MIN_POINTS and json_element_count(payload) > HOST_ELEMENT_LIMIT:
        picked = picked[::2] or picked[-1:]
        limit = len(picked)
        payload = _payload(picked, keys, now=now, hours=hours,
                           source_count=source_count, limit=limit)
    return payload
