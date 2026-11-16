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
