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

---

## [R7] Freckmann G, Hagenlocher S, Baumstark A, Jendrike N, Gillen RC, Rössner K, Haug C.
**Continuous glucose profiles in healthy subjects under everyday life conditions and after different meals.**
*Journal of Diabetes Science and Technology*. 2007 Sep;1(5):695-703. doi:10.1177/193229680700100513. PMID: 19885137

**查阅方式**：Europe PMC REST 取回题录 + 摘要数值（经文献单元核对）。

**用于**：`body/engine.py` 餐后血糖曲线的定标目标。

**取用内容**：
- 24 名健康志愿者。日常平均葡萄糖 89.3±6.2 mg/dL（4.96 mmol/L）
- **餐后峰值时点平均 46–50 min**
- 早餐峰值 132.3±16.7 mg/dL（7.34 mmol/L，范围 101–168）
- 高纤维/蛋白/脂肪餐峰值仅 99.2±10.5 与 122.1±20.4 mg/dL，且下降更慢

**支撑的建模选择**：
- 胃排空半时 90 → 40 min，十二指肠 12 → 6 min，空肠 14 → 8 min
  （把串联延迟压回生理量级，峰值时点 95-99 min → 63-73 min）
- 健康餐后峰值目标带 6.0–7.8 mmol/L

**诚实标注**：本模型峰值时点仍未完全达到文献的 46–50 min（当前 63–73 min）。
剩余差距来自「胃→十二指肠→空肠→肠糖池→血」的五级串联结构本征延迟，
以及本模型胃排空按体积一阶衰减、而非真实的分相排空（lag phase + 线性相）。
未进一步压缩是为避免把胃排空调到非生理值。

---

## [R8] Shah VN, DuBose SN, Li Z, Beck RW, Peters AL, Weinstock RS, et al.
**Continuous Glucose Monitoring Profiles in Healthy Nondiabetic Participants: A Multicenter Prospective Study.**
*The Journal of Clinical Endocrinology and Metabolism*. 2019 Oct;104(10):4356-4364. doi:10.1210/jc.2018-02763. PMID: 31127824

**查阅方式**：Europe PMC REST 取回题录 + 摘要数值。

**用于**：`body/engine.py` 血糖「是否落在正常区间」的验收标准。

**取用内容**：
- 12 中心、153 名 7–80 岁健康非糖尿病人，Dexcom G6 盲戴 ≤10 天
- 平均血糖 98–99 mg/dL（5.4–5.5 mmol/L）
- **70–140 mg/dL（3.9–7.8 mmol/L）时间占比中位数 96%（IQR 93–98%）**
- **>140 mg/dL（7.8 mmol/L）中位 2.1%，约 30 min/天**

**支撑的建模选择**：验收线取「餐后 2h < 7.8 mmol/L」与「峰值常态 6.0–7.5」。

---

## [R9] Vlachos D, Malisova S, Lindberg FA, Karaniki G.
**Glycemic Index (GI) or Glycemic Load (GL) and Dietary Interventions for Optimizing Postprandial Hyperglycemia in Patients with T2 Diabetes: A Review.**
*Nutrients*. 2020 May;12(6):1561. doi:10.3390/nu12061561. PMID: 32471238

**查阅方式**：Europe PMC REST 取回题录 + 摘要。

**用于**：`body/engine.py` 的 GI/GL 双通道实现。

**取用内容**：
- 低 GI 食物消化吸收更慢，餐后血糖与胰岛素反应更平缓
- **GL（血糖负荷）= GI × 碳水克数**，把"质量"与"数量"合并，
  对餐后反应的解释力优于单用 GI
- 不要把 GI 同时用来缩放峰值高度与速率（应分离）

**支撑的建模选择**（本模型据此把 GI 拆成两条独立通路）：
- **速率侧**：GI 分快慢两池，快池入血半时 8 min、慢池 42 min
- **幅度侧**：`_carb_to_blood_for_gi()` 按加权 GI 折算入血比例
  （GI 100 → 满额 0.70；GI 30 → 约 0.455），差额按既有逻辑回补肝糖原

---

## [R10] Rizza RA, Mandarino LJ, Gerich JE.
**Dose-response characteristics for effects of insulin on production and utilization of glucose in man.**
*American Journal of Physiology*. 1981 Jun;240(6):E630-E639. doi:10.1152/ajpendo.1981.240.6.e630. PMID: 7018254

**查阅方式**：Europe PMC REST 取回题录 + 摘要数值。

**用于**：作为「肝糖输出 vs 外周摄取」相对敏感性的**方向性依据**。

**取用内容**：
- 15 名健康人，阶梯胰岛素钳夹 + [3-³H]葡萄糖示踪
- **肝糖输出半最大抑制 29±2 μU/mL，完全抑制约 60 μU/mL**
- **外周葡萄糖利用半最大需 55±7 μU/mL**（显著高于肝，P<0.01）
- 基础葡萄糖产生/利用速率 1.93±0.11 / 1.92±0.13 mg·kg⁻¹·min⁻¹

**诚实标注 —— 本模型的当前状态**：
- 曾试图把这两个 EC50 直接代入 `engine.py` 的 Hill 方程，
  **实测造成回归**（白米饭峰值 8.11 → 10.87），已回退。
- 原因：本模型的 `insulin` 字段是 0–1.6 的**相对量纲**，不是 μU/mL，
  两者不能直接换算代入。要真正落地该文献，须先把胰岛素量纲整体
  换算到 μU/mL —— 属独立改动，不在本次范围。
- 文献结论目前**未被代码使用**，仅作为未来改动的依据备查。

---

## 未采用 / 未核实清单（2026-11-16 新增）

完整清单见 `docs/血糖与睡眠文献依据.md` 文末。与本次代码改动直接相关的：

| 事项 | 状态 |
|---|---|
| 「连续 4 小时睡眠」作为健康阈值 | **文献中不存在**，纯工程假设（`min_sleep_bout_hours`） |
| 「午睡 ≤30 min 不影响夜间睡眠」 | **文献不支持**，证据薄弱，未用作依据 |
| PSQI 回顾窗口 | **PSQI 官方是 1 个月**，本模型的「近 7 日」是类比，**不称 PSQI** |
| 人类葡萄糖分布容积 14–16 L 的一手测定 | 未找到；[R8 系列之外的 Mari 2011] 数据来自犬 |

---

## [R11] Nicolò A, Girardi M, Bazzucchi I, Felici F, Sacchetti M.

**题录**：Respiratory frequency and tidal volume during exercise: differential control and unbalanced interdependence. *Physiol Rep*. 2018;6(21):e13908. doi:10.14814/phy2.13908. PMID 30393984. PMCID PMC6215760.

**查阅方式**：PMC 全文（开放获取）。

**用于**：确定运动通气的执行器官是**潮气量（VT）而非呼吸频率（fR）**，据此重写 engine.py 的 _respiratory()。

**取用内容**：
- fR 与 VT 由不同输入调控、时序不同，二者是「非平衡的相互依赖」。
- 中等强度运动、代谢性刺激占主导时，是 **VT 承担分钟通气的增加**。
- **单独提高 fR 会增加死腔通气并导致 CO₂ 潴留；提高 VT 不会** —— 因为解剖死腔按每次呼吸固定扣减（本模型 150 mL）。

