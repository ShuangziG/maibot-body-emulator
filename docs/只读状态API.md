# 只读状态 API

> 给其他插件（厨房、醉酒等）调用的公开接口与契约。回到主页：[README.md](../README.md)

---

## 给其他插件的公开 API

> 生理真值只在这里，表达交给下游插件。下面四个接口都是 `@API(public=True)`，
> 别的插件用 `api.call("<插件ID>.drunk_snapshot", ...)` 调用。

### 16.1 `drunk_snapshot()` — 只读

一次拿到醉酒相关的全部状态，纯读、不推进仿真、不落盘。

```json
{
  "ok": true, "version": 1,
  "world_t": 104050.0, "wall_t": 1794704598.0, "clock_offset_h": 22.05,
  "bac": 0.35, "chyme_alcohol": 6.98,
  "glucose": 5.3, "energy": 71.02, "hunger": 62.46, "thirst": 20.12,
  "sleeping": true, "sleep_stage": "deep", "sleep_debt": 3.2,
  "passed_out": false, "passed_out_at": 0.0, "sluggish": 0.45,
  "drinks": 5, "last_drink_items": "白酒",
  "last_meal_items": "米饭 + 汤", "last_meal_t": 103150.0
}
```

- `sluggish`（0~1）是**推导值**：血乙醇越高越迟钝，仅作单调映射，不是独立生理量。
- `passed_out` / `passed_out_at` 目前**恒为 false / 0.0**——身体没有断片模型，字段位留给以后。
- 身体未就绪时返回 `{"ok": false, "error": "身体尚未就绪"}`。

### 16.2 `log_drink(items, portion=1.0, note="", request_id="")` — 写入

把一次饮酒/饮水记进身体。复用与 `body_eat` 工具**同一条消化管线**，不另算营养。

```json
{
  "ok": true, "version": 1, "request_id": "abc",
  "accepted": ["Baijiu38"], "unknown": [],
  "portion": 0.5, "volume_ml": 25.0, "kcal": 52.5,
  "bac_before": 0.21, "bac_after": 0.21, "glucose_delta": 0.0, "fallback": false
}
```

- `portion` 夹紧在 0.05~3.0：半杯 = 0.5（体积、酒精、热量一起减半）。
- **认不出的词不报错**，列进 `unknown`，`ok` 仍为 `true`，此时按白水记（`fallback: true`）。
- `request_id` 幂等：同一 id 重复提交只生效一次，第二次返回 `duplicate: true`。
- **注意**：`bac_after` 在记账当下通常仍是旧值——酒精先进胃（`chyme_alcohol` 上升），要等胃排空才入血。判断"她醉没醉"请轮询 `drunk_snapshot`，别只看这次调用的返回值。

### 16.3 `get_clock()` — 只读

```json
{ "ok": true, "version": 1, "world_t": 104050.0, "wall_t": 1794704598.0, "offset_h": 22.05, "speed": 1.0 }
```

用来判断「她的夜里」是不是「你的夜里」，避免半夜说「正在晒太阳」。

### 16.4 `list_foods()` — 只读

返回 311 条食物/饮品词表。**两套字段名同时给**，兼容已对接的两个插件：

- `items`：`key / label / group / volume_ml / kcal / alcohol_g / selectable`
- `foods`：同上，另加 `default_portion`（如 `"一碗 200 g 熟"`）
- `aliases`：473 条别名映射（单词映射到单个 key 时给字符串，映射到多个 key 时给数组）

### 16.5 `log_meal(items, kind="meal", portion=1.0, note="", request_id="")` — 写入

外部插件代记一顿吃喝。`items` 三种写法都收：

```python
[{"key": "Tomato", "amount_g": 180}, {"label": "牛肉", "amount_g": 90}]   # 带克数
["Tomato", "Rice"]                                                        # 只给词
"番茄,米饭"                                                                # 逗号串
```

**克数换算**：按该食物 `portion` 字符串里的基准换算——「一碗 200 g 熟」→ 200 g 为 1 份，
传 100 g 就记半份；抠不出克数的（如「1 个」）退回 `volume_ml` 近似，并在返回里标 `estimated: true`。

返回回执（厨房据此说话）：

```json
{
  "ok": true, "version": 1, "kind": "meal",
  "accepted": [{"key": "Tomato", "label": "西红柿", "amount_g": 180.0, "ratio": 1.2, "estimated": false}],
  "rejected": [{"label": "龙肝", "reason": "unknown_food"}],
  "totals": {"kcal": 292.4, "volume_ml": 380.0, "carb_g": 62.0, "protein_g": 6.6, "fat_g": 0.0,
             "alcohol_g": 0.0, "caffeine_mg": 0.0, "water_ml": 0.0, "na_mmol": 0.0},
  "delta": {"glucose_mmol": 0.0, "bac_g_l": 0.0},
  "state": {"hunger": 20.0, "thirst": 20.0, "energy": 80.0, "health": 100.0,
            "gastric_volume_ml": 380.0, "glucose": 5.0}
}
```

注意：按克数记账时**不做「补到七分饱」**——厨房报的是实际重量，再补就会让半碗凭空变大。

### 16.6 `get_state()` — 只读

```json
{"ok": true, "version": 1, "sim_day": 1, "clock": "08:00", "clock_offset_seconds": 28800.0,
 "world_t": 0.0, "hunger": 20.0, "thirst": 20.0, "energy": 80.0, "health": 100.0,
 "glucose": 5.0, "bac": 0.0, "gastric_volume_ml": 380.0,
 "last_meal_items": "西红柿 + 米饭", "last_meal_ts": 0.0, "drinks": 0,
 "sleeping": false, "stress": 20.0, "depression": 2.0}
```

### 16.7 `adjust_mood(...)` — 写入

身体**没有 `mood` 字段**，只有 `stress`（压力）与 `depression`（累积应激）。两套签名都收：

```python
adjust_mood(stress_delta=5, reason="厨房：做好了饭")          # 厨房式
adjust_mood(stress_add=3, depression_add=-2, label="暧昧")  # 醉酒式
adjust_mood(mood_delta=4)                                   # 映射为 stress -2
```

- 每项夹在 ±20，最终值限制在 0~100；
- `mood_delta` 按「心情好 = 压力降一半」保守映射，不直接写心情；
- 传 `label` 或 `reason` 都会回显，便于审计。

### 16.8 契约

1. **单位固定**：`bac` = g/L，`chyme_alcohol` = g，`volume_ml` = mL，`world_t` = 秒（float，单调递增）。
2. **幂等**：`log_drink` 的 `request_id` 重复提交只生效一次。
3. **部分成功**：未知食材不抛错，`unknown` 列出，`ok` 仍为 `true`。
4. **只读无副作用**：`drunk_snapshot` / `get_clock` / `list_foods` 不触发仿真步进、不落盘。
5. **版本**：返回里带 `version`，以后加字段不改已有字段语义。
6. **频率**：只读接口建议 30~60 秒一次（调用方可缓存）；`log_drink` 是低频写。
7. **能力声明**：调用方 `_manifest.json` 必须声明 `api.call`，否则宿主抛 `[E_CAPABILITY_DENIED]`。
