"""webui_history 载荷的回归测试：证明它永不触发宿主的 JSON 上限。

背景（线上故障）：宿主对插件 WebUI API 返回值限制 ≤20000 个 JSON 元素，
24 小时 / 18 条序列 ≈ 3.0 万元素，身体跑满约 16 小时后接口必然 502。

这里不 import 插件包（会拉起 maibot_sdk），直接按路径加载
body/history.py；宿主侧的校验函数按 src/plugin_runtime/webui_schema.py
的 validate_json 逐字抄录，用来做"真机口径"对照。
"""

from __future__ import annotations

import importlib.util
import json
import random
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("body_history", ROOT / "body" / "history.py")
history = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(history)

KEYS = [
    "hr", "sbp", "dbp", "glucose", "temp", "energy", "hunger", "thirst",
    "gastric", "bac", "na", "stress", "depression", "euphoria", "dysphoria",
    "mood_valence", "mood_tone", "sleep_depth",
]

# 与宿主 src/plugin_runtime/webui_schema.py::validate_json 等价。
# 抄录目的：用真正会拦下请求的那份判据来验收，而不是自己发明一套。
HOST_MAX_BYTES = 524288


def host_validate_json(value, *, max_bytes=HOST_MAX_BYTES, max_depth=12):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > max_depth or count > 20000:
            raise ValueError(f"JSON 超过 {max_depth} 层或 20000 个元素")
        if isinstance(item, (dict, list)) and count + len(pending) + len(item) > 20000:
            raise ValueError("JSON 超过 20000 个元素")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON 对象的键必须为字符串")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("仅接受 JSON 数据")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError(f"JSON 超过 {max_bytes // 1024} KiB")


def make_points(n, *, start_minute=0):
    """造 n 个逐分钟采样点，字段与 _sample_point 一致。"""

    rng = random.Random(20261120)
    stages = ["awake", "rem", "light", "deep"]
    points = []
    for i in range(n):
        minute = (start_minute + i) % (24 * 60)
        points.append({
            "t": float(i * 60),
            "clock": f"{minute // 60:02d}:{minute % 60:02d}",
            "day": 1 + (start_minute + i) // (24 * 60),
            "hr": round(rng.uniform(55, 105), 1),
            "sbp": round(rng.uniform(95, 135), 1),
            "dbp": round(rng.uniform(55, 88), 1),
            "glucose": round(rng.uniform(3.6, 8.4), 2),
            "temp": round(rng.uniform(35.9, 37.6), 2),
            "energy": round(rng.uniform(10, 95), 1),
            "hunger": round(rng.uniform(0, 90), 1),
            "thirst": round(rng.uniform(0, 80), 1),
            "gastric": round(rng.uniform(0, 800), 1),
            "bac": 0.0,
            "na": round(rng.uniform(136, 145), 2),
            "sao2": 98.0,
            "stress": round(rng.uniform(0, 70), 1),
            "depression": round(rng.uniform(0, 60), 1),
            "euphoria": round(rng.uniform(0, 50), 1),
            "dysphoria": round(rng.uniform(0, 40), 1),
            "mood_valence": round(rng.uniform(-60, 60), 1),
            "mood_tone": round(rng.uniform(-40, 40), 1),
            "sleep_stage": stages[0] if 6 <= minute // 60 <= 22 else stages[1 + i % 3],
            "sleep_depth": round(rng.uniform(0, 1), 2),
        })
    return points


NOW = make_points(1)[0]


class CountingMatchesHost(unittest.TestCase):
    def test_counter_agrees_with_host_path(self):
        """自研计数必须与宿主同口径：一致时才敢用来推预算。"""

        fixtures = [
            {},
            [],
            {"a": [1, 2, 3]},
            {"x": {"y": [{"z": 1}]}},
            history.build_history_payload(make_points(90), KEYS, now=NOW, hours=6),
            history.build_history_payload(make_points(1440), KEYS, now=NOW, hours=24),
        ]
        for fixture in fixtures:
            self.assertLessEqual(host_counts(fixture), 20000)
            host_validate_json(fixture)  # 宿主判据必须放行，否则这份载荷上线即 502
            self.assertEqual(
                history.json_element_count(fixture),
                host_counts(fixture),
                f"计数口径不一致: {str(fixture)[:80]}",
            )


def host_counts(value):
    """宿主算法的纯计数版本（不抛异常），用于与 history.json_element_count 对账。"""

    pending = [value]
    count = 0
    while pending:
        item = pending.pop()
        count += 1
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return count


class PayloadWithinHostLimits(unittest.TestCase):
    def assert_ok(self, payload, label):
        host_validate_json(payload)  # 抛异常即失败
        self.assertLessEqual(history.json_element_count(payload), 20000, label)
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            HOST_MAX_BYTES,
            label,
        )

    def test_small_window_keeps_full_resolution(self):
        """12 小时 720 点必须在预算内且完全不动分辨率。"""

        points = make_points(720)
        payload = history.build_history_payload(points, KEYS, now=NOW, hours=12)
        self.assert_ok(payload, "12h")
        self.assertFalse(payload["sampled"])
        self.assertEqual(payload["count"], 720)
        self.assertEqual(len(payload["labels"]), 720)
        self.assertEqual(payload["stride"], 1.0)
        for key in KEYS:
            self.assertEqual(len(payload["series"][key]), 720, key)

    def test_24h_is_downsampled_but_valid(self):
        """24 小时是 now 页真实请求；1440 点必须降采样且形状保留。"""

        points = make_points(1440)
        payload = history.build_history_payload(points, KEYS, now=NOW, hours=24)
        self.assert_ok(payload, "24h")
        self.assertTrue(payload["sampled"])
        self.assertEqual(payload["source_count"], 1440)
        self.assertLess(payload["count"], 1440)
        self.assertGreaterEqual(payload["count"], 600)
        self.assertGreater(payload["stride"], 1.0)
        # 首尾必须落在原点集的首尾，否则会看出来"少了开头/结尾"
        self.assertEqual(payload["labels"][0], points[0]["clock"])
        self.assertEqual(payload["labels"][-1], points[-1]["clock"])
        for key in KEYS:
            self.assertEqual(len(payload["series"][key]), payload["count"], key)
        self.assertEqual(len(payload["days"]), payload["count"])
        self.assertEqual(len(payload["stages"]), payload["count"])

    def test_48h_full_history_is_valid(self):
        """timeline 页最大档 48 小时 + 默认 history_limit=2200 的最坏情形。"""

        points = make_points(2200)
        payload = history.build_history_payload(points, KEYS, now=NOW, hours=48)
        self.assert_ok(payload, "48h")
        self.assertEqual(payload["source_count"], 2200)

    def test_pathological_history_limit_is_valid(self):
        """用户把 history_limit 调到很大时也不能炸接口。"""

        points = make_points(12000)
        payload = history.build_history_payload(points, KEYS, now=NOW, hours=72)
        self.assert_ok(payload, "12000 points")
        self.assertGreaterEqual(payload["count"], history.MIN_POINTS)

    def test_empty_window(self):
        payload = history.build_history_payload([], KEYS, now=NOW, hours=1)
        self.assert_ok(payload, "empty")
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["sampled"])
        self.assertEqual(payload["labels"], [])
        for key in KEYS:
            self.assertEqual(payload["series"][key], [], key)

    def test_more_series_shrinks_points_not_breaks(self):
        """以后再加序列时，预算是自动换算的：点数变少，但仍合规。"""

        wide = KEYS + [f"extra_{i}" for i in range(30)]
        points = make_points(1440)
        payload = history.build_history_payload(points, wide, now=NOW, hours=24)
        self.assert_ok(payload, "wide")
        narrow = history.build_history_payload(points, KEYS, now=NOW, hours=24)
        self.assertLess(payload["count"], narrow["count"])

    def test_deterministic(self):
        points = make_points(1440)
        a = history.build_history_payload(points, KEYS, now=NOW, hours=24)
        b = history.build_history_payload(points, KEYS, now=NOW, hours=24)
        self.assertEqual(a, b)

    def test_stage_transitions_survive(self):
        """降采样不能让整个夜间分期图塌成一色：至少保留部分分期切换。"""

        points = make_points(1440)
        payload = history.build_history_payload(points, KEYS, now=NOW, hours=24)
        self.assertGreater(len(set(payload["stages"])), 1)


if __name__ == "__main__":
    unittest.main()