**落地结果**：被控量改为肺泡通气量 VA，由它反解 tidal：`tidal = VA*1000/rr + 150`。
原实现只给 rr 加 8（固定 tidal=500），死腔占比恒为 0.30，实测运动 pCO₂ 冲到 59、误报呼吸性酸中毒。

---

## [R12] Whipp BJ, Davis JA, Wasserman K.

**题录**：Ventilatory control of the isocapnic buffering region in rapidly-incremental exercise. *Respir Physiol*. 1989;76(3):357-367. doi:10.1016/0034-5687(89)90076-5. PMID 2501844.

**查阅方式**：PubMed 题录 + 摘要。

**用于**：解释「酸在增加但 PaCO₂ 不降」的反直觉现象，避免把运动中的 CO₂ 升高误判为呼吸性酸中毒。

**取用内容**：
- 递增负荷运动中，超过无氧阈后的一段范围内 **PaCO₂ 并不下降**，称「等碳酸缓冲段」（isocapnic buffering）。
- 原因是乳酸被 HCO₃⁻ 缓冲时**额外产生 CO₂**，同步增加的通气把它排掉，故 PaCO₂ 维持。
- 该现象不是「静态的 PCO₂ 不变」，而是对代谢性酸中毒的通气反应。

**落地结果**：_acidbase() 中乳酸只消耗 HCO₃⁻，**不重复计入 CO₂** —— 缓冲产生的 CO₂ 已由通气项排出，避免同一份酸被记两次。

---

## [R13] Stickland MK, Lindinger MI, Olfert IM, Heigenhauser GJF, Hopkins SR.

**题录**：Pulmonary Gas Exchange and Acid-Base Balance During Exercise. *Compr Physiol*. 2013;3(2):693-739. doi:10.1002/cphy.c110048. PMID 23720327. PMCID PMC8315793.

**查阅方式**：PMC 全文（作者稿）。

**用于**：确认运动时 PaCO₂ 的正常范围，以及运动性酸中毒的性质（代谢性为主）。

**取用内容**：
- 多数受试者在递增负荷试验中，**直到接近 V̇O₂max 前，PaCO₂ 都维持在静息值几个 mmHg 以内**。
- 运动通气的驱动是多来源的：中枢指令、III/IV 类肌肉传入、外周/中枢化学感受器 —— **不是「运动时把频率调高」这种单一开环项**。

**落地结果**：
- 运动中 pco2_set 取 38.5 mmHg（轻度过度通气），不再让 pCO₂ 冲到 59。
- 运动性酸中毒改由乳酸降低碱剩余的通路产生；配合 [R14] 的 Van Slyke 方程后，重运动 pH 落在 7.32~7.40（真实 7.30~7.35）。

---

## [R14] Siggaard-Andersen O.

**题录**：The van Slyke equation. *Scand J Clin Lab Invest Suppl*. 1977;146:15-20. doi:10.3109/00365517709098927. PMID 13478.

**查阅方式**：PubMed 题录（原文为付费期刊，未取全文）；方程形式核对自临床血气仪通用算法与多份教材式表述。

**用于**：建立「碱剩余 BE ↔ pH ↔ HCO₃⁻」三者之间的换算关系，是 `_acidbase()` 的核心方程。

**取用内容**：
- 标准碱剩余（SBE）方程：`BE = (1 − 0.023×Hb) × ((HCO₃⁻ − 24.4) + (2.3×Hb + 7.7) × (pH − 7.40))`
- 取 Hb = 5 g/dL 即为「标准碱剩余」口径——该口径把血浆与红细胞之间的缓冲分摊折算为细胞外液等效值，是临床血气仪的通用做法。
- 碱剩余的意义：把所有缓冲对吸收完酸之后「还剩多少碱」压缩成一个标量，由此反解 pH 时，**非碳酸氢盐缓冲被自动计入**。

**落地结果**：`_acidbase()` 不再由 HCO₃⁻ 直接套 Henderson-Hasselbalch，而是：
1) 乳酸负荷 → 碱剩余下降（1 mmol/L = 1 mEq/L）；
2) 由 BE 与 pCO₂ 用二分法反解 pH（24 次迭代，精度约 8e-8）；
3) 再由 pH 与 pCO₂ 反算 HCO₃⁻，保证三者自洽。

---

## [R15] Krbec M, Waldauf P, Zadek F, Brusatori S, Zanella A, Duška F, Langer T.

**题录**：Non-carbonic buffer power of whole blood is increased in experimental metabolic acidosis: an *in-vitro* study. *Front Physiol*. 2022;13:1009378. doi:10.3389/fphys.2022.1009378.

**查阅方式**：Frontiers 开放获取全文。

**用于**：确认「血液缓冲不止碳酸氢盐」这一前提，为非碳酸氢盐缓冲项提供依据。

**取用内容**：
- 人血中有两类缓冲限制 pH 波动：**碳酸氢盐缓冲系统**与**非碳酸（non-carbonic）缓冲**。
- 非碳酸缓冲由血浆蛋白、磷酸盐，以及**最重要的红细胞内血红蛋白**构成（引 Van Slyke 1922 原始工作）。
- 只有非碳酸缓冲能限制**呼吸性**紊乱引起的 pH 变化；代谢性酸中毒时碳酸氢盐被消耗，而非碳酸缓冲的浓度保持。
- 该研究实测 βNC 随强离子差下降而升高（每降 1 mEq/L 升 0.36 mmol/L），说明缓冲能力并非常数。

**落地结果**：模型中 HCO₃⁻ 不再是唯一的缓冲承担者；非碳酸氢盐缓冲经 [R14] 的碱剩余项折算。**未**实现 βNC 随酸中毒上升的动态效应（本模型取常数），属已知简化。

---

## 酸碱部分的「估算」标注（2026-11-16 新增）

| 事项 | 状态 |
|---|---|
| 乳酸 : 碱剩余 1 : 1 | **教科书结论（Stewart 强离子差框架），未找到一手测定文献**，按估算处理（engine.py 内已标注） |
| 非碳酸氢盐缓冲当量 `_M_HB = 40.0` | **经验标定值**。标准 SBE 口径（Hb 5 g/dL）为 19.2，只覆盖细胞外液；体内约 60% 缓冲在细胞内，故上调。标定目标：乳酸 10 → pH≈7.29（实测 7.30） |
| 细胞内缓冲占约 60% | 引自 Deranged Physiology（教学网站，二手转述 Van Slyke 原始工作），**未核对一手测定** |
| 肾脏代偿系数「pCO₂ 每高 10 mmHg，HCO₃⁻ 升 3.5」 | 教科数量级，未逐条核对原始研究 |
| 运动时 pco2_set = 38.5 mmHg | 由 [R13] 的「维持静息值几个 mmHg 以内」取定，非精确测定值 |
| 告警阈值（运动中 7.10 / 恢复期 7.20 / 静息 7.30） | **工程取值，非文献阈值**。真实重运动 pH 可低至 7.1~7.2，故运动中放宽；静息沿用 7.30 |

---

## [R16] 糖原容量与餐后葡萄糖处置（2026-11-16 血糖修复新增）

**用途**：为「糖原储存必须是血糖的真实去路」与「储存容量上限」两个改动提供依据。

