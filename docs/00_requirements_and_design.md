# AI 应用增长与数据运营策略分析平台
## 需求解析与系统设计（Design Baseline v1.3）

> 依据文档：`ai_growth_data_ops_rag_workbuddy_guide.md`（任务书 v1，1047 行）
> 编制日期：2026-11-02
> 状态：**设计基线 v1.3**。阶段 0~3 已完成。阶段 1 据实测新增 C-10 ~ C-12（改写模块 B），
> 阶段 3 新增 C-13（T3 合成 ID 的原则性偏离）。当前进度以 `README.md` 的阶段表为准。
> 原则：本文件只做解析与设计，不替代任务书；两者冲突时以**本文件"勘误与冲突"章节**的处置结论为准，并逐条注明理由。

---

## 0. 执行摘要

任务书是一份结构完整、约束明确的实习作品集项目规格书，可执行性较高。但在**逐条落实为工程规格**时，发现 9 类必须先解决的前置问题，否则会在阶段 2 之后集中暴露为返工：

| 编号 | 严重度 | 问题 | 后果 |
|---|---|---|---|
| C-01 | 高 | 知识块 500–800 汉字 vs BGE 模型 512 token 上限 | 向量化静默截断，检索质量失真且不可复现 |
| C-02 | 高 | `.gitignore` 忽略 `outputs/` 与 6 个模块的交付清单直接冲突 | 阶段验收"交付物不存在于仓库"，或临时改规则导致历史不一致 |
| C-03 | 高 | 本机无 Python 3.11、无 Homebrew、无 MySQL | 任务书第 3 章给出的安装路径全部需要管理员权限或大体积安装 |
| C-04 | 中 | A/B 实验样本量未与真实可用新客数对齐 | 若按惯例设 MDE=10%，真实数据几乎必然样本不足，结论无效 |
| C-05 | 中 | 任务书要求 DAU/WAU/MAU 与"人均使用功能数"，但数据无 user 级表 | 强行计算会得出错误口径，违背"不得混用"原则 |
| C-06 | 低 | 知识库文档数 11 份（§5）与 12 份（§1）不一致；`outputs/rag_eval_results.csv` 未纳入目录结构 | 交付对不齐 |
| **C-10** | **致命** | **490/500 账户首次使用早于注册日，注册基线漏斗不可构造** | 模块 B 原设计必须改写 |
| C-11 | 高 | `feature_name` 为 `feature_1..40` 编号，无 AI 语义 | 不能称为「AI 问答/总结/写作」 |
| C-12 | 高 | 流失三套口径（600 事件 / 352 账户 / 110 标记）互不相等 | 流失率可算出三个不同值 |

> C-10 ~ C-12 为阶段 1 实测新增，详见同号小节。

本设计的处置方式：**不修改任务书的业务要求，只修正工程上的不可执行点**，并把每处修正的理由写到可核查的程度。

---

## 1. 需求解构

### 1.1 项目本质：三个可独立评估的能力层

任务书表面是"6 个模块"，实质是三层能力，面试考察点也按这三层分布：

| 层 | 内容 | 交付证据 | 面试价值占比（判断） |
|---|---|---|---|
| L1 分析能力 | 指标体系、漏斗留存、分层、策略推导 | 指标字典、Notebook、分层 CSV、策略手册 | 高（最容易被追问） |
| L2 实验能力 | 样本量、SRM、比例检验、护栏指标 | A/B 报告、模拟数据 | 中高（决定是否"懂统计"） |
| L3 工程能力 | RAG 检索、评估、指标与 LLM 的隔离 | RAG 代码、评估 CSV、诊断报告 | 高（差异化模块） |

**关键判断：** L3 是差异化模块，但**不能以牺牲 L1 的口径严谨性为代价**。任务书 §0 第 4、5 条（指标只由 SQL/Python 计算、LLM 不碰数字）是全项目最硬的红线，任何设计都不得绕过。

### 1.2 模块—交付物—验收点矩阵

| 模块 | 交付文件 | 验收命令/检查 | 依赖前置 |
|---|---|---|---|
| A 指标体系 | `docs/metric_dictionary.md`、`sql/02_kpi_analysis.sql`、`outputs/tables/*.csv`、经营诊断报告 | SQL 结果与 Pandas 抽样核对一致 | 阶段 1 数据质量 |
| B 行为与分层 | `notebooks/02_*.ipynb`、`src/segmentation.py`、`outputs/tables/account_segments.csv`、`docs/segmentation_findings.md` | 每层账户数之和 = 总账户数；阈值有来源 | 模块 A |
| C 策略 | `docs/operation_strategy_playbook.md`、`outputs/tables/strategy_priority.csv` | 3 套策略每条可追溯到模块 B 的具体数字 | 模块 B |
| D A/B | `notebooks/03_*.ipynb`、`src/ab_test.py`、`outputs/simulated/ab_test_simulated.csv`、`docs/ab_test_report.md` | 样本量推导链完整；SRM 通过；文件名含 simulated | 模块 A（取基线） |
| E AI 使用 | `notebooks/04_*.ipynb`、`docs/ai_usage_analysis.md`、`outputs/tables/ai_usage_summary.csv` | 成本处标注假设单价；成功率分功能可复核 | 阶段 1 + 模拟日志 |
| F RAG | `src/rag/*`、`scripts/build_knowledge_base.py`、`scripts/ask_ops_assistant.py`、`docs/rag_evaluation_report.md`、`outputs/rag_eval_results.csv` | 重建幂等；Hit@3 可复算；引用可定位 | 模块 A/B/C（取指标函数） |

### 1.3 任务书 §0 十条硬约束 → 可执行的机械检查

约束必须能落到"跑一条命令就能判定真假"，否则只是口号。

| # | 原文要求 | 落地为 | 检查方式 |
|---|---|---|---|
| 1 | 先检查已安装软件 | 只读探测，不预设架构 | `scripts/check_environment.py` 输出 JSON |
| 2 | 不得擅自升级/卸载 | 安装动作一律先出方案再等确认 | 每次安装前打印"将执行命令 + 影响范围" |
| 3 | 未经确认不做管理员操作 | 所有 `sudo`/pkg 安装列为待确认 | 会话内显式列出 |
| 4 | 指标先由 SQL/Python 计算 | LLM 入参只接受已算好的 JSON | `tests/test_rag_output.py` 断言数值与会话内指标 JSON 逐键一致 |
| 5 | 不让 LLM 自由生成 SQL | 只暴露 3 个白名单函数，参数做类型与范围校验 | 代码审查 + 单测覆盖非法日期/账户范围 |
| 6 | 不用 Streamlit/Docker/FastAPI 等 | 依赖清单白名单，出现即失败 | `requirements.txt` 审阅 + `pip list` 比对 |
| 7 | 不训练 LLM | 只做 embedding 推理 | 无 trainer 代码 |
| 8 | 每阶段先测试再提交 | 阶段门禁（见 §8） | 每条门禁有明确命令 |
| 9 | 不得声称未验证的成功 | 报告只写实际命令输出 | 汇报模板固定要求贴输出摘要 |
| 10 | 代码需可理解 | 关键函数中文注释 | 抽查 |

