# AI 应用增长与数据运营策略分析平台

以一个**模拟的 B2B AI SaaS 产品**为业务背景，分析企业客户从注册、首次使用 AI、功能采用、
持续活跃到订阅/流失的完整过程；通过客户分层与 A/B Test 设计运营策略；再用 RAG 检索产品说明、
指标口径与运营手册，生成有数据依据、可追溯来源的运营诊断报告。

---

## ⚠️ 数据边界声明（请先读这一段）

本项目涉及三类数据，性质完全不同，**任何结论都必须带上对应标注**：

| 标注 | 数据 | 性质 | 允许的表述 |
|---|---|---|---|
| **T1** | RavenStack 五张 CSV | 公开**合成** SaaS 数据（非真实企业数据） | 「公开合成数据集」 |
| **T2** | `data/simulated/ai_request_logs.csv` | 按固定种子与明确规则**生成**的模拟日志 | 「模拟日志，用于演示分析方法」 |
| **T3** | `data/simulated/ab_test_simulated.csv` | 固定种子的**模拟**实验分组 | 「模拟实验，不代表线上业务成果」 |

**本仓库不代表任何真实企业的运营成果。** 所有 A/B 结论、成本数字、成功率均基于上述标注，
不得引用为真实业务证据。

---

## 项目状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 环境与项目初始化 | ✅ 完成（自检 27 PASS / 0 WARN / 0 FAIL） |
| 1 | 数据获取与质量审查 | ✅ 完成（24 项检查，5 个实质问题已定处置） |
| 2 | 指标分析与用户运营 | ✅ 完成（17 项 SQL/Pandas 对账全一致，16 个单测通过） |
| 3 | A/B Test 与 AI 使用分析 | 未开始 |
| 4 | RAG 知识库与检索 | 未开始 |
| 5 | RAG + 业务数据 + LLM 集成 | 未开始 |
| 6 | 最终报告与复现验收 | 未开始 |

### 文档导航

| 文档 | 回答什么问题 |
|---|---|
| [`00_requirements_and_design.md`](docs/00_requirements_and_design.md) | 设计基线与 12 条勘误 |
| [`data_dictionary.md`](docs/data_dictionary.md) | 五个表每个字段是什么 |
| [`data_quality_report.md`](docs/data_quality_report.md) | 24 项质量检查与 7 条处置决策 |
| [`data_capability_assessment.md`](docs/data_capability_assessment.md) | **这套数据能做什么、不能做什么** |
| [`metric_dictionary.md`](docs/metric_dictionary.md) | 每个指标的口径、分母、限制 |
| [`segmentation_findings.md`](docs/segmentation_findings.md) | 六层分层结果与阈值敏感性 |

> **阶段 1 的关键结论：** 该数据集的 `signup_date` 与使用时间不连贯——490/500 个账户的首次使用
> 早于注册日。因此「注册→首次使用」的激活漏斗**不能**用真实数据构造，已改为「以首次使用日为锚」
> + 模拟日志双轨方案。这不是回避问题，而是数据本身不支持；详见数据质量报告 Q-23。

---

## 技术栈

- Python 3.11（uv 管理，见下文）
- pandas / numpy / scipy / statsmodels / scikit-learn
- MySQL 8.4.7（本机已有 Oracle tarball 实例，端口 3306）
- Embedding：`BAAI/bge-small-zh-v1.5`（本地推理，512 维）
- 向量库：ChromaDB（本地持久化，`data/chroma/`，不入库）
- LLM：DeepSeek `deepseek-flash`（仅生成文字，不计算指标）

**明确不使用**：Streamlit、Power BI、Docker、FastAPI、LangChain、LlamaIndex、多智能体框架。

---

## 环境准备

### 1. 虚拟环境（已完成）

本项目使用 uv 管理的 Python 3.11.16：

```bash
cd ai-growth-data-ops-rag
source .venv/bin/activate
python -V          # 应为 3.11.16
```

VS Code 中：`Cmd + Shift + P` → `Python: Select Interpreter` → 选择
`.venv/bin/python`；打开 `.ipynb` 时选择同一个 Kernel。

### 2. Homebrew 与 MySQL

Homebrew 已安装到标准前缀 `/opt/homebrew`（arm64 预编译包要求该路径）。

**MySQL 复用本机已有实例**，不另外安装：