| 来源 | 取用内容 |
|---|---|
| *Advances in Physiology Education* (2024), doi:10.1152/advan.00023.2024 | 人体肝糖原约 100 g、骨骼肌约 400 g（吸收后状态）；肝可储 100~120 g |
| *Wikipedia: Glycogen*（二手汇总，仅作量级参考） | 骨骼肌糖原总量 400~900 g，随训练与饮食大幅波动 |
| *Medscape: Postprandial Glucose Regulation*（综述） | 餐后约 30% 葡萄糖由肝脏处置，15~20% 进入肌糖原；糖原合成是主要去路之一 |
| *ProSciento / KBI 高胰岛素-正常血糖钳夹资料* | 最大葡萄糖处置率 M 值约 9~12 mg/kg/min（70 kg ≈ 3.9~4.7 mmol/min） |

**落地结果**：
- 糖原上限由 500 g 提到 **800 g**（考虑肌糖原上限可达 900 g + 肝 100 g）。
- 糖原储存改为**从血糖真实扣除**（此前只记账不扣糖，违反质量守恒）。
- 胰岛素上限 1.60 → 2.20（健康人餐后胰岛素可达空腹 5~10 倍）。
- 补 **肾糖阈**：血糖 >10 mmol/L 时超出部分经尿排出（旧实现只算尿量、不扣血糖）。

**诚实标注**：
- 肾糖阈排泄系数 0.20 mmol/min per mmol/L 为**工程取值**，量级参照肾血浆流量与最大重吸收率，未见一手测定。
- 处置系数 0.19 经实测对比：提高到 0.24 只把极端场景峰值从 14.7 降到 13.1，却使静息下限从 4.78 降到 4.46，故**未采用**。
- **已知未尽（v1.2.3 时点）**：~~连续 3 天每日三餐高碳水（糖原顶格）时峰值仍可达约 14.7，高于健康人实测（约 9~10）；根因是模型处置能力在高糖段饱和，属结构性限制，需另案重做处置模型。~~
- **2026-11-16 已闭合**：见 [R17]；同一靶场实测 12.93 → 9.18。

---

## [R17] 葡萄糖有效性（质量作用）与肠吸收上限（2026-11-16 血糖收尾，issue #6）

**用途**：闭合 [R16] 里标注的「已知未尽」—— 连续多日高碳水、糖原顶格后血糖仍冲到 12.9~14.7。诊断结论是：外周处置在高糖段已接近生理上限，**不能**再抬 0.19（会让静息下限掉到 4.46，已否决），缺的是一条独立的、只在高糖段起作用的去路。

| 来源 | 取用内容 |
|---|---|
| Bergman 最小模型（minimal model）的 S_G 参数 —— **二手转述**（综述/教科书口径） | 葡萄糖有效性（非胰岛素依赖的葡萄糖清除）常见取值约 0.01~0.03 /min；本模型取 **0.020 /min** |
| issue #6 提出的量级 —— **未见一手测定** | 真实肠道葡萄糖吸收约 1~1.5 g/min，载体介导（SGLT1/GLUT2），故设上限 1.5 g/min（8.33 mmol/min） |
| [R16] 已引的钳夹 M 值 9~12 mg/kg/min | 用于确认「外周摄取已接近生理上限」，因此抬的是新去路而不是旧系数 |

**落地结果**：
- `GE_SLOPE_DEFAULT = 0.020`（/min）、`GE_FLOOR_DEFAULT = 5.0`（mmol/L）：只对高出静息基线的部分生效，低血糖段恒为 0。
- `ABSORB_CAP_MMOL_MIN = 8.33`（=1.5 g/min）：快慢两池共享预算，超限部分留在肠池里下一分钟继续吸，总量守恒。

**诚实标注**：
- S_G 是**工程取值**：模型先按既有曲线标定（米饭峰值约 7.0，文献 6~7.5），再在其上补高糖段斜率，不是把文献绝对值直接代入。胰岛素量纲仍是相对量 0~2.2，未换算到 μU/mL —— 这一点与 [R16] 未变的限制相同。
- 吸收上限 1.5 g/min 来自 issue 转述，**未核实一手测定**；常规三餐实测吸收峰值 1.06 g/min，不触限，只有极端大份才会被削。
- 副作用实测：静息下限 5.24 → 5.16，禁食 48 h 最低 5.17，高强度运动 3 h 最低 5.17，均未进低血糖区。
- **仍已知未尽（v1.2.3 时点）**：~~连续三天每餐吃到两倍份量（约 1800 kcal/餐）时峰值仍约 14.8，属超出标定范围的极端输入，未再调参。~~
- **2026-11-16 已闭合**：见 [R18]（糖原满位后的脂肪合成）；同一靶场饱和后稳态 15.0 → 10.85。

---

## [R18] 糖原满位后的溢出：脂肪合成（de novo lipogenesis，2026-11-16 补）

**用途**：极限测试发现糖原顶格（800 g）后，若碳水继续灌入，血糖只能靠「爬高」来匹配吸收速率 —— 双倍大份稳态 15.0、未加质量作用时 20.5。补一条真实存在、但此前完全没建模的出口。

| 来源 | 取用内容 |
|---|---|
| **Acheson KJ, Schutz Y, Bessard T, Anantharaman K, Flatt JP, Jéquier E. Glycogen storage capacity and de novo lipogenesis during massive carbohydrate overfeeding in man. Am J Clin Nutr. 1988;48(2):240-247.**（原文 PDF 已核） | ①糖原可容纳约 **500 g 增益**，之后净脂肪合成才开始增加体脂；②大量碳水过载时约 **475 g 碳水/天 → 150 g 脂肪/天**（≈3.17:1）；③碳水利用率升至 **1010±37 g/天**；④原文结论：如此大量的碳水**并未造成循环血糖的异常升高** |

**落地结果**：
- `DNL_TRIGGER_FRAC = 0.85`：糖原达到 85% 上限（680 g）才开始，对应 Acheson「约 500 g 增益后」的量级。
- `DNL_CAP_MMOL_MIN = 3.5`（≈38 g 葡萄糖/h）、`DNL_GAIN_PER_MMOL = 0.85`：速率随血糖高出 7.0 的部分上升并封顶。
- `DNL_GLUCOSE_G_PER_FAT_G = 3.17`：仅用于把扣掉的葡萄糖折算成脂肪克数做自证，**脂肪不入任何账本**（本模型没有脂肪代谢，产物直接丢弃）。

**实测（7 天靶场）**：

| 场景 | 未加 DNL 峰值 | 加 DNL 后稳态 | 日产脂 |
|---|---|---|---|
| 三餐双倍大份 | 15.08 | **10.86** | 38~41 g |
| 常规三餐重碳水 | 10.90~11.19 | **9.14** | 11~13 g |
| 每天五餐 | 10.99~11.13 | **9.22~9.27** | 23~27 g |

> 数值取当前靶场：血糖测例已关闭自动饮水，排除「随机挑到含糖饮料」对峰值的干扰。