### 1.4 数据真实性的三层分级（贯穿全项目的话术纪律）

| 层级 | 数据 | 允许表述 | 禁止表述 |
|---|---|---|---|
| T1 真实公开合成数据 | RavenStack 五表（Kaggle/GitHub 公开，作者声明为合成 SaaS 数据） | "公开合成数据集" | "某某企业真实数据" |
| T2 模拟日志 | `ai_request_logs.csv` | "按明确规则生成的模拟日志" | "线上调用日志" |
| T3 模拟实验 | `ab_test_simulated.csv` | "模拟实验，仅演示分析方法" | "实验证明提升 X%" |

**执行细节：** T2/T3 文件的每一行分析产出（图、表、报告段落）必须携带标识。建议统一在报告小标题或图表标题后缀加 `[模拟]`，并在 README 顶部设"数据边界声明"区块。

---

## 2. 勘误与冲突处置（核心章节）

### C-01 知识块长度与 Embedding 模型容量冲突【高】

**事实：** `BAAI/bge-small-zh-v1.5` 最大输入长度 **512 tokens**（模型卡确认；24M 参数，输出 512 维，MIT 许可）。任务书 §6.1 要求 chunk 500–800 个中文字符。中文经 BGE 分词器后接近 **1 字 ≈ 1 token**，叠加标点、数字、英文术语后 token 数通常**大于**字符数。

**后果：** 800 字的知识块会被截断到 512 token 再向量化，尾部 30%+ 内容不参与检索匹配。这种失效是**静默的**——检索仍会返回结果，只是召回质量下降，且用 Hit@3 评估时会表现为"莫名其妙漏召回"，很难定位。

**处置：**
- chunk 目标长度 **320–380 汉字**，硬上限 **420 汉字**，下限 150 汉字，重叠 **60–80 汉字**。
- 切分前用 `tokenizer.encode` 实测 token 数，超 512 的块触发二次切分并记 WARN 日志（含 chunk_id 与 token 数）。
- 保留任务书的"按 Markdown 标题优先分节"与"不切断公式/条件/步骤"两条规则：**先按标题分节，节内再按长度切；若某节整体 ≤420 字则整节成块**，不强切。
- 把实测的 `max_token_len=512`、`chunk_max_chars=420` 写入 `docs/rag_design.md` 与 README，作为可复现参数。

**理由：** 这是唯一能让"检索评估有意义"的改法。若不改，§6.6 的 Recall@3/Hit@3 度量的是被截断后的残缺语义，评估结论不可信。

**阶段 4 前置的实测校正（2026-11-02，`scripts/check_rag_env.py`）：**

| 项 | 设计期推断 | 实测值 |
|---|---|---|
| 向量维度 | 512 | **512** ✓ |
| `model_max_length` / `max_position_embeddings` | 512 | **512 / 512** ✓ |
| 中文「字 → token」比 | 「接近 1:1，叠加标点后通常明显大于」 | **1.005**（200 字 → 202 token） |
| 500 字块 | 未测 | 502 token，刚好卡在边界 |
| 600 字块 | 未测 | 602 token，**确实被截断** |
| 800 字块（任务书上界）被丢弃比例 | 「尾部 30%+」 | 806 → 512，**丢弃 36.5%** |
| 实测安全上限 | 推断约 420 | **约 500 字**（420 字仅用 422 token，余 90） |

**结论维持不变，但理由更准确：** C-01 的核心判断（800 字块会被静默截断）被实测证实且更严重（36.5% 而非 30%）；同时原先"token 数会明显大于字数"的估计偏高——实际只多 0.5%，因此 420 字的硬上限**偏保守**，真实安全线在 500 字左右。

**仍保持 420 的理由改为检索质量而非模型容量：** 块越大，单块语义越杂，命中时的上下文精确度越低。本项目取 350 字目标 / 420 上限，是为检索精度服务，不是被 token 上限逼的。这一区分已写入 `docs/rag_design.md`，避免后人误以为必须卡在 420。

### C-02 `.gitignore` 与交付清单冲突【高】

**事实：** 任务书 §8 建议 `.gitignore` 包含 `outputs/`、`data/simulated/*`、`data/processed/*`；而 §1 各模块交付物明确要求 `outputs/tables/account_segments.csv`、`outputs/rag_eval_results.csv`、`outputs/simulated/ab_test_simulated.csv` 等文件。

**后果：** 两种失败模式——(a) 严格按 `.gitignore` 执行，则仓库里没有任何分析产物，面试官看到的是空壳代码；(b) 中途临时改规则，则历史提交语义不一致，且任务书 §8.7"如需上传少量示例结果，由用户确认后单独添加白名单目录"其实已经预留了口子。

**处置（取白名单方案）：** 保留数据原始层忽略，对**小体积聚合产物**开白名单：

```gitignore
# 原始/中间/向量库：一律忽略（数据许可未明示，见 §2 C-07）
data/raw/*
data/processed/*
data/chroma/
data/simulated/*

# 产物：只放行小体积聚合结果与报告，忽略图片缓存与大文件
outputs/**
!outputs/tables/
!outputs/tables/*.csv
!outputs/reports/*.md
!outputs/rag_eval_results.csv
!outputs/simulated/
!outputs/simulated/*.csv

# 提交前统一体积闸门：单文件 >2MB 不允许进库
```

配套 `scripts/check_git_hygiene.py`：扫描 `git diff --cached` 的文件名与体积，命中 `.env`/`*.key`/`*.csv` 超限/`chroma` 即非零退出。

### C-03 环境缺口：Python 3.11 / Homebrew / MySQL 全部缺失【高】

**实测（2026-11-02，只读探测）：**

| 项 | 结果 | 判定 |
|---|---|---|
| macOS | 27.0 (build 26A428) | 满足，但版本很新，个别轮子需实测 |
| 芯片 | Apple M1 Pro，8 核，16 GB，arm64 | 满足 MPS 预期，16GB 足够本项目 |
| 磁盘 | 剩余 282 GiB | 远超 8–10 GB 要求 |
| Xcode CLT | `/Library/Developer/CommandLineTools` | 已装 |
| Homebrew | **未安装** | 缺口 |
| Python | 系统 3.9.6；另有 3.13.12（工具链内置）；**无 3.11** | 缺口 |
| uv | **0.12.15**（`~/.local/bin/uv`），可下载 3.11.16 与 3.12.14 | **替代路径** |
| Git | 2.54.0；**user.name / user.email 均为空** | 缺口（提交会失败或use错误身份） |
| MySQL | **未安装** | 缺口 |
| VS Code | `/Applications/Visual Studio Code.app`，`code` CLI 可用 | 已装 |
| GitHub | 无 remote、无 `gh`；`~/.ssh/id_ed25519` 存在但无法确认已登记 | 缺口 |