| 项 | 值 |
|---|---|
| 版本 | 8.4.7（Oracle 官方 tarball，Community Server GPL） |
| 安装位置 | `~/mysql/mysql-8.4.7-macos15-arm64` |
| 数据目录 | `~/mysql/data` |
| 监听 | `127.0.0.1:3306`，socket 见 `~/mysql/my.cnf` |
| 开机自启 | `~/Library/LaunchAgents/com.local.mysql.plist`（KeepAlive） |
| 客户端 | `~/mysql/mysql-8.4.7-macos15-arm64/bin`，已写入 `~/.zprofile` 的 PATH |
| root 密码 | `~/mysql/ROOT_PASSWORD.txt`（权限 600，**不入库**） |

数据库与专用用户初始化（幂等）：

```bash
bash scripts/init_database.sh
```

脚本会创建库 `ai_growth_ops` 与用户 `ai_ops_user`（密码取自 `.env`），并用应用用户回连验证。

> 说明：Agent 环境无法执行 Homebrew 装包——Homebrew 安装 bottle 时会用 `sandbox-exec`
> 二次封装，在已处于 macOS 沙箱内的进程中因嵌套沙箱被系统拒绝，且 Homebrew 明确拒在
> 默认前缀下跳过沙箱。这也是本机改用既有 MySQL 实例的原因之一。
> 详见 `docs/00_requirements_and_design.md` 第 9.6 节。

### 3. 密钥配置

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY；MySQL 密码在初始化时已随机生成
```

`.env` 已在 `.gitignore` 中，**永不入库**。没有 API Key 时可使用 Mock 模式验证流程。

---

## 常用命令

```bash
source .venv/bin/activate

python scripts/check_environment.py   # 环境自检，退出码 0 表示无阻塞
bash scripts/init_database.sh         # 初始化库与用户（幂等）
python scripts/download_data.py       # 下载五张原始 CSV（已存在则跳过）
python scripts/profile_data.py        # 数据画像与质量检查
python scripts/load_to_mysql.py       # 建表 + 装载（行数校验）
python scripts/run_sql.py sql/02_kpi_analysis.sql   # 建指标视图
python scripts/run_analysis.py        # 指标计算 + SQL/Pandas 全量对账
python scripts/run_sql.py sql/03_customer_segments.sql
python scripts/segment_accounts.py    # 客户分层 + 对账
pytest -q                             # 单元测试
python scripts/run_analysis.py        # 指标分析（阶段 2 后可用）
python scripts/build_knowledge_base.py # 构建向量库（阶段 4 后可用）
python scripts/ask_ops_assistant.py   # 运营助手（阶段 5 后可用）
pytest                                # 单元测试
```

---

## 目录结构

```text
ai-growth-data-ops-rag/
├── data/
│   ├── raw/            # RavenStack 原始 CSV（只读，不入库）
│   ├── processed/      # 清洗后数据（不入库）
│   ├── simulated/      # T2 / T3 模拟数据（不入库）
│   └── chroma/         # 本地向量库（不入库，可重建）
├── knowledge_base/     # 12 份模拟运营知识 Markdown
├── docs/               # 设计、字典、质量报告、各模块分析报告
├── notebooks/          # 4 个分析 Notebook
├── sql/                # 建表与 KPI SQL（MySQL 8.4 语法）
├── src/                # 业务代码（config / 分析 / rag）
├── scripts/            # 命令行入口
├── tests/              # pytest
└── outputs/            # 聚合结果（仅小体积 CSV/MD 入库）
```

---

## 数据来源与署名

- 主数据集：RavenStack SaaS Subscription & Churn Analytics（**合成数据**）
  - Kaggle：<https://www.kaggle.com/datasets/rivalytics/saas-subscription-and-churn-analytics-dataset>
  - 分析仓库：<https://github.com/tivonio/sql-churn-analysis>（其 `data/raw/` 含五个原始 CSV）
- 该仓库未声明 LICENSE，因此**原始 CSV 不随本仓库分发**，改由 `scripts/download_data.py` 重建，
  并在 `docs/data_manifest.md` 记录来源 URL、下载日期与每文件 sha256。
- Embedding 模型：`BAAI/bge-small-zh-v1.5`（MIT License，BAAI / FlagEmbedding）

---

## 已知限制

1. 数据为账户级（account），**无 user 粒度**，因此 DAU/WAU/MAU 类指标按账户口径计算，
   「人均」类指标实际为「户均」。
2. `feature_usage` 存在重复 `usage_id`，去重规则与影响行数记录在数据质量报告。
3. 模拟日志不包含人工质量评价，因此**不推断模型回答质量**。
4. 成本为「按假设单价估算」，单价快照 2026-09-18，非真实账单。