**诚实标注**：
- 3.17:1 是 Acheson 原文两个日总量之比（475 g CHO / 150 g 脂肪），属**实验回归值**，不是每分子葡萄糖的化学计量；用它算出的日产脂 39~42 g 比原文 150 g 低，说明本模型的高糖段处置尚未把 DNL 用满，方向安全。
- 速率上限 3.5 mmol/min 是**工程取值**：按原文明示「并未出现异常高血糖」反推，使饱和后稳态回到 10~11 量级；没有一手测定支撑瞬时 DNL 速率。
- 触发阈值 0.85×800 = 680 g 与 Acheson 的「约 500 g 增益」并非同一基准（本模型的总容量口径为 800 g，已含肌肉糖原上限），属量级对齐而非数值换算。
- **未做**：产出的脂肪没有进入体重/体脂/血脂任何通路（按需求丢弃），所以过量碳水的长期后果（增脂、胰岛素抵抗）仍然不会在模型里显现。

---

## [R19] 运动的心率与体温动力学（2026-11-17 新增）

**用途**：修正运动时心率「秒级跳到目标」与体温无负反馈两个问题。三条要落地的约束如下。

| 来源 | 取用内容 |
|---|---|
| Silva LRBE, Zamunér AR, Gentil P, et al. Cardiac Autonomic Modulation and the Kinetics of Heart Rate Responses in the On- and Off-Transient during Exercise. *Front Physiol*. 2017;8:542. doi:10.3389/fphys.2017.00542 | 运动开始（on-transient）是**迷走撤退 + 交感激活**的组合，心率上升快；恢复（off-transient）主要靠**迷走再激活**，因此**上升与恢复不对称**，恢复明显更慢。原文用指数模型拟合，τ 即恢复速度 |
| 心率恢复（HRR1）健康成人常模（Cleveland Clinic 与年龄分层常模，二手汇总） | 运动后 **1 分钟回落 15~25 bpm**；20–29 岁 ≥22，60–69 岁 ≥18。回落偏小提示体能差或自主神经调节异常 |
| *Core body temperature responses during competitive sporting events: A narrative review*（PMC10588574）；高温运动科普资料（二手） | 剧烈运动核心温度常见 **38.3~39.4 ℃**；高温环境长时间运动可达 40 ℃。中等强度运动的升幅文献未给单一值，按 **0.5~1.0 ℃** 处理 |
| *Physiology, Temperature Regulation*（StatPearls, NBK507838） | 体温由下丘脑调定点调节，散热主要靠**出汗蒸发**；核心温度是出汗率的主要驱动，蒸发效率受湿度影响 |

**落地结果**：
- 心率改为**方向性指数逼近**：上升 τ = 20 s（迷走撤退，快），恢复 τ = 95 s（迷走再激活，慢）。实测中强度 HRR1 = 17 bpm、停止 5 分钟回到基线 ±5。
- 心率取 `HRmax × 60% / 72%`（见 [R20]），产热 +0.55 / +1.10 ℃；旧实现是固定 +40 bpm / +0.8 ℃。目前运动**一律按慢跑处理**，快跑档代码保留但不可达（issue #10）。
- 新增**出汗散热负反馈** `base -= 0.35 × sweat`：出汗不再只扣体水，而是把核心温度拉回来。旧实现里 sweat 与体温完全解耦，环境热 + 运动 + 黄体期可以叠加到 38 ℃ 被判「低烧」。

**诚实标注**：
- τ(20 s / 95 s) 是**工程取值**：按「上升快、恢复慢」「HRR1 15~25 bpm」两条定性约束反解，不是从文献直接读出的时间常数（原文 τ 的单位与人群口径不同，且对象是代谢综合征女性）。
- 中等强度 0.5~1.0 ℃ 的升幅是**通用生理常识口径**，未找到单一权威原文；高强度 38.3~39.4 ℃ 来自综述与科普，属二手。
- 未建模：皮肤血流、湿度对蒸发效率的影响、运动性脱水对体温的二次抬高（高温长运动实际会更热）。当前模型在这些方向上**偏保守**（宁可不发烧）。

---

## [R20] 心率随年龄的定标（2026-11-17 新增）

**用途**：把心率从「固定基线 + 固定加数」改成按年龄与最大心率百分比计算。默认年龄 18 岁，公式保留。

| 来源 | 取用内容 |
|---|---|
| Tanaka H, Monahan KD, Seals DR. Age-predicted maximal heart rate revisited. *J Am Coll Cardiol*. 2001;37(1):153-156 | 最大心率 **HRmax = 208 − 0.7 × 年龄**（18,712 人元分析；比 220−年龄 更准，尤其 40 岁以上）。18 岁 ≈ 195 bpm，60 岁 ≈ 166 bpm |
| AHA / Mayo Clinic / Cleveland Clinic（二手汇总） | 成人静息心率正常区间 **60~100 bpm**；体能好的人可低至 40~50 |
| 睡眠心率常模（二手汇总） | 健康年轻成人睡眠心率 **45~70 bpm**（体型/体能差），常见 40~60；最低点常在深睡 |
| AHA 与 Zone 2 资料（二手汇总） | 中等强度 = 50~70% HRmax；**慢跑 = Zone 2 = 60~70% HRmax** |