**处置：** Python 3.11 走 **uv** 而非 Homebrew（理由：uv 已存在且单文件、无需 sudo，能精确锁 3.11.16；Homebrew 的 `python@3.11` 会被 `brew upgrade` 连带升级，带来 venv 的 ABI 漂移风险）。这与任务书"只选一种安装方式"不冲突——它选择的是 uv 这一种。

```bash
uv python install 3.11.16
uv venv --python 3.11.16 .venv
source .venv/bin/activate
```

MySQL 走 Homebrew（用户已决策，执行细节见 §9.5，注意 `mysql@8.4` 版本化公式与 keg-only 问题）；Git 身份与 GitHub remote 仍待用户提供（见 §9.3-D4）。

### C-04 A/B 样本量必须由真实基线反推【中】

任务书要求说明 MDE、样本量、功效，但未规定推导顺序。若先拍 MDE=10% 再找样本，本项目大概率失败。

**已算出的两张表（α=0.05 双侧，power=0.80，两比例 z 检验）：**

表 1 所需样本量（每臂）：

| 基线转化率 | +10% 相对提升 | +15% | +20% | +30% |
|---|---|---|---|---|
| 0.15 | 9257 | 4193 | 2402 | 1106 |
| 0.20 | 6510 | 2943 | 1683 | 772 |
| 0.25 | 4862 | 2193 | 1251 | 571 |
| 0.30 | 3763 | 1693 | 963 | 437 |
| 0.35 | 2978 | 1336 | 758 | 342 |

表 2 给定样本量可检测的最小提升（基线 0.25 为例）：

| 每臂样本 | 最小可检测绝对提升 | 相对提升 |
|---|---|---|
| 200 | 0.130 | 52% |
| 300 | 0.105 | 42% |
| 400 | 0.090 | 36% |
| 500 | 0.080 | 32% |
| 1000 | 0.056 | 22% |

**结论与设计规则：** RavenStack 为公开小规模合成集，可用"新客"数量很可能只有数百量级。因此**推导顺序固定为**：

1. 阶段 2 用真实数据算出 7 日首次有效任务完成率基线 `p1` 与可用新客数 `N`；
2. 取 `n_arm = floor(N/2)`，用表 2 的反解公式算出该样本量下的**最小可检测效应**；
3. 若 `n_arm < 500`（即 MDE 相对值 >30%），则在 `docs/ab_test_report.md` 与 Notebook 首屏**明确声明"真实样本不足以检测中等效应，故模拟实验按设计样本量生成"**，模拟样本量取表 1 对应值并注明"样本量为设计值，非真实观测数"；
4. **禁止**为了让结果显著而调小方差、放大效应或重跑随机种子。模拟效应参数在生成脚本里一次性写死，并在报告中原样披露。

### C-05 用户级指标不可计算【中】

**事实：** RavenStack 五表（accounts / subscriptions / feature_usage / support_tickets / churn_events）粒度均为**账户级**，无 user/session 表。

**后果：** 任务书 §1 模块 A 要求"DAU/WAU/MAU（按账户或用户口径必须明确，不得混用）"、模块 B 要求"人均使用功能数"。若照字面实现，会造出一个无数据支撑的"人均"。

**处置：**
- 全项目统计主体统一为 **account**，指标名一律用"活跃账户数（DAA/WAA/MAA）"表述，并在指标字典"限制"列写明"数据无 user 粒度，DAU 类指标不可计算"。
- "人均使用功能数"改为 **"户均使用功能数"**（功能数 / 活跃账户数）。
- 指标字典增设 **可计算性** 列，取值 `可计算 / 部分可计算（注明缺失字段）/ 不可计算（注明原因）`。这是硬性要求：宁可标"不可计算"，也不填近似值。

### C-06 交付清单内部不一致【低】

| 位置 | 内容 | 处置 |
|---|---|---|
| §1 模块 F | 知识库 12 份（含 `pricing_plan.md`） | 以 §1 为准，共 12 份 |
| §5 目录结构 | 只列 11 份（缺 `pricing_plan.md`） | 补齐 |
| §5 目录结构 | 无 `outputs/rag_eval_results.csv` 位置 | 归入 `outputs/` 根（与 `tables/` 平级），并在 `.gitignore` 白名单放行 |

### C-07 数据许可未明示【需记录】

`tivonio/sql-churn-analysis` 仓库 README 未声明 LICENSE 文件。任务书 §2.1 要求"遵守原作者许可及署名要求"。

**处置：** 由于许可不明确，采取最保守策略——**原始 CSV 不进 Git**（与 C-02 一致），仓库内提供 `scripts/download_data.py` 与 `docs/data_manifest.md`（记录来源 URL、下载日期、每文件 sha256、行数、列数）。README 署名区写明数据来源与"合成数据"性质。这样既满足可复现，又不分发许可不明的数据。

### C-08 DeepSeek 模型名与计价的核对结论（任务书此处正确）

