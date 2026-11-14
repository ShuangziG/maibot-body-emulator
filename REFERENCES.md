# 参考文献

> 本文件只记录**实际查阅过**的文献与资料来源。
> 没有查过的不写进来，凭常识知道的只在代码注释里标「估算」。
>
> 引用纪律：
> 1. `CHANGELOG_DEV.md` 里凡是引用了文献的改动，标注编号（如 `[R1]`）
> 2. 新增文献先补到本文件，再在改动记录里引用
> 3. 二手转述必须注明是转述，不得冒充一手文献
>
> 格式：作者. 标题. 期刊 年份;卷(期):页码. doi/PMID

---

## [R1] Devlin J, Paton B, Poole L, Sun W, Ferguson C, Wilson J, Kemi OJ.
**Blood lactate clearance after maximal exercise depends on active recovery intensity.**
*The Journal of Sports Medicine and Physical Fitness*. 2014 Jun;54(3):271-278.
PMID: 24739289

**查阅方式**：PubMed E-utilities（`esummary.fcgi`）获取元数据，标题/期刊/卷期/页码均经 API 核实。

**用于**：`body/engine.py` 血乳酸清除模型。

**取用内容**：
- 最大强度运动后，主动恢复清除血乳酸显著快于被动恢复
- 清除速度与恢复强度相关（intensity-dependent）

**未取用**：本文未给出具体半衰期数值，半衰期另见下方说明。

---

## [R2] 血乳酸半衰期 25 分钟 —— **二手转述，待替换为原始文献**

**来源**：网络搜索命中的一篇中文科普文（清华波士顿跑步群，标题「排酸跑？你被骗了！」）

**原文表述**：「在大强度运动完后，如果保持静息状态，血乳酸清除速度最慢，血乳酸消除的半时反应为 25 分钟，恢复至运动前水平大约需要 1-2 个小时」

**用于**：`body/engine.py` 中 `clear_halflife_min = 25.0`

**诚实标注**：这条是**科普转述**，不是我读到的一手论文。数字与运动生理学教科书的普遍说法一致，但**没有直接核实到原始定量研究**。后续若找到原始文献必须替换本条。

---

## 未采用的检索结果（记录备查）

以下条目是检索时返回的元数据，**我没有阅读正文，也没有采用其结论**，故不计入参考文献：

- Ferguson BS, et al. *Lactate metabolism: historical context, prior misinterpretations, and current understanding.* Eur J Appl Physiol. 2018;118(4):691-728. doi:10.1007/s00421-017-3795-6 — 仅见于检索列表
- Brooks GA. *Mitochondrial lactate metabolism: history and implications for exercise and disease.* J Physiol. 2021;599(3):863-888. doi:10.1113/JP278930 — 仅见于检索列表

---

## 非文献类数据来源

### 一手资料：LIFE.apk 元数据

**路径**：`_apk_LIFE/assets/bin/Data/Managed/Metadata/global-metadata.dat`
**取证偏移**：755085（枚举串）、277748（器官面板）

**用于**：月经周期双相位机、食物表、症状枚举。

这是本项目的**原始依据**，不属于学术文献但优先级高于任何二手材料。
详见 `apk_report/LIFE_APK_机制勘察.md`。

### Open-Meteo 天气 API

**地址**：`https://api.open-meteo.com/v1/forecast`、`https://geocoding-api.open-meteo.com/v1/search`

**核查方式**：实际调用验证（北京返回 10.4℃ / 78% / 1.5km/h），确认支持 `relative_humidity_2m` 与 `wind_speed_10m`。

**用于**：`body/weather.py`。

---

## 没有文献支撑的部分（必须在代码里标"估算"）

| 位置 | 数值 | 状态 |
|---|---|---|
| `engine.py` 乳酸产率 | 28 / 15 mmol/L/h | 估算 —— 反推自"15 分钟跑到 8 mmol/L"的目标，无文献 |
| `engine.py` 乳酸清除运动后加速 | ×1.25 | 估算 —— [R1] 只给了定性方向，具体系数是我定的 |
| `engine.py` 疲劳合成权重 | 0.45/0.30/0.25 | 估算 |
| `engine.py` 睡眠四态占比 | 55/35/10 → 60/10/30 | 估算 —— 定性规律是常识，具体百分比是编的 |
| `weather.py` outdoor_load | 平方根曲线 | 估算 |
| `weather.py` 体感投影 | 2.2 / 2.0 分每度 | 估算 |
---