**落地结果**：
- `age` 是引擎**内置常量 18**（`BodyEngine.age`），不进配置、不进面板 —— 见 [issue #9](https://github.com/ShuangziG/maibot-body-emulator/issues/9)。
- 运动目标心率改为 `max(当前公式, HRmax × 强度百分比)`：慢跑 60%、快跑 72%——年龄越大目标越低，这是固定 `+40 bpm` 做不到的。
- 静息基线 `60.0 + 0.20 × (年龄 − 18)`：18 岁日间约 73、夜间深睡约 61（跌幅 16%）；60 岁日间约 82。
- 呼吸频率与 CO₂ 产热也按强度分档：慢跑 +8 次/分、+0.35 L/min；快跑 +14、+0.60。

**诚实标注**：
- 静息随年龄的斜率 **0.20 bpm/岁 是工程取值**：文献只说成人静息区间 60~100、随年龄大致稳定或轻微上移，没有给出可直接代入的斜率。取 0.20 是为了让 18 岁 ≈ 73、60 岁 ≈ 82 落在常见区间。
- 睡眠心率由两块构成：一是交感/迷走张力（`r_beta1` / `r_m2`），二是显式的**深睡项** `−4.0 × 睡眠深度`。取 4.0 的依据是两条约束同时满足：①绝对值 61 落在文献睡眠心率 45~70 内；②夜间跌幅 (73−61)/73 = **16%**，落在文献夜间心率跌幅 10~20% 内。若取 6.0（早期试值）绝对值 58 仍合格，但跌幅 21% 超出常见区间，故定为 4.0。
- 「慢跑 = 60% HRmax」取的是 Zone 2 下限（60~70% 的保守端），与「目前只按最低档算」的要求一致；快跑 72% 也仍在中等强度上沿，未做冲刺级（>85%）档。
- 年龄目前**固定 18 岁**，配置项存在但请勿随意改；改年龄会同时改变静息基线与运动目标两处。
---

## 情绪神经内分泌（2026-11-18 v1.3.0 新增，[R21]-[R35]）

**用途**：为情绪系统（欣快/烦躁双向轴 + 糖皮质激素受体）提供依据。
以下文献均经 PubMed E-utilities（esummary 取元数据、efetch XML 取摘要）实测读取，
不是凭记忆写入的。检索式与原始返回存于 `_emotion_probe/`（开发用，不随插件发布）。

**[R21] Knezevic E, Nenic K, Milanovic V, et al.** The Role of Cortisol in Chronic Stress,
Neurodegenerative Diseases, and Psychological Disorders. *Cells*. 2023 Nov 29.
doi:10.3390/cells12232726. PMID 38067154.
取用：皮质醇由**肾上腺**产生；释放受**视交叉上核**调控昼夜节律并激活 **HPA 轴**；
慢性应激/疾病/衰老致其调节紊乱；**社会等级等社会因素**也会影响皮质醇水平。

**[R22] Lei AA, Phang VWX, Lee YZ, et al.** Chronic Stress-Associated Depressive Disorders:
The Impact of HPA Axis Dysregulation and Neuroinflammation on the Hippocampus - A Mini Review.
*Int J Mol Sci*. 2025 Mar 24. doi:10.3390/ijms26072940. PMID 40243556.
取用：HPA 轴**持续**激活破坏皮质醇调节→身心健康下降；与炎症、氧化应激交互→神经炎症→
**海马**结构与功能改变，而海马是**情绪调节**与认知的关键结构。

**[R23] Berridge CW, Waterhouse BD.** The locus coeruleus-noradrenergic system: modulation of
behavioral state and state-dependent cognitive processes. *Brain Res Brain Res Rev*. 2003 Apr.
doi:10.1016/s0165-0173(03)00143-7. PMID 12668290.
取用：蓝斑向全中枢供去甲肾上腺素；促进**显著性**信息处理；参与**觉醒状态**的启动与维持；
在觉醒态内以**浓度依赖**方式调节感觉、注意与记忆。

**[R24] Berridge KC, Robinson TE.** What is the role of dopamine in reward: hedonic impact,
reward learning, or incentive salience? *Brain Res Brain Res Rev*. 1998 Dec.
doi:10.1016/s0165-0173(98)00019-8. PMID 9858756.
取用：原文明确回答——多巴胺**不**介导享乐影响、**不**介导享乐性奖赏学习，而是介导
**动机显著性（incentive salience）**，且该功能**可与享乐分离**。
证据：伏隔核+新纹状体多巴胺耗竭达 99% 的大鼠仍保留正常享乐/厌恶反应。
**建模含义**：本插件不实现多巴胺；若将来实现，只能做「想要」不能做「快乐值」。

**[R25] Moncrieff J, Cooper RE, Stockmann T, Amendola S, Hengartner MP, Horowitz MA.**
The Serotonin Theory of Depression: A Systematic Umbrella Review of the Evidence.
原刊 *Molecular Psychiatry* 2022（doi:10.1038/s41380-022-01661-0，见 [R26] 摘要转述）；
本次读取的是 *Focus (Am Psychiatr Publ)* 2026 转载版，doi:10.1176/appi.focus.26024112. PMID 42538881.
取用：伞状综述覆盖 5-HT 及 5-HIAA 浓度、5-HT1A 受体结合、SERT 水平、色氨酸耗竭、
SERT 基因等；**结论不支持**抑郁与血清素浓度/活性降低存在一致关联。
**诚实标注**：本条为**转载版**元数据，原刊条目未直接读取。

**[R26]** The myth of serotonin theory of depression: an analysis of umbrella review
methodologies and clinical implications. *Psychiatr Danub*. 2025 Sep. PMID 40982824.
取用：对 [R25] 的**方法学批评**——指出其事后修改方案、选择性报告、下调血清素能发现权重
等问题，认为结论被削弱，主张更细致的理解。
**用途**：本条存在的意义是说明 [R25] **并非学界共识**。本插件因此**不实现血清素**；
若要实现，只能作为「情绪稳定性/易感度」参数，不能做「5-HT 低 → 抑郁」。

**[R27] Jurek B, Neumann ID.** The Oxytocin Receptor: From Intracellular Signaling to Behavior.
*Physiol Rev*. 2018 Jul 1. doi:10.1152/physrev.00031.2017. PMID 29897293.
取用：催产素/OXTR 在生殖、**社会与情绪行为**中的作用；除射乳与子宫收缩外，
还涉及性行为、母性行为、**配对联结**，以及**焦虑、信任**等。

**[R28] Bodnar RJ.** Endogenous opiates and behavior: 2014. *Peptides*. 2016 Jan.
doi:10.1016/j.peptides.2015.10.009. PMID 26551874.
取用：内源性阿片系统年度综述（第 37 期）；覆盖**疼痛与镇痛**、**应激与社会地位**、
摄食饮水、耐受与依赖等行为效应。本插件的内啡肽目前只用于镇痛。

**[R29] Hantsoo L, Epperson CN.** Allopregnanolone in premenstrual dysphoric disorder (PMDD):
Evidence for dysregulated sensitivity to GABA-A receptor modulating neuroactive steroids across
the menstrual cycle. *Neurobiol Stress*. 2020 May. doi:10.1016/j.ynstr.2020.100213. PMID 32435664.
取用：PMDD 是**黄体期**出现的重度情绪障碍，核心症状为**情感不稳、易激惹、抑郁心境、焦虑**，
且**应激敏感性升高**；可概念化为对神经活性类固醇**敏感性次优**的障碍；
别孕烯醇酮是 GABA-A 正向别构调节剂；ALLO-GABA 对 HPA 轴控制不良→黄体期应激敏感性升高。
**建模含义（重要）**：关键不是激素水平高低，而是**敏感性失调**。
本插件现有 `pms_load` 走的是「黄体后期激素双降」，**机制上与文献不符**（症状方向对了）。

**[R30] Tiranini L, Nappi RE.** Recent advances in understanding/management of premenstrual
dysphoric disorder/premenstrual syndrome. *Fac Rev*. 2022. doi:10.12703/r/11-11. PMID 35574174.
取用：PMS/PMDD 为**黄体期**障碍；卵巢类固醇（雌二醇、孕酮）被认为是**致病效应物**，
但关键特征是 **GABA 能中枢抑制系统对别孕烯醇酮的敏感性改变**；另有血清素可用性降低参与。

**[R31] Dekkers TJ, van Rentergem JAA, Meijer B, et al.** A meta-analytical evaluation of the
dual-hormone hypothesis. *Neurosci Biobehav Rev*. 2019 Jan. doi:10.1016/j.neubiorev.2018.12.004.
PMID 30529754.
取用：30 篇文献/33 研究/49 效应量/**n=8538**；睾酮×皮质醇交互对地位相关行为
**仅边际支持**，效应量极小（**r = -0.061, p = .026**）。
**建模含义**：本插件**不实现睾酮**——投入产出比差，且会强化刻板印象。

**[R32] Liu J, Clough SJ, Hutchinson AJ, et al.** MT1 and MT2 Melatonin Receptors:
A Therapeutic Perspective. *Annu Rev Pharmacol Toxicol*. 2016.
doi:10.1146/annurev-pharmtox-010814-124742. PMID 26514204.
取用：褪黑素由**松果体**（及视网膜局部）按**昼夜节律**合成释放，**白天低、夜间高**；
经 MT1/MT2 两种 GPCR 作用于睡眠与昼夜节律、**情绪障碍**、学习记忆等。

**[R33] Kautz M, Charney DS, Murrough JW.** Neuropeptide Y, resilience, and PTSD therapeutics.
*Neurosci Lett*. 2017 May 10. doi:10.1016/j.neulet.2016.11.061. PMID 27913193.
取用：NPY 系统是**应激反应的关键中介**与 PTSD 的潜在治疗靶点；大量前临床证据支持
NPY 调节应激反应与**焦虑**。

**[R34] Milaneschi Y, Simmons WK, van Rossum EFC, et al.** Depression and obesity:
evidence of shared biological mechanisms. *Mol Psychiatry*. 2019 Jan.
doi:10.1038/s41380-018-0017-5. PMID 29453413.
取用：抑郁与肥胖**双向**相关；共享通路包括 **HPA 轴**、免疫炎症激活、
能量代谢的神经内分泌调节因子（**leptin**、insulin）与微生物组。

**[R35] Hirschfeld RM.** History and evolution of the monoamine hypothesis of depression.
*J Clin Psychiatry*. 2000. PMID 10775017.
取用：单胺假说的历史脉络（仅作背景，未取具体数值）。
**诚实标注**：本条无 DOI，仅经 esummary 核实题录，摘要未取到。

### 情绪部分未能取得一手文献的主题（不得当作结论）

| 主题 | 状态 |
|---|---|
| 甲状腺激素 T3/T4 与抑郁/焦虑 | 检索 3 次均返回贫血、Graves 病管理、病例报告等偏题结果，**未取到对口综述** |
| 胃饥饿素 ghrelin 与易激惹（「饿怒」） | 命中双相障碍/进食障碍等，**未取到直接证据** |
| 血管加压素 vasopressin 与社会行为 | 命中「爱的分子基础」、不忠遗传学等，**质量不足，未采用** |

因此 `hunger → stress` 的系数（`max(0, hunger-70)*0.35`）仍是**估算**，
`pms_load*18`、催产素/NPY 的时间常数同样是**估算**，均未标为文献支撑。

### 情绪部分的「估算」标注

| 位置 | 数值 | 状态 |
|---|---|---|
| GCR Hill 半饱和常数 Kd | 0.8 | 形态依据 [R21][R22]（受体饱和），**具体数值是工程取值** |
| 情绪增益系数 | 1.8 | 工程标定，按「+40 应到『挺开心』档」反推 |
| 情绪→压力耦合 | 0.55 | 工程标定，使满值 ±100 对应 ∓55 分压力 |
| 瞬时情绪衰减 | 360 s / 180 s | 体感目标（一句话的影响数分钟内过去），无一手测定 |
| 欣快/烦躁的合成权重 | 见 engine._affect | **工程映射，非文献系数** |

---

## 血液、免疫、营养与生活习惯（2026-11-24 v1.4.0 新增，[R36]-[R65]）

**用途**：为 v1.4.0 的四个新子系统（血液 / 免疫 / 膳食营养结构 / 生活习惯）提供依据。
以下条目均经 PubMed E-utilities（esearch + esummary）取回真实元数据后写入，
标题、期刊、卷期页、DOI、PMID 均来自 API 返回，不是凭记忆抄录；
标「（该条无 DOI，仅有 PMID）」的是 API 确实没返回 DOI 的老文献。

**[R36] Convertino VA.** Blood volume response to physical activity and inactivity. *Am J Med Sci*. 2007 Jul;334(1):72-9. doi:10.1097/MAJ.0b013e318063c6e4 PMID 17630597.
取用：血容量是可变量（随活动/失活动态调整）这一前提；本模型不建模长期适应。

**[R37] Nadler SB, Hidalgo JH, Bloch T.** Prediction of blood volume in normal human adults. *Surgery*. 1962 Feb;51(2):224-32. doi:（该条无 DOI，仅有 PMID） PMID 21936146.
取用：成人血容量按体重 60~70 mL/kg 的量级（60 kg 女性约 4.0 L），即 blood.BV_REF_ML 的取值区间。

**[R38] Gutierrez G, Reines HD, Wulf-Gutierrez ME.** Clinical review: hemorrhagic shock. *Crit Care*. 2004 Oct;8(5):373-81. doi:10.1186/cc2851 PMID 15469601.
取用：失血分级口径：I 级 <15% 血容量不改变生命体征。用于把一次意外出血的上限留在 I 级。

**[R39] Franco RS.** Measurement of red cell lifespan and aging. *Transfus Med Hemother*. 2012 Oct;39(5):302-7. doi:10.1159/000342232 PMID 23801920.
取用：红细胞寿命约 120 天 → 一阶破坏项的时间常数。

**[R40] Jelkmann W.** Regulation of erythropoietin production. *J Physiol*. 2011 Mar 15;589(Pt 6):1251-8. doi:10.1113/jphysiol.2010.195057 PMID 21078592.
取用：促红素由肾脏按氧供/血红蛋白水平调控；重度贫血时红细胞产出可提高数倍（模型上限取 6 倍）。

**[R41] Camaschella C.** Iron deficiency. *Blood*. 2019 Jan 3;133(1):30-39. doi:10.1182/blood-2018-05-815944 PMID 30401704.
取用：缺铁的储备—吸收—贫血链条；铁吸收分数随储备下降而升高。

**[R42] Hallberg L, Högdahl AM, Nilsson L, Rybo G.** Menstrual blood loss--a population study. Variation at different ages and attempts to define normality. *Acta Obstet Gynecol Scand*. 1966;45(3):320-51. doi:10.3109/00016346609158455 PMID 5922481.
取用：经血量的群体分布（中位约 35 mL，>80 mL 为月经过多），用于每周期经血总量的抽样。

**[R43] Hallberg L, Högdahl AM, Nilsson L, Rybo G.** Menstrual blood loss and iron deficiency. *Acta Med Scand*. 1966 Nov;180(5):639-50. doi:10.1111/j.0954-6820.1966.tb02880.x PMID 5923385.
取用：经血铁损失 → 铁储备下降 → 缺铁性贫血的因果链。

**[R44] Hallberg L, Rossander-Hultén L.** Iron requirements in menstruating women. *Am J Clin Nutr*. 1991 Dec;54(6):1047-58. doi:10.1093/ajcn/54.6.1047 PMID 1957820.
取用：行经女性每日需吸收铁约 2 mg（对应膳食铁 18 mg 量级）。

**[R45] Machlus KR, Italiano JE Jr.** The incredible journey: From megakaryocyte development to platelet formation. *J Cell Biol*. 2013 Jun 10;201(6):785-96. doi:10.1083/jcb.201304054 PMID 23751492.
取用：血小板生成与寿命（约 7~10 天）→ 血小板的一阶回复时间常数。

**[R46] George-Gay B, Parker K.** Understanding the complete blood count with differential. *J Perianesth Nurs*. 2003 Apr;18(2):96-114. doi:10.1053/jpan.2003.50013 PMID 12710004.
取用：白细胞三分类的参考区间，以及「细菌升中性粒、病毒升淋巴」的应答方向。

**[R47] Gabay C, Kushner I.** Acute-phase proteins and other systemic responses to inflammation. *N Engl J Med*. 1999 Feb 11;340(6):448-54. doi:10.1056/NEJM199902113400607 PMID 9971870.
取用：急性期反应：IL-6 为主要驱动、CRP 为下游标志物。

**[R48] Mackowiak PA.** Concepts of fever. *Arch Intern Med*. 1998 Sep 28;158(17):1870-81. doi:10.1001/archinte.158.17.1870 PMID 9759682.
取用：发热是调定点上移（蓄热期寒战、退热期出汗）；常见感染 38~39 ℃ 的量级。

**[R49] Dantzer R, O'Connor JC, Freund GG, Johnson RW, Kelley KW.** From inflammation to sickness and depression: when the immune system subjugates the brain. *Nat Rev Neurosci*. 2008 Jan;9(1):46-56. doi:10.1038/nrn2297 PMID 18073775.
取用：生病行为三件套（乏力、食欲下降、心境低落）。

**[R50] Webster Marketon JI, Glaser R.** Stress hormones and immune function. *Cell Immunol*. 2008 Mar-Apr;252(1-2):16-26. doi:10.1016/j.cellimm.2007.09.006 PMID 18279846.
取用：糖皮质激素对免疫功能的抑制 → 「慢性应激 → 免疫张力下降」。

**[R51] Irwin MR.** Why sleep is important for health: a psychoneuroimmunology perspective. *Annu Rev Psychol*. 2015 Jan 3;66:143-72. doi:10.1146/annurev-psych-010213-115205 PMID 25061767.
取用：睡眠不足 → 炎症升高与抗病毒免疫下降 → 免疫张力项。

**[R52] Simpson RJ, Kunz H, Agha N, Graff R.** Exercise and the Regulation of Immune Functions. *Prog Mol Biol Transl Sci*. 2015;135:355-80. doi:10.1016/bs.pmbts.2015.08.001 PMID 26477922.
取用：规律中等强度运动提高免疫监视；过量相反，故模型对运动项设上限。

**[R53] Reynolds A, Mann J, Cummings J, Winter N, Mete E, Te Morenga L.** Carbohydrate quality and human health: a series of systematic reviews and meta-analyses. *Lancet*. 2019 Feb 2;393(10170):434-445. doi:10.1016/S0140-6736(18)31809-9 PMID 30638909.
取用：膳食纤维 25~29 g/天 与全因死亡率下降相关 → 纤维推荐量与评分权重。

**[R54] Krebs-Smith SM, Pannucci TE, Subar AF, Kirkpatrick SI, Lerman JL, Tooze JA, Wilson MM, Reedy J.** Update of the Healthy Eating Index: HEI-2015. *J Acad Nutr Diet*. 2018 Sep;118(9):1591-1602. doi:10.1016/j.jand.2018.05.021 PMID 30146071.
取用：膳食质量指数的构建思路（对齐推荐量给分、超标扣分）。本模型评分结构参照它，阈值取中国 DRIs。

**[R55] Odegaard AO, Jacobs DR Jr, Steffen LM, Van Horn L, Ludwig DS, Pereira MA.** Breakfast frequency and development of metabolic risk. *Diabetes Care*. 2013 Oct;36(10):3100-6. doi:10.2337/dc13-0316 PMID 23775814.
取用：进餐频次与代谢风险相关 → 「错过饭点要补吃」这一修正的行为学依据。

**[R56] Biswas A, Oh PI, Faulkner GE, Bajaj RR, Silver MA, Mitchell MS, Alter DA.** Sedentary time and its association with risk for disease incidence, mortality, and hospitalization in adults: a systematic review and meta-analysis. *Ann Intern Med*. 2015 Jan 20;162(2):123-32. doi:10.7326/M14-1651 PMID 25599350.
取用：久坐与全因死亡/住院风险相关 → 「久坐」这一负向习惯的定义。

**[R57] Goyal M, Singh S, Sibinga EM, Gould NF, Rowland-Seymour A, Sharma R, et al..** Meditation programs for psychological stress and well-being: a systematic review and meta-analysis. *JAMA Intern Med*. 2014 Mar;174(3):357-68. doi:10.1001/jamainternmed.2013.13018 PMID 24395196.
取用：冥想类干预降低焦虑/压力评分 → 把「冥想/松弛」列为正向习惯。

**[R58] Ross A, Thomas S.** The health benefits of yoga and exercise: a review of comparison studies. *J Altern Complement Med*. 2010 Jan;16(1):3-12. doi:10.1089/acm.2009.0044 PMID 20105062.
取用：瑜伽在多项指标上与常规运动相当或更优 → 「瑜伽」这一正向习惯。

**[R59] Windred DP, Burns AC, Lane JM, Saxena R, Rutter MK, Cain SW, Phillips AJK.** Sleep regularity is a stronger predictor of mortality risk than sleep duration: A prospective cohort study. *Sleep*. 2024 Jan 11;47(1):zsad253. doi:10.1093/sleep/zsad253 PMID 37738616.
取用：睡眠规律性（入睡时刻离散度）与健康结局相关 → 「规律睡眠」的标准差判据。

**[R60] Sawka MN, Cheuvront SN, Carter R 3rd.** Human water needs. *Nutr Rev*. 2005 Jun;63(6 Pt 2):S30-9. doi:10.1111/j.1753-4887.2005.tb00152.x PMID 16028570.
取用：成人每日需水量与「适量饮水」的量级（总水约 2.0 L，其中饮水约 1.5 L）。

**[R61] Gearhardt AN, Corbin WR, Brownell KD.** Food addiction: an examination of the diagnostic criteria for dependence. *J Addict Med*. 2009 Mar;3(1):1-7. doi:10.1097/ADM.0b013e318193c993 PMID 21768996.
取用：高油高盐高糖食物的「成瘾」判据 → 「垃圾食品成瘾」的定义。

**[R62] Weiss G, Ganz T, Goodnough LT.** Anemia of inflammation. *Blood*. 2019 Jan 3;133(1):40-50. doi:10.1182/blood-2018-06-856500 PMID 30401705.
取用：炎症 → hepcidin 升高 → 铁被锁在储备、肠道吸收受抑（炎症性贫血）。

**[R63] Liu F, Aulin LBS, Guo T, Krekels EHJ, et al..** Modelling inflammatory biomarker dynamics in a human lipopolysaccharide (LPS) challenge study using delay differential equations. *Br J Clin Pharmacol*. 2022 Dec;88(12):5420-5427. doi:10.1111/bcp.15476 PMID 35921300.
取用：人体内毒素试验中 IL-6 与 CRP 的时序（CRP 峰值滞后 IL-6 约一天、CRP 半衰期约 19 h）。

**[R64] Nuccio RP, Barnes KA, Carter JM, Baker LB.** Fluid Balance in Team Sport Athletes and the Effect of Hypohydration on Cognitive, Technical, and Physical Performance. *Sports Med*. 2017 Oct;47(10):1951-1982. doi:10.1007/s40279-017-0738-7 PMID 28508338.
取用：轻度失水即影响体能与认知 → 「脱水」判定的方向性依据（具体阈值是本模型取值）。

**[R65] WHO.** Haemoglobin concentrations for the diagnosis of anaemia and assessment of severity.
*WHO/NMH/NHD/MNM/11.1*. Geneva: World Health Organization; 2011.
https://www.who.int/publications/i/item/WHO-NMH-NHD-MNM-11.1
取用：贫血的血红蛋白分级阈值（成年非孕女性 <120 g/L 为贫血，<90 中度，<60 重度），
即 body/blood.py 的 anemia_grade()。
**查阅方式**：页面标题与编号于 2026-11-24 直接抓取核实（HTTP 200）。
**诚实标注**：本条目是**机构文件**，不是期刊论文；只核对了标题与编号，未逐页核对内文表述。

### 本轮的「估算」标注（如实列出）

| 位置 | 内容 | 状态 |
|---|---|---|
| blood.step_plasma | 每升总体水对应血浆 57.1 mL、RAAS/ADH 张力系数 0.06/0.05/0.04 | 由「血浆≈总体水 5.7%」反推量级 + 工程标定 |
| blood.step_rbc | 骨髓产出对 Hb 偏低的放大斜率（每 30 g/L 放大 3 倍） | 方向来自 [R40]，斜率为工程取值 |
| blood.step_iron | 基础铁丢失 0.8 mg/天、吸收基准 12%、炎症抑制 60% | 量级取自 [R41][R44][R62]，系数为工程取值 |
| immune.step_infection | 感染曲线的高斯峰值日（病毒 2.2 天 / 细菌 2.6 天）与宽度 | 形态与临床病程一致，具体数字为工程取值 |
| immune.step_cytokine | IL-6 时间常数 3 h、CRP 19 h、CRP≈0.8×IL-6 | 时序方向来自 [R47][R63]，比例系数为量级近似 |
| nutrition.score_for | 评分各项权重（20/15/15/10/15/5/10/5/5） | 结构参照 [R54]，权重是本模型的工程取值 |
| nutrition.step | 基础代谢用 Mifflin-St Jeor + 活动系数 1.35 | 公式通用，活动系数为估算 |
| habits.judge | 有氧 150 分钟/周、力量 2 次/周、规律睡眠 SD 45 分钟、放松 10/20 分钟 | 门槛量级有出处（WHO 指南、[R59]），具体数值为工程取值 |
## [R36] Harding EC, Franks NP, Wisden W.
**Sleep and thermoregulation.**
*Current Opinion in Physiology*. PMC7323637

**查阅方式**：直接抓取 PMC 正文段落（一手）。

**用于**：`body/engine.py` `_thermo` 的睡眠期体温建模（v1.3.1 方案 A）。

**取用内容**：进入 NREM 即降温；"core temperature drops on every transition to NREM sleep"
（去同步实验中仍成立）；REM 期体温调节暂停（poikilothermia），脑温反升 0.1~0.2℃；
卧床受试者 24 小时体温周期约 1℃；入睡前 2 小时核心温开始下降。

**未取用**：REM 期调节暂停这一条**没有**单独建模 —— 再引入一个分期耦合会退回锯齿伪像。

## [R37] Human Body Temperature Circadian Rhythm in Health and Disease.
*IntechOpen*. DOI: 10.5772/intechopen.1003852

**查阅方式**：抓取章节正文（一手）。

**用于**：正常核心体温范围与昼夜节律的存在性。

**取用内容**：Wunderlich 1868 百万级腋温读数 → 均值 37℃、正常上限 38℃；
核心部位测值高于口腔/腋下/鼓膜。

## [R38] Dhami P, et al.
**Approach to Low Body Temperature or Mild Hypothermia in the Geriatric Population: A Narrative Review.**
PMC13094164

**查阅方式**：抓取摘要与正文段落（一手）。

**用于**：`s.shiver` 的阈值。

**取用内容**：正常核心 36.1~37.2℃；失温 <35.0℃；35.0~36.0 称 "low body temperature"；
寒战是 <35℃ 的伴随症状。→ 阈值 36.5 → 35.8 → **35.5**。

## [R39] 低热与发热的临床划分（**二手**）
低热 37.5~38.3℃；CDC 定义发热 ≥38.0℃。

**查阅方式**：检索摘要与厂商科普页，**未取得原始指南全文**。

**用于**：`FEVER_C`（体温影响心率/CO₂/失水的起点），37.3 → **37.5**。

## [R40] 夜间血压下降幅度（**二手**）
正常 dipper 夜间血压下降 10~20%；非 dipper（<10%）心血管风险升高。

**查阅方式**：检索摘要（Hypertension Research 综述一系），**未读原文**。

**用于**：核对夜间血压建模 —— 实测下降 13%，在区间内，**未改动代码**。

## [R41] NREM / REM 心率区间（**二手**）
NREM 心率 50~75（深睡可到 40 余），REM 可回升接近清醒水平。

**查阅方式**：检索摘要（临床科普综述），**未读原始研究**。

**用于**：核对深睡心率 60、REM 62.8 —— 在区间内，**未改动代码**。

## [R42] Positivity offset（**二手**）
人对中性刺激倾向给出轻微正性评价，对生活总体给出"大部分时候是好的"。

**查阅方式**：检索摘要与维基条目，原始文献（Cacioppo 等）**未读**。

**用于**：支持欣快给健康身体一个 15~25 的轻微正性基线。

## [R43] Letting Go of the Negative, Holding on to the Positive（**二手**）
差异情绪习惯化研究：正性情绪消退更快、负性情绪更持久。

**查阅方式**：加州大学伯克利分校 Letters & Science 新闻稿（2024），**未见论文原文**。

**用于**：烦躁与欣快拆成不同时间常数（`DYSPH_TAU = 900` vs `EUPH_TAU = 180`）。

## [R44] 咖啡因剂量与时机对睡眠的影响（随机交叉试验）
Dose and timing effects of caffeine on subsequent sleep: a randomized clinical crossover trial. PMC11985402

**查阅方式**：抓取 PMC 正文摘要段（一手）。23 名男性、7 个条件、安慰剂对照、双盲交叉。

**取用内容**：100 mg 即使睡前 4 h 也无显著影响；400 mg 在睡前 12 h 内显著推迟入睡，
8 h 内睡眠碎片化显著增加，4 h 前主观睡眠质量 −34.02%。

**用于**：`_night_window` 的咖啡因推迟入睡（阈值 150 mg、400 mg → +20 分钟）。

## [R45] 酒精对睡眠结构的影响（PSG）
The Acute Effects of Alcohol on Sleep Architecture in Late Adolescence. *Alcohol Clin Exp Res*. 2013;37(10):1720-1728. PMC3987855

**查阅方式**：抓取 PMC 页面（摘要与元数据为一手；**具体 REM 抑制百分比未取到**）。

**取用内容**：前半夜 REM 被抑制、后半夜反跳，伴睡眠碎片化。
**模型中 25%/10% 是量级估计**，不是原文数值。

## [R46] 急性运动与慢波睡眠
- Kredlow MA, et al. The effects of physical activity on sleep: a meta-analytic review.（**二手**，未见全文）
- 另有研究报道急性运动后 SWS +33%；而"睡前 4~8 h 最佳、过近反而缩短 SWS"来自
  *Different Intensities of Evening Exercise on Sleep* 的检索摘要（**二手**）。

**用于**：`_advance_stage` 的运动加深慢波（+20% / +10%）与入睡提前（−8 / −4 分钟）。
