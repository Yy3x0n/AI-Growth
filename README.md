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
| 0 | 环境与项目初始化 | 进行中 |
| 1 | 数据获取与质量审查 | 未开始 |
| 2 | 指标分析与用户运营 | 未开始 |
| 3 | A/B Test 与 AI 使用分析 | 未开始 |
| 4 | RAG 知识库与检索 | 未开始 |
| 5 | RAG + 业务数据 + LLM 集成 | 未开始 |
| 6 | 最终报告与复现验收 | 未开始 |

设计基线见 [`docs/00_requirements_and_design.md`](docs/00_requirements_and_design.md)，
其中记录了 8 条对原始任务书的勘误与处置理由。

---

## 技术栈

- Python 3.11（uv 管理，见下文）
- pandas / numpy / scipy / statsmodels / scikit-learn
- MySQL 8.4 LTS（Homebrew `mysql@8.4`，keg-only）
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

Homebrew 前缀为标准 `/opt/homebrew`（arm64 预编译包才可用）。MySQL 使用 8.4 LTS 版本化公式：

```bash
brew install mysql@8.4
```

> `mysql@8.4` 是 **keg-only**，不会自动进 PATH。已写入 `~/.zprofile`：
> `export PATH="/opt/homebrew/opt/mysql@8.4/bin:$PATH"`

### 3. 密钥配置

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 与 MySQL 密码
```

`.env` 已在 `.gitignore` 中，**永不入库**。没有 API Key 时可使用 Mock 模式验证流程。

---

## 常用命令

```bash
source .venv/bin/activate

python scripts/check_environment.py   # 环境自检，退出码 0 表示无阻塞
python scripts/profile_data.py        # 数据概况（阶段 1 后可用）
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
