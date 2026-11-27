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
 "sleeping": false, "stress": 20.0, "depression": 2.0,
 "euphoria": 4.8, "dysphoria": 0.0, "mood_valence": 0.0, "mood_tone": 3.2}
```

> **v1.3.0 起**回传三个情绪字段：`euphoria`（欣快 0~100）、`dysphoria`（烦躁 0~100）、
> `mood_valence`（瞬时情绪效价 -100~+100）。三者与 `depression`（长期心境）不同：
> 前两者是当场感受，`mood_valence` 写入后自然衰减。
>
> **v1.3.1 起**另有 `mood_tone`（心境底色 −60~+40，**只读**）：由身体状态（精力、昨夜睡眠、
> 睡眠债、疼痛、应激累积、经前负荷、口渴饥饿）驱动的慢变量，时间常数 4 小时。
> 它解释"两次对话之间情绪不是一条 0 直线"——写入接口**不能**直接改它。
> 同时 `mood_valence` 的衰减时间常数由 6 分钟放宽到 **20 分钟**（一句话的影响该覆盖几轮对话）。

> **v1.4.0 起**追加 18 个只读字段（原有字段的语义与量纲不变）：
>
> | 分组 | 字段 | 单位 / 取值 |
> |---|---|---|
> | 血液 | `blood_vol` / `hb` / `hct` | mL / g·L⁻¹ / % |
> | 血液 | `anemia` | `none` / `mild` / `moderate` / `severe` |
> | 血液 | `platelet` / `iron_store` | 10⁹·L⁻¹ / mg |
> | 免疫 | `wbc` / `neut` / `lymph` | 10⁹·L⁻¹ |
> | 免疫 | `crp` / `infection` / `infection_site` | mg·L⁻¹ / 0~1 / 文本 |
> | 免疫 | `fever_drive` | ℃（致热驱动，不是体温） |
> | 营养 | `diet_score` / `diet_flags` | 0~100 / 逗号分隔的标记 |
> | 身体 | `weight_kg` / `bmi` | kg / —（派生值，不反馈生理） |
> | 习惯 | `habits` | 字符串数组，如 `["regular_sleep","healthy_diet"]` |
>
> 用它们做判断时的注意点：
> - `fever_drive` 是加到调定点上的偏移量，**体温请看 `temp`**（get_state 不直接回传 temp，
>   需要体温请用 `webui_now` 或 `webui_blood`）。
> - `habits` 是判定结果，不代表生理状态本身；且习惯判定按设计**零副作用**，
>   外部读它做任何事都不会反过来改动身体。
> - `weight_kg` / `bmi` 由累计能量盈亏派生，只用于"超重/肥胖"判定。

### 16.7 `adjust_mood(...)` — 写入

身体**没有长期 `mood` 字段**，情绪分三个层次，各自的时间尺度不同：

| 层次 | 字段 | 写入参数 | 持续多久 |
|---|---|---|---|
| 长期心境 | `depression` | `depression_delta` / `depression_add` | 几天（由应激累积） |
| 心境底色 | `mood_tone`（v1.3.1） | **无写入参数（只读）** | 4 小时（由身体状态驱动） |
| 当场压力 | `stress` | `stress_delta` / `stress_add` / `mood_delta` | 约 15 分钟 |
| 当场情绪 | `mood_valence` | **`valence`（v1.3.0 新增）** | 约 20 分钟自衰减（v1.3.1 由 6 分钟放宽） |

```python
adjust_mood(stress_delta=5, reason="厨房：做好了饭")          # 厨房式
adjust_mood(stress_add=3, depression_add=-2, label="暧昧")  # 醉酒式
adjust_mood(mood_delta=4)                                   # 映射为 stress -2
adjust_mood(valence=-45, reason="这一句里她在抱怨")          # v1.3.0：当场情绪
```

- `stress_delta` / `depression_delta` 每项夹在 ±20，最终值限制在 0~100；
- `mood_delta` 按「心情好 = 压力降一半」保守映射，不直接写心情；
- **`valence` 夹在 ±100，且是累加的** —— 一轮对话里多个分句各判一次不会互相覆盖；
  引擎按约 20 分钟的时间常数让它自然衰减回 0（v1.3.1 由 6 分钟放宽），不会赖着不走；
- 正面 `valence` 降压力、抬欣快；负面 `valence` 升压力、抬烦躁；
- 返回值回显 `mood_valence` / `euphoria` / `dysphoria` / `mood_tone` 当前值，便于写入后闭环核对；
- 传 `label` 或 `reason` 都会回显，便于审计。

### 16.8 契约

1. **单位固定**：`bac` = g/L，`chyme_alcohol` = g，`volume_ml` = mL，`world_t` = 秒（float，单调递增）。
2. **幂等**：`log_drink` 的 `request_id` 重复提交只生效一次。
3. **部分成功**：未知食材不抛错，`unknown` 列出，`ok` 仍为 `true`。
4. **只读无副作用**：`drunk_snapshot` / `get_clock` / `list_foods` 不触发仿真步进、不落盘。
5. **版本**：返回里带 `version`，以后加字段不改已有字段语义。
6. **频率**：只读接口建议 30~60 秒一次（调用方可缓存）；`log_drink` 是低频写。
7. **能力声明**：调用方 `_manifest.json` 必须声明 `api.call`，否则宿主抛 `[E_CAPABILITY_DENIED]`。

---

## 睡眠结构：`webui_now` 的 `sleep` 对象

> 这一节是给前端与二次开发对接用的，不是上面那套给其他插件的公开 API。
> 字段由 `LifeBodyPlugin._sleep_view()` 产出，遵循「只加字段、不删字段」。

### 一张卡上有三套口径

睡眠卡片同时展示「结构行」「最近一夜」「夜间分期图」，三者**来源不同**，看数据时别把它们当成同一个数：

| 界面位置 | 字段 | 口径 |
|---|---|---|
| 浅睡 / 深睡 / REM / 总时长 | `light_h` `deep_h` `rem_h` `total_h` | **最近一次合格 bout**。午睡不计入，永远不会显示午睡时长。 |
| 最近一夜 / 整夜构成 | `last_night` | **按夜聚合**后的一整夜（同一晚的多段 bout 合并）。 |
| 夜间分期图 | `bouts` + `history` 采样 | 按 `last_night.start_t`/`end_t` 裁剪真实采样点。 |

### 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `total_h` `light_h` `deep_h` `rem_h` | float | 结构行时长（小时），口径为最近一次合格 bout |
| `light_pct` `deep_pct` `rem_pct` | float | 上述三段占 `total_h` 的百分比（0~100） |
| `quality` | float | 睡眠质量评分 0~100（PSQI 成分的 7 日类比，见 `docs/血糖与睡眠文献依据.md`） |
| `debt_h` | float | 睡眠债（小时） |
| `stage` / `stage_text` | str | 当前分期 `awake\|light\|deep\|rem` 及中文名 |
| `sleeping` | bool | 此刻是否睡着 |
| `bout_active` | bool | 当前是否处在「连续睡眠区间」中（已入睡且未结算） |
| `cycle_index` / `cycle_pos` | int / float | 本夜第几个 NREM-REM 周期、当前周期进度 0~1 |
| `last_sleep_hours` | float | 最近一次**合格** bout 的睡眠小时（午睡不写） |
| `counts_as_night` | bool | 当前口径下的时长是否已达合格夜门槛 |
| `min_bout_hours` | float | 合格夜门槛（默认 4.0） |
| `last_night` | obj | 最近一夜的聚合视图，见下 |
| `week_nights` | int | 近 7 日**合格夜数**（按夜计数，不按 bout 段数） |
| `week_hours` `week_quality` `week_efficiency` | float | 近 7 日汇总（时长 / 按小时加权评分 / 按小时加权效率） |
| `week_skipped` | int | 近 7 日被判为午睡而跳过的段数 |
| `bouts` | list | 最近 7 段合格 bout，元素含 `start_t` `end_t` `hours` `light_h` `deep_h` `rem_h` `quality` `efficiency` `wakes` `cycles` `night_key` |
| `depth` | float | 睡眠深度 0~1（0 = 清醒，1 = 熟睡） |
| `awake_h` | float | 当前 bout 内累计清醒小时（计入在床时间，不计入睡眠时长） |
| `wake_count` | int | 本 bout 被唤醒次数 |
| `wake_by_message` `wake_by_thirst` `wake_by_bladder` | int | 上述唤醒按原因分类的计数 |
| `wake_pending` | bool | 是否处于「已判定唤醒、等待生效」的过渡态 |
| `last_wake_reason` | str | 最近一次唤醒原因（中文短语） |
| `awake_hours` | float | 连续清醒小时数（强制睡眠判据用） |
| `forced_sleep` | bool | 是否处于强制睡眠（连续清醒过久，无视作息窗口） |
| `microsleep_count` | int | 微睡眠次数 |

### `sleep.last_night`

空的或异常的夜一律返回 `{}`，前端各字段按 `—` 显示，无需做 None 判断。

| 字段 | 含义 |
|---|---|
| `start_t` `end_t` | 这一夜的起止（仿真秒）。**分期图用它裁剪区间**，否则一次日间小睡会被当成「最近一段睡眠」画进图里 |
| `hours` | 整夜累计睡眠小时（各段 bout 之和） |
| `bout_h` | 整夜在床小时（含中途清醒） |
| `bouts` | 这一夜由几段 bout 合并而成 |
| `cycles` | 这一夜累计的 NREM-REM 周期数 |
| `wakes` | 夜内被唤醒次数 |
| `light_h` `deep_h` `rem_h` | 整夜分期构成 |
| `quality` `efficiency` | 按小时加权的评分与睡眠效率 |

### 夜归属与午睡判定

- **夜归属看入睡时刻**：同一 `night_key` 的 bout 合并为一夜。取入睡时刻而不是醒来时刻，是为了让「02:45 睡到 12:13」这种晚起仍归前一晚 —— 用醒来时刻会因为它越过正午分界而被劈成两夜。
- **`night_key` 在结算时固化**：写进 `bout["night_key"]`,之后只读不算。它**不随 `clock_offset` 漂移而改变**（`sync_clock` 同步、重启按墙钟重对齐都会改 `clock_offset`，早期版本因此出现过「两夜并一夜」）。
- **午睡判定**：`总时长 < min_bout_hours` **且** bout 起点不在夜间窗口（含前后 2 小时余量）→ 记午睡，只累加 `week_skipped`，不写 `sleep_bouts`、不写 `last_sleep_hours`、不结算睡眠债。
- **存档迁移**：`BodyState.sleep_night_rule < 2` 的旧存档，在 `_restore()` 时会按 `sleep_bouts` 用新规则整体重建一次 nights（幂等），并把标记置为 2。