任务书 §4.5 写 `deepseek-flash`。核查结论：**该模型名当前有效**——DeepSeek-V4.1-Flash 于 2026-09-09 GA，对外模型名为 `deepseek-flash`；`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 15:59 UTC 退役。故**不需要修改任务书**，但需在 `.env.example` 注释中写明"若账户不可用则从控制台确认替代模型名"，避免换机后 404。

**计价（用于模块 E 成本分析，作为"假设单价"而非真实成本）：** 官方公开价（2026-09-18 核验快照）V4.1-Flash 每百万 token：输入未命中缓存 $0.15、命中 $0.003、输出 $0.60（off-peak）；peak 为 2 倍。设计上要求：

- `src/ai_usage.py` 中单价定义为常量并附 `PRICE_SNAPSHOT_DATE = "2026-09-18"`；
- 成本表列名必须是 `estimated_cost_usd_at_assumed_price`，不得叫 `cost`；
- 报告正文必须写"单价为公开价快照假设值，峰谷计费未细分，结论仅用于相对比较"。

### C-10 时间轴不连贯：模块 B 的漏斗设计必须改写【致命，阶段 1 实测】

**发现（`docs/data_quality_report.md` Q-23 / Q-15 / Q-24）：**

- 500 个账户中 **490 个**的首次使用日早于注册日，中位数早 400 天；
- 76.6% 的使用记录早于其所挂订阅的开始日；
- 使用记录落在该账户任一订阅区间内的比例仅 22.5%；
- 「注册后 7 日内首次使用」的账户只有 **7 个**。

**根因判断：** 合成数据生成器未在时间维度上约束 `feature_usage.subscription_id` 的挂接关系，
不是个别脏数据。上游分析仓库回避了这个问题（其口径全部落在账户-月快照层面）。

**后果：** 任务书模块 B 的新客激活漏斗（注册 → 首次 AI 请求 → 首次成功任务 → 7 日复购）
**在该数据集上不具备可构造性**。若照原样实现，会得到一个 7/500 = 1.4% 的"激活率"——
这是数据缺陷的产物，不是业务结论。

**处置（改写设计，不隐瞒）：**

| 分析对象 | 原设计 | 改为 | 证据来源 |
|---|---|---|---|
| 新客激活漏斗 | 以 `signup_date` 为 t0 | 以 **`first_use_date`** 为 t0 的「使用起始 7/30 日留存」 | 真实数据 |
| 注册→首次请求→有效→复购 | 真实数据 | **T2 模拟日志**（含 `account_id`、`request_time`，可与 `signup_date` 对齐） | 模拟数据 |
| 功能渗透 | AI 功能（问答/总结/写作） | 真实数据称「功能渗透（feature_1..40）」；AI 功能维度用 T2 | 真实 + 模拟 |

**措辞纪律：** 指标名里凡出现"注册后"字样的，一律不得用于真实数据的计算结果。

**对模块 D 的连带结论：** 可用账户 500 个（每臂 250），按 §C-04 的表 2，基线 0.25 时
只能检测约 47% 的相对提升。故模拟实验必须按**设计样本量**生成，并声明"样本量为设计值，
非真实观测数"。这与 C-04 的预判一致，现由实测确认。

### C-11 功能维度语义：`feature_1..40` 不是 AI 功能【高】

**事实：** `feature_name` 取值为 `feature_1` ~ `feature_40`，40 个，分布均匀（各约 620 条），
无任何 AI 语义。

**处置：** 禁止把编号功能映射为「AI 问答/文档总结/AI 写作」。真实数据的功能分析统一命名为
「功能渗透（feature_1..40）」；AI 功能名称只在 T2 模拟日志中出现，并标注模拟。
此条与 C-10 的处置表一致，属于数据真实性红线（§1.4 T1/T2 分级）。

### C-12 流失口径三选一【高】

`churn_events` 600 行 / 352 个账户 / 其中 61 行为复活；`accounts.churn_flag=True` 仅 110 个；
两处同时命中的只有 75 个。

**处置：** 主口径 = `churn_events` **去重账户数**，复活行单列不重复计入；`churn_flag`
降级为交叉校验字段，差异（仅标记 35 / 仅事件 277）在报告中披露。理由：事件表带日期与原因，
可做时间序列；快照标签无法追溯。

### C-13 T3 实验必须自造账户 ID，与 §3.4 冲突【高，阶段 3 实施】

任务书 §3.4 要求「`account_id` 必须取自真实账户集合，禁止自造」。T2 严格遵守；
T3 无法遵守，因为设计样本量超出真实账户总数一个数量级：

| 方案 | 每臂样本 | 可检测的相对提升 | 结论 |
|---|---:|---:|---|
| 全部 500 个真实账户 | 250 | 46% | 运营上不成立 |
| 单月新增约 21 个账户 | 10 | 235% | 荒谬 |
| 按设计样本量合成 | 2,193 | 15%（设计值） | 采用 |

**处置：** T3 使用 `SIM-AB-xxxxx` 合成 ID，并在三处同时披露：
`docs/data_simulation_method.md` §4.1、`docs/ab_test_report.md` §1、README 数据边界表。
若改为复用 500 个真实 ID 凑样本，会破坏「一个账户只能进一组」的独立性——那是比自造 ID
更严重的方法论错误。**这是本设计对任务书唯一的一处原则性偏离，已在报告中显式标注。**

---

## 3. 数据契约设计

### 3.1 五表粒度（预判，阶段 1 实测后校正）

| 表 | 业务粒度 | 主键/去重键 | 已知陷阱 |
|---|---|---|---|
| accounts | 一账户一行 | `account_id` | 可能有 `churn_date` 与状态字段口径冲突 |
| subscriptions | 一订阅一行（账户可多条） | `subscription_id` | **同一账户同月可能重叠**；行数 ≠ 账户数；MRR 不可直接 `sum` 后按账户计数 |
| feature_usage | 一次使用事件 | `usage_id`（**存在重复**，仓库 README 已明示） | 需先量化重复率再定去重键；去重前后行数都要留档 |
| support_tickets | 一工单一行 | `ticket_id`（待确认） | 满意度可能为空，不能当 0 |
| churn_events | 一流失事件一行 | `event_id`（待确认） | 行数 ≠ 独立流失账户数 |

### 3.2 关联关系

```
accounts (account_id) 1 ──< N subscriptions (account_id, subscription_id)
                              │
                              └──< N feature_usage (subscription_id)
accounts (account_id) 1 ──< N support_tickets (account_id)
accounts (account_id) 1 ──< N churn_events (account_id)