## [R3] Cheung K, Hume P, Maxwell L.
**Delayed onset muscle soreness: treatment strategies and performance factors.**
*Sports Medicine*. 2003;33(2):145-164. doi:10.2165/00007256-200333020-00005. PMID: 12617692

**查阅方式**：PubMed E-utilities（`efetch.fcgi` + `rettype=abstract`）读取全文摘要。

**用于**：`engine.py` 的 DOMS 模型（延迟性肌肉酸痛）。

**取用内容**：
- DOMS 有六种假说，**乳酸假说已被否定** —— 这与本模型「DOMS 独立于乳酸」的设计一致
- 离心运动造成微损伤，强度与时长都是影响因素
- DOMS 影响运动表现：关节活动度、峰值力矩下降
- **运动后 1-2 天应降低运动强度** —— 支撑 `doms > 30 时拒绝运动` 的门槛

---

## [R4] Hotfiel T, Freiwald J, Hoppe MW, Lutter C, Forst R, Grim C, Bloch W, Hüttel M, Heiss R.
**Advances in Delayed-Onset Muscle Soreness (DOMS): Part I: Pathogenesis and Diagnostics.**
*Sportverletzung Sportschaden*. 2018 Dec;32(4):243-250. doi:10.1055/a-0753-1884. PMID: 30537791

**查阅方式**：PubMed E-utilities 读取摘要。

**用于**：DOMS 时间曲线。

**取用内容**：
- **「peak soreness at 48-72 h post-exercise」** —— 直接决定模型中 48h 达峰
- 机制是肌细胞超微结构损伤 + 局部炎症反应
- 临床症状延迟出现，是复杂的局部与全身反应序列

---

## [R5] Doma K, Matoso B, Protzen G, Singh U, Boullosa D.
**The Repeated Bout Effect of Multiarticular Exercises on Muscle Damage Markers and Physical Performances: A Systematic Review and Meta-Analyses.**
*Journal of Strength and Conditioning Research*. 2023 Dec;37(12):2504-2515. doi:10.1519/JSC.0000000000004628. PMID: 38015738

**查阅方式**：PubMed E-utilities 读取摘要。

**用于**：`exercise_habit` 对 DOMS 的抑制（重复运动效应）。

**取用内容**：
- 20 项研究的系统综述 + 元分析
- 第二次运动后的 DOMS 与 CK 显著低于第一次（T24 与 T48，p<0.001，**SMD = 0.51-1.23**）
- **一次运动即可产生保护效果**，且不受强度与运动量变化影响

**模型取值**：习惯 0→1 时 DOMS 降幅 69%，落在文献效应量区间内（估算值）。

---

## 新增的「估算」标注

| 位置 | 数值 | 状态 |
|---|---|---|
| `engine.py` DOMS 上升段 | 24h 线性爬升 | [R4] 只说延迟出现，具体斜率是估算 |
| `engine.py` DOMS 平台段 | 正弦平台 | 估算，为贴合「48-72h 达峰」的定性描述 |
| `engine.py` DOMS 消退 | 72h 后 96h 内线性归零 | 估算（[R4] 未给消退速率）|
| `engine.py` 习惯衰减 | 14 天 | 估算（[R5] 未给保护消退时间）|
| `engine.py` 习惯抑制 | 70% | 估算，落在 SMD 0.51-1.23 区间 |
---

## [R6] Durmer JS, Dinges DF.
**Neurocognitive consequences of sleep deprivation.**
*Seminars in Neurology*. 2005 Mar;25(1):117-129. doi:10.1055/s-2005-867080. PMID: 15798944

**查阅方式**：PubMed E-utilities 读取摘要。

**用于**：`engine.py` 的强制睡眠（微睡眠）机制。

**取用内容（原文表述）**：

> "Microsleeps, sleep attacks, and lapses in cognition increase with sleep loss
> as a function of state instability."

> "Sleep deprivation studies repeatedly show a variable (negative) impact on
> mood, cognitive performance, and motor function due to an increasing sleep
> propensity and destabilization of the wake state."

**支撑的建模选择**：
- 连续清醒越久，不可抗拒的睡眠发作（sleep attacks）概率越高
- 这不是「意志力问题」，而是觉醒状态的不稳定 —— 所以建模成强制事件

**模型取值**：
- 阈值 20 小时：**估算**。文献只给「随睡眠剥夺加剧」的定性结论，
  未给出具体的「第 N 小时必然睡着」的数值。20 小时是保守选择。
- 强制睡眠持续 6 小时：**估算**，代表一次补偿性长睡。