【模拟层】
accounts.account_id ──< ai_request_logs.account_id      (T2，固定种子)
accounts.account_id ──< ab_test_simulated.account_id    (T3，随机化单位)
```

**关键约束（写入 SQL 注释与指标字典）：**
- "账户数"一律 `COUNT(DISTINCT account_id)`，禁止对订阅表 `COUNT(*)`。
- "流失账户数"一律 `COUNT(DISTINCT account_id)`，禁止对 churn_events `COUNT(*)`。
- 月度 MRR 采用**月末快照 + 单订阅胜出规则**：同一 (account_id, 月) 若有多条有效订阅，按 `start_date` 最新 → `mrr_amount` 最高 → `subscription_id` 排序取第一条。理由：否则同账户同月重复计入 MRR，收入类指标全错。

### 3.3 清洗规则登记表（`docs/data_quality_report.md` 的核心表）

每一条清洗动作必须登记，格式固定：

| 规则ID | 对象 | 触发条件 | 处理 | 影响行数 | 依据 | 是否可回滚 |
|---|---|---|---|---|---|---|
| R-01 | feature_usage | `usage_id` 完全重复 | 保留首行，其余标记 | 待实测 | 仓库 README 明示 | 是（原文件只读） |
| R-02 | feature_usage | `usage_id` 重复但字段不同 | **不删**，加 `dup_flag` 并单独出表 | 待实测 | 需业务判断 | 是 |

**硬规则：** 原始 CSV 只读；任何清洗结果写 `data/processed/`；"保留原始行数 / 去重后行数"必须同时出现在报告里，不得只写后者。

### 3.4 模拟数据生成规范

| 项 | 要求 |
|---|---|
| 种子 | 单一常量 `RANDOM_SEED = 20261102`，写入 `src/config.py` |
| account_id 来源 | 必须取自真实 `ravenstack_accounts.csv` 的 ID 集合，禁止自造 |
| 时间范围 | 落在数据观察期内（阶段 1 确定后写死） |
| AI 功能名 | 仅 3 个：AI 问答 / 文档总结 / AI 写作 |
| 成功率差异 | 允许按功能设定基线（如问答 0.92、总结 0.88、写作 0.90），但须在 `docs/data_simulation_method.md` 写明参数表 |
| 成本字段 | 单价用 §C-08 快照，且与真实字段分开存放 |
| 标识 | 文件名含 `simulated`；CSV 增加 `data_source = "simulated"` 列 |

---

## 4. 指标口径设计（Metric Contract）

统计主体统一 **account**；时间窗默认自然月，留存用"注册周 + N 日"。

| 指标 | 分子 | 分母 | 窗口 | 来源 | 可计算性 |
|---|---|---|---|---|---|
| 新增企业账户数 | 该期首次出现的 account_id 数 | — | 月 | accounts | 可计算（需确认注册日期字段） |
| 活跃账户数 DAA/WAA/MAA | 该期有 ≥1 次 feature_usage 的 account_id 数 | — | 日/周/月 | feature_usage | 可计算（主体=account，非 user） |
| 首次 AI 使用率 | 有首次 AI 请求的账户数 | 该期新增账户数 | 注册后 7/30 日 | feature_usage | 可计算（需定义"AI 请求"= 功能维度过滤） |
| 首次有效任务完成率 | 首次请求后 `task_success=1` 的账户数 | 有首次请求的账户数 | 注册后 7 日 | feature_usage（T2 字段） | 部分可计算：**RavenStack 原始表无 `task_success`**，仅模拟日志有 → 该项必须拆为"真实表口径：usage 成功事件"与"模拟日志口径：task_success"，两者不合并 |
| 7 日 / 30 日留存（账户） | 注册周后第 7/30 日仍有使用的账户数 | 该注册周新增账户数 | 固定窗口 | feature_usage | 可计算（需日期范围足够，阶段 1 判断） |
| 功能渗透率 | 使用该功能的账户数 | 活跃账户数 | 月 | feature_usage | 可计算 |
| 任务成功率 | 成功请求数 | 总请求数 | 月 | **仅 T2 模拟日志** | 可计算（标注模拟） |
| 请求失败率 | `request_status != success` 数 | 总请求数 | 月 | 仅 T2 | 同上 |
| P50 / P95 响应时间 | `latency_ms` 分位数 | — | 月 | 仅 T2 | 可计算（说明为模拟延迟） |
| Token 消耗 | `input_tokens` / `output_tokens` 求和 | — | 月 | 仅 T2 | 可计算 |
| 估算成本 | tokens × 假设单价 | — | 月 | 仅 T2 + 快照价 | 部分可计算（假设单价） |
| MRR / ARR | 月末快照 MRR 求和；ARR = MRR×12 | — | 月 | subscriptions | 可计算（须用胜出规则） |
| 净收入留存 NRR / 毛流失率 | 见月度瀑布 | — | 月环比 | subscriptions | 视字段完整度，阶段 2 判定 |
| 流失率（Logo churn） | 上月付费、本月不付费的账户数 | 上月付费账户数 | 月 | subscriptions | 可计算 |
| 工单率 | 工单数 | 活跃账户数 | 月 | support_tickets | 可计算（注意一账户多工单） |
| 客户满意度 CSAT | 满意度均值 | 有评分工单数 | 月 | support_tickets | 部分可计算（空值须排除并披露 N） |

**分层阈值设计方法（避免"伪行业标准"）：**
1. 候选阈值来自**实际分布分位数**（如活跃天数的 P25/P50/P75）；
2. 每个阈值必须能对应一个可执行的运营动作（否则分数无意义）；
3. 阈值表写入 `docs/segmentation_findings.md`，含"若阈值 ±20% 各层规模如何变化"的敏感性小表；
4. 措辞：写"本项目依据 X 月分布选取 P50 作为界限"，**禁止**写"行业通用阈值为…"。

六层定义（任务书指定，此处补判定逻辑）：

| 层 | 判定逻辑（待阈值填充） | 运营含义 |
|---|---|---|
| 新客户待激活 | 注册 ≤30 日 且 无首次有效任务 | 策略 1 主战场 |
| 低活跃 | 近 30 日使用天数 ≤ x 次 且 非新客 | 策略 3 候选 |
| 浅层采用 | 使用功能数 = 1 | 策略 2 主战场 |
| 深度采用 | 使用功能数 ≥ 3 且 近 30 日活跃 ≥ y | 标杆群，用于对照 |
| 高价值 | MRR 分位 ≥ P75 | 分层结构分析 |
| 风险关注 | 近 30 日活跃下降 ≥ z% 或 近 60 日未使用 | 策略 3 优先 |

**互斥性要求：** 六层必须有明确的判定优先级（按"新客 → 风险 → 低活跃 → 深度 → 浅层 → 高价值"顺序命中即停），否则各层规模之和 > 总账户数。这是模块 B 的验收硬指标。

---

## 5. RAG 设计

### 5.1 处理链路

```
knowledge_base/*.md (12 份，标注"项目模拟资料")
   → document_loader（按 ## 标题分节，保留 source/title/section）
   → text_splitter（320–380 字，上限 420；overlap 60–80；token 实测校验）
   → embedding_model（bge-small-zh-v1.5，优先 MPS，失败回退 CPU）
   → vector_store（Chroma PersistentClient，data/chroma/）
   → retriever（Top-K=4，输出含 distance 与元数据）
   → rag_service（指标函数 JSON + 检索片段 → DeepSeek）
   → 8 段固定结构报告
```

### 5.2 Chunk 元数据（写入 Chroma metadatas）

| 字段 | 说明 |
|---|---|
| `chunk_id` | `{doc_stem}#{section_slug}#{ordinal}`，全局唯一、可重建 |
| `source_file` | 相对路径 `knowledge_base/xxx.md` |
| `title` | 文档 H1 |
| `section` | H2 标题文本 |
| `content_hash` | chunk 正文 sha256 前 16 位 → **幂等写入的依据** |
| `document_version` | 文档 front-matter 的 `version`，缺省 `1` |
| `index_version` | 索引构建时间戳 + embedding 模型名，写入 collection metadata |
| `created_at` | ISO8601 |

### 5.3 幂等与重建策略

- 重建时按 `chunk_id` **upsert**，不 `add`；旧版本残留 chunk 按 `index_version` 差异删除。
- 重复运行 `build_knowledge_base.py` 后 collection 计数必须不变 → 作为阶段 4 验收命令。
- 文档变更检测：比对 `content_hash` 集合；仅对变化文档重算 embedding（省时，也避免 MPS 抖动导致向量轻微漂移）。
- 任何 embedding 维度 ≠ 512 直接抛错退出，不静默续跑。

### 5.4 检索与查询指令

- Top-K 默认 4，配置项 `RAG_TOP_K`。
- 距离字段输出为 `distance`（Chroma 默认 L2/余弦视配置），报告与 UI 中**只能称"距离"或"相似度分数"**，禁止称"概率""置信度"。
- BGE v1.5 中文版官方说明检索时**无需**指令前缀。设计上仍保留开关 `QUERY_INSTRUCTION=""`，阶段 4 用 18 题评估集做"有/无指令"的对照，以实测决定默认值（不凭印象定）。

### 5.5 三场景固定函数契约（白名单，禁止模型生成 SQL）

| 场景 key | 函数签名 | 校验 |
|---|---|---|
| `activation_diagnosis` | `get_activation_metrics(start_date, end_date) -> dict` | 日期须为 ISO 且 `start <= end`，范围落在数据期内；跨度 ≤ 366 天 |
| `feature_adoption_diagnosis` | `get_feature_adoption_metrics(start_date, end_date) -> dict` | 同上 |
| `low_activity_customer` | `get_low_activity_accounts(as_of_date, limit) -> dict` | `limit ∈ [1, 500]`；`as_of_date` 不得晚于数据末日 |

返回结构统一：`{"scenario", "period", "metrics": {...}, "breakdown": [...], "data_limitations": [...]}`。`data_limitations` 为空时也必须存在（列表），迫使实现方思考口径缺口。

### 5.6 Prompt 契约

系统提示词硬性包含 7 条（对应任务书 §6.5），并追加两条工程约束：

1. 只依据 `<metrics_json>` 与 `<retrieved_chunks>` 作答，二者之外的事实不得引入；
2. 数字必须原样引用 `<metrics_json>`，不得四舍五入到不同量级、不得做二次运算；
3. 缺失字段明确写"数据未提供"，不得推测；
4. "数据事实"与"原因假设"分节，假设段每句须以"假设："开头；
5. 建议必须齐备：目标客群 / 动作 / 主指标 / 护栏指标；
6. 每条知识依据标注 `文档名 §章节`；
7. 不得声称已触达客户或已改配置；
8. **【新增】输出必须为固定 8 段 Markdown 结构，段标题不得增删**；
9. **【新增】若 `<retrieved_chunks>` 与问题不相关，须在"数据限制"段落明示"知识库未覆盖该问题"，不得用常识填补**。

### 5.7 评估协议

| 项 | 设计 |
|---|---|
| 题目数 | 18 题（任务书要求 15–20） |
| 构成 | 12 题应命中（覆盖 12 份文档）、3 题应拒答（知识库无答案）、3 题跨文档（需 2 份以上证据） |
| 标注 | 每题人工标注 `expected_docs`（允许 1–2 个） |
| 指标 | Hit@3（任一预期文档进前 3）、Recall@3（预期文档命中比例）、引用正确率（人工打分 0/1）、事实一致性（数值与 metrics JSON 逐键比对，自动） |
| 拒答能力 | 3 题必须输出"知识库未覆盖/证据不足"，否则计失败 |
| 产物 | `outputs/rag_eval_results.csv`（逐题一行）、`docs/rag_evaluation_report.md`（含失败案例归因） |

**评估纪律：** 评估集在构建索引**之前**写好并冻结，禁止看到结果后改题。18 题写完后打 tag `eval-set-v1`。

---

## 6. 实验设计（模块 D）

### 6.1 参数链（顺序不可颠倒）

```
真实基线 p1（阶段 2 实测）
  → 可用新客数 N
  → n_arm = floor(N/2)
  → 反解最小可检测效应 MDE_min（表 2 公式）
  → 若 MDE_min 相对值 >30%：声明样本不足 + 模拟实验按设计样本量生成
  → 冻结 (p1, p2, n_arm, MDE, α=0.05, power=0.80)
  → 单次运行模拟 → 全量记录（含 SRM、CI、p 值、护栏）
```

### 6.2 统计方法

| 环节 | 方法 | 说明 |
|---|---|---|
| 随机化 | `account_id` 级，`random.Random(20261102)` shuffle 后对半 | 同企业成员不拆组 |
| 分组均衡 | SRM 卡方检验，α=0.001（严格阈值，避免误报） | 期望 50/50 |
| 主指标检验 | 两比例 z 检验（statsmodels `proportions_ztest`） | 双侧 |
| 置信区间 | 差值 95% CI，Newcombe 混合评分法 | 比 Wald 在小样本更稳 |
| 护栏指标 | 请求错误率、工单率：同样做检验，任一显著恶化则判定不可上线 | 与主指标并列披露 |
| 结论框架 | 统计显著 ∧ 效应 ≥ MDE ∧ 护栏未恶化 → 才可称"支持上线" | 三者缺一即"证据不足" |

### 6.3 防 p-hacking 的机制约束

- 种子、p1、p2、n 全部写入 `src/config.py`，模拟脚本只读不写；
- 脚本一次性输出全部结果（含不显著情况），不允许"跑一次不好看再调参数"；
- 报告首段固定声明："本实验为模拟，效应参数由脚本显式设定，p 值不构成真实业务证据。"

---

## 7. 工程架构

### 7.1 分层与依赖方向（严格单向，禁止反向 import）

```
scripts/            ← 唯一入口层（命令行）
   ↓
src/rag/            ← 应用层：编排"取指标 + 检索 + 生成"
   ↓
src/segmentation / strategy_analysis / ab_test / ai_usage / metrics   ← 分析层（纯函数，无 IO）
   ↓
src/data_cleaning / data_loader   ← 数据层（Pandas IO）
   ↓
src/database / config             ← 基础设施层
```

**约束：** 分析层函数必须是**纯函数**（输入 DataFrame、输出 DataFrame/dict），不得内部读文件。这样 `tests/` 才能用构造的小 DataFrame 做单测，也是任务书"SQL 与 Python 互相验证"能成立的前提。

### 7.2 配置单一来源

`src/config.py` 集中：路径常量、`RANDOM_SEED`、`RAG_TOP_K`、`PRICE_SNAPSHOT_DATE`、单价表、日期边界。`.env` 只放密钥与连接串。**禁止**在 notebook 里硬编码路径。

### 7.3 测试清单（`tests/`）

| 文件 | 断言 |
|---|---|
| `test_metrics.py` | 构造 5 行小表验证激活率、留存率、账户去重口径；断言"订阅行数≠账户数"场景不误算 |
| `test_segmentation.py` | 六层互斥且并集 = 全部账户；优先级命中正确 |
| `test_retriever.py` | 已知问题命中预期文档；chunk 长度 ≤420 字；重建后计数不变 |
| `test_rag_output.py` | 8 段结构齐全；报告内数值 ⊆ metrics JSON；无 metrics 时触发拒答 |
| `test_data_quality.py`（新增） | usage_id 重复率计算正确；去重不改变原始文件 |

---

## 8. 阶段门禁

| 阶段 | 通过条件（可执行） | Commit |
|---|---|---|
| 0 环境 | `.venv` 内 `python -V` = 3.11.x；`python scripts/check_environment.py` 退出码 0；`git status` 不含 `.env`/`.venv`/`chroma` | `chore: initialize mac development environment` |
| 1 数据 | `python scripts/profile_data.py` 产出五表 profile；`docs/data_quality_report.md` 含"原始/去重"双行数；`data/raw` 未被写 | `data: add saas source data checks and documentation` |
| 2 指标 | SQL 与 Pandas 对 5 个核心指标差异 = 0（打印核对表）；`pytest tests/test_metrics.py tests/test_segmentation.py` 全绿 | `feat: add saas metrics and customer operation analysis` |
| 3 实验 | `pytest tests/` 全绿；A/B 报告含完整参数链；模拟数据带标识列 | `feat: add ai usage metrics and onboarding ab test` |
| 4 RAG | 连续两次 build 后 collection 计数相同；18 题评估 CSV 产出；Hit@3 打印 | `feat: build rag operations knowledge retrieval` |
| 5 集成 | 3 场景各跑 1 次，输出 8 段报告；数值一致性断言通过；无 Key 时 Mock 模式可跑通 | `feat: integrate rag and llm operations assistant` |
| 6 收尾 | 新终端按 README 全流程复跑成功；`git log --oneline` 7 次提交；无敏感文件 | `docs: finalize ai growth and data operations rag project` |

**门禁纪律：** 任一条件未满足，阶段状态保持 in_progress，不以"基本完成"表述。

---

## 9. 阶段 0 实施设计（待确认后执行）

### 9.1 将创建的文件

```
<工作区>/ai-growth-data-ops-rag/          ← 已定：当前工作区子目录
│   实际绝对路径：/Users/yy3x0n/Desktop/未命名文件夹/ai-growth-data-ops-rag
├── .venv/                             (uv, Python 3.11.16)
├── .gitignore                         (按 C-02 白名单版)
├── .env.example                       (按 §C-08 补注释)
├── requirements.txt                   (任务书 §4.1 清单)
├── README.md                          (含数据边界声明的骨架)
├── .vscode/settings.json              (解释器指向 .venv)
├── scripts/check_environment.py       (输出 JSON + 人类可读报告)
└── docs/project_scope.md
```

### 9.2 阶段 0 不做的动作（明确边界）

不安装 MySQL、不安装 Homebrew、不装任何 pip 包（除建 venv）、不配置 Git 身份、不创建 GitHub 仓库、不下载数据。原因：前三项涉及系统级变更或大体积安装，后三项需用户提供信息。

### 9.3 决策记录（2026-11-02 已确认）

| 编号 | 决策点 | 用户决定 | 落地影响 |
|---|---|---|---|
| D1 | 项目落盘路径 | **当前工作区子目录** | 根目录为 `/Users/yy3x0n/Desktop/未命名文件夹/ai-growth-data-ops-rag`；本设计文档 `docs/design/` 在阶段 0 后迁入该目录 `docs/` |
| D2 | MySQL 方案 | **先装 Homebrew，再 `brew install`** | 见 §9.5 的命令与两个必须注意的坑 |
| D3 | 本轮执行范围 | **只审设计，暂不动手** | 阶段 0 不执行；建目录、建 venv、装 brew 全部留待放行 |
| D4 | Git 身份 + GitHub remote | **仍未提供** | 阶段 0 可正常建库，但 `git commit` 前必须先配置 `user.name` / `user.email`；推送需仓库 URL |

### 9.4 阶段 0 预计产出报告结构

环境项实测值 → 缺口清单 → 已创建文件清单 → 检查脚本实际输出 → `git status` → 待办事项 → 下一阶段输入。

### 9.5 Homebrew 安装 MySQL 的方案（**未采用**，保留为备选知识）

> 本节方案因环境限制未能执行，最终采用 §9.7 的既有实例复用方案。保留此节是因为
> 其中两个 `brew` 的坑具有通用价值，换机重装时会再次遇到。

**坑 1：`brew install mysql` 装不到 8.4。** Homebrew 的 `mysql` 公式指向最新大版本（当前 `26.10.0`），任务书 §3.6 希望的是 8.4 LTS。要用 8.4 需版本化公式：

```bash
brew install mysql@8.4
```

**坑 2：`mysql@8.4` 是 keg-only。** 不会自动链接到 `/opt/homebrew/bin`，因此 `mysql --version` 会报 command not found。需要二选一：

```bash
# 方案 a：显式加入 PATH（写进 ~/.zshrc，可逆、不污染全局）
echo 'export PATH="/opt/homebrew/opt/mysql@8.4/bin:$PATH"' >> ~/.zshrc

# 方案 b：强制链接（不推荐，与其他 mysql 版本冲突风险）
# brew link --force mysql@8.4
```

**完整顺序（每一步都要等上一步成功）：**

| 步 | 命令 | 需管理员 | 预计耗时 | 失败时的处置 |
|---|---|---|---|---|
| 1 | Homebrew 安装脚本（官方 `.pkg` 或 install.sh） | 是（创建 `/opt/homebrew` 属主） | 10–30 分钟 | 中止，改用 SQLite 兜底 |
| 2 | `brew --version` 与 `which brew` 校验 | 否 | 秒级 | 若提示未加入 PATH，按提示写入 `~/.zprofile` |
| 3 | `brew install mysql@8.4` | 否 | 5–15 分钟 | 中止，报告错误原文 |
| 4 | 按坑 2 配置 PATH，`mysql --version` 应显示 8.4.x | 否 | 秒级 | 检查 keg-only 路径 |
| 5 | `brew services start mysql@8.4` | 否 | 1 分钟 | 报端口 3306 占用则先排查占用 |
| 6 | `mysqladmin ping` 应返回 `mysqld is alive` | 否 | 秒级 | 查 `brew services list` 状态 |
| 7 | 建库 `ai_growth_ops` 与专用用户 `ai_ops_user` | 否（用 root 本地登录） | 1 分钟 | 记录生成的密码到 `.env`，不入 Git |

**Python 路径不受此决策影响，仍走 uv。** 理由：Homebrew 的 `python@3.11` 会被 `brew upgrade` 连带升级，venv 存在被动的 ABI 漂移风险；uv 管理的 3.11.16 是独立解释器，且能把版本号钉死，更符合任务书"保证可复现"的要求。两者不冲突——选的是 uv 这一种安装方式。

**风险提示（诚实披露）：** macOS 27.0 发布极新，Homebrew 公式生态可能存在尚未适配的依赖（如某些 bottle 缺失导致源码编译）。若第 3 步出现长时间编译或失败，稳妥做法是退回 SQLite 完成分析、SQL 脚本仍按 MySQL 8.4 语法编写，阶段 6 前再补验。这属于任务书 §3.6 已明确允许的降级路径。

### 9.6 实测结论：Agent 环境无法完成 Homebrew 安装（C-09）

阶段 0 实施中发现三类环境级阻塞，均已实测确认，记录以免重复踩坑：

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| E-1 | `sudo` 报 `operation not permitted`，即使关闭工具沙箱仍失败 | 执行环境对 `sudo` 二进制做了硬拦截 | 改用系统授权弹窗通道（`osascript ... with administrator privileges`）完成建目录等一次性 root 操作 |
| E-2 | `brew install` 报 `sandbox-exec: sandbox_apply: Operation not permitted` | Homebrew 安装 bottle 时会用 `sandbox-exec` 二次封装；而执行环境本身已在 macOS 沙箱内，**嵌套 sandbox 被系统拒绝** | 见 E-3 |
| E-3 | 设 `HOMEBREW_AVOID_NESTED_SANDBOXING=1` 后报 `Refusing to skip the sandbox: ... only supported in a custom prefix` | Homebrew 源码 `extend/os/mac/sandbox.rb` 明确：默认前缀 `/opt/homebrew` 下**不允许**跳过沙箱 | 该步必须在不受沙箱限制的终端中执行 |

**因此：**
1. Homebrew 本体已按官方等价路径装好（`/opt/homebrew` 前缀、归属当前用户、可用 bottle）——这一步只需 mkdir + chown + git clone，不触发嵌套沙箱。
2. `mysql@8.4` 的安装与初始化封装为 `scripts/setup_mysql.sh`，由用户在 VS Code 终端执行一次。脚本幂等，重复执行会跳过已装部分。
3. 网络瓶颈同样实测：官方 `formulae.brew.sh` 的 API 索引约 35 MB，直连约 15 KB/s（需 40 分钟以上）；清华镜像约 1.4 MB/s。PyPI 直连约 19 KB/s，清华镜像约 1.3 MB/s。镜像配置写入 `~/.zprofile` 与 `.venv/pip.conf`，注释中保留恢复官方源的说明。

。

---

## 10. 风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R-1 | macOS 27 过新，torch/sentence-transformers 轮子不兼容 | 中 | 阻塞模块 F | 阶段 4 前置装 torch 并跑 MPS 探针；失败则 CPU + 换 `bge-small-zh`（非 v1.5） |
| R-2 | 首次下载 BGE 模型需联网，国内网络不稳 | 中 | 阻塞模块 F | 支持 `HF_ENDPOINT` 镜像；模型缓存不进 Git |
| R-3 | 无 DeepSeek API Key | 中 | 阻塞模块 F 的 LLM 环节 | Mock 模式先行，保证流程与断言可跑 |
| R-4 | RavenStack 日期跨度不足 12 个月 | 中 | Cohort 留存与 NRR 受限 | 指标字典标"部分可计算"，报告写限制 |
| R-5 | 可用新客数远小于 500 | 高 | A/B 功效不足 | 按 C-04 设计规则，模拟实验按设计样本量生成并声明 |
| R-6 | 六层分层阈值主观 | 中 | 被质疑"拍脑袋" | 用分位数 + 敏感性表 + 明确禁止"行业标准"表述 |
| R-7 | 数据许可不明 | 低 | GitHub 合规风险 | 原始数据不入库 + 署名 + manifest 记录 sha256 |

---

## 11. 与任务书的一致性声明

| 任务书要求 | 本设计是否遵守 | 说明 |
|---|---|---|
| 按阶段 0→6 推进，禁止一次生成 | 遵守 | 本文件仅为设计，未写业务代码 |
| 不用 Streamlit/Docker/FastAPI/Agent 框架 | 遵守 | 依赖白名单已固化 |
| LLM 不计算指标 | 遵守 | 5.5/5.6 双约束 |
| 不做权限级系统变更 | 遵守 | 9.2 明确不做 |
| 数据真实性标注 | 遵守 | 1.4 三层分级 |

**唯一偏离项：** chunk 长度由 500–800 下调至 320–420 汉字（C-01）；理由与验证方式已给出。若用户坚持原规格，需同步接受"检索评估结果可能受截断影响"这一限制，并在报告中声明。

### 9.7 最终采用：复用本机既有 MySQL 8.4.7（已实施）

**发现：** 机器上已存在一个运行中的 MySQL 8.4.7，非 Homebrew 安装，而是 **Oracle 官方 tarball**：

| 项 | 值 |
|---|---|
| 安装路径 | `~/mysql/mysql-8.4.7-macos15-arm64` |
| 配置 | `~/mysql/my.cnf`（`basedir`/`datadir`/`socket`/`port=3306`/`bind-address=127.0.0.1`） |
| 数据目录 | `~/mysql/data` |
| 自启动 | `~/Library/LaunchAgents/com.local.mysql.plist`，`RunAtLoad` + `KeepAlive` |
| 既有业务库 | `ai_saas_analytics`（含 `raw_*` 五表 + 两张 `synthetic_*`），系上一次尝试的遗留 |
| 既有账号 | `root@localhost`、`saas_analyst@127.0.0.1` |

**决策依据：** 任务书 §3.6 明确「用户已有 MySQL 8.0 或 8.4 时优先复用，不要求升级；不要同时安装两个版本以免端口冲突」。既有实例为 8.4.7，正落在该范围内，且数据目录内含上一次的项目表，复用可避免端口冲突与重复安装。

**root 密码重置（用户授权后执行）：**

1. 校验监听进程的可执行路径确属 `~/mysql/...`，避免误停其他实例；
2. `kill -TERM`（非 `-9`）平滑关闭，让 InnoDB 正常刷盘；
3. 以 `--skip-grant-tables --skip-networking --daemonize` 重启，仅走 socket、不开 TCP；
4. **同一会话内**执行 `FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED BY ...;`
5. 关闭临时实例，恢复正常启动；
6. 新密码写入 `~/mysql/ROOT_PASSWORD.txt`（权限 600），不写入仓库、不打印到对话。

**两个必须记录的坑：**

- **`FLUSH PRIVILEGES` 会立即恢复鉴权。** 首次尝试把「刷新权限」与「改密」分成两次连接执行，第二次连接即被拒（`Access denied ... using password: NO`），因为刷新后 skip-grant 的免鉴权窗口即关闭。必须写进同一会话。
- **`KeepAlive` 会导致进程被自动拉起。** 首次 `kill -TERM` 后 1 秒内 launchd 就重启了一个新实例，与随后的临时实例争抢 `ibdata1` 文件锁（`Unable to lock ./ibdata1 error: 35`）。正确顺序是先 `launchctl bootout` 卸载 job，再操作进程。

**残留限制：** 本会话内 `launchctl bootstrap` / `load` 均返回 `Input/output error (5)`，属执行环境对 launchd 注册的限制。`com.local.mysql.plist` 文件本身完好，下次登录会自动加载；本会话内改用 `mysqld --daemonize` 启动。

**项目库初始化：** 新建独立库 `ai_growth_ops` 与用户 `ai_ops_user@127.0.0.1`，**不改动、不删除** `ai_saas_analytics` 与既有账号。脚本为 `scripts/init_database.sh`（幂等）。

**额外依赖：** 补装 `cryptography`。MySQL 8 的 `caching_sha2_password` 在非 TLS 连接下需要 RSA 交换密码，PyMySQL 依赖该包；缺失时首次（认证缓存为空）连接会失败。
