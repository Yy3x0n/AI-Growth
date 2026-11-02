# 验收清单

> 验收日期：2026-11-02
> 验收方式：`bash scripts/reproduce.sh` 全链路实跑 + 干净 shell 复核 + 仓库安全检查
> 全部结论均为**本次实际输出**，不是"应该通过"的推断。

---

## 一、任务书阶段 6 验收条件

| # | 验收条件 | 结果 | 证据 |
|---|---|---|---|
| 1 | 新环境可按 README 完成安装、数据准备、指标分析、知识库构建与问答演示 | ✅ | 见 §2 干净 shell 复核 |
| 2 | 核心测试通过 | ✅ | `200 passed in 45.42s` |
| 3 | README 说明模拟数据边界 | ✅ | README「数据边界声明」表 + 看板顶部横幅 |
| 4 | 仓库不含 `.env` / API Key / MySQL 凭据 / `.venv` / Chroma / 大模型文件 | ✅ | 见 §3 仓库安全检查 |

---

## 二、全链路复现（`bash scripts/reproduce.sh`）

**总耗时 5 分 31 秒，18 步全部成功，退出码 0。**

| # | 步骤 | 耗时 | 关键输出 |
|---|---|---:|---|
| 1 | 环境自检 | 5s | PASS 27 / WARN 0 / FAIL 0 |
| 2 | 下载原始数据 | 0s | 已存在，跳过 |
| 3 | 数据画像与质量检查 | 2s | 24 项检查，5 表 33,100 行 |
| 4 | 入仓（建表 + 装载） | 1s | 行数全部一致 |
| 5 | 创建 KPI 视图 | 0s | 执行成功 |
| 6 | 指标计算 + 对账 | 2s | **17 项全部一致（容差 1e-6）** |
| 7 | 生成模拟数据 | 4s | 参数恢复自检 6 项偏差 <0.3pp |
| 8 | 装载模拟日志 | 3s | 69,403 行 |
| 9 | AI 使用分析 + 对账 | 2s | 23 个指标列一致 |
| 10 | A/B 实验分析 | 3s | 决策：证据不足，不建议直接上线 |
| 11 | 框架自校验 | 0s | 默认跳过（`SKIP_HARNESS=0` 启用） |
| 12 | 客户分层 + 对账 | 2s | 层规模、层收入零差异 |
| 13 | 构建知识库索引 | 37s | 12 文档 → 67 块 |
| 14 | 检索评估 | 36s | Hit@3 1.0 / Top1 0.8667 / MRR 0.9333 |
| 15 | 生成诊断报告 | 180s | 3 份全部「正常」，审计通过 |
| 16 | 生成图表与看板 | — | 6 张图 + `dashboard_static.html` + **ECharts 交互版 `dashboard.html`**（1.03 MB，KPI 卡/增长/分层/AI/实验四区） |
| 17 | 单元测试 | 49s | **200 passed** |
| 18 | 密钥扫描 | 2s | 119 文件零告警 |

### 2.1 复现一致性：数字是否与文档记录相同

复现的意义不是"跑通了"，而是**跑出来的数字和文档写的一样**。逐项比对：

| 指标 | 文档记录 | 本次复跑 | 一致 |
|---|---|---|:--:|
| A/B 主指标 | 25.1710% → 26.8582% | 25.1710% → 26.8582% | ✅ |
| 绝对提升 | +1.69pp | +1.69pp | ✅ |
| 95% CI | [-0.91, +4.28] | [-0.0091, +0.0428] | ✅ |
| p 值 | 0.2029 | 0.2029 | ✅ |
| 护栏 错误率 p | 0.0190 | 0.0190 | ✅ |
| 护栏 工单率 p | 0.0005 | 0.0005 | ✅ |
| 分层账户合计 | 500 | 500 | ✅ |
| 高价值收入占比 | 57.1% | 57.1% | ✅ |
| 末月 MRR | 1,286,108 | 1,286,108 | ✅ |
| 检索 Hit@3 / Top1 / MRR | 1.0 / 0.8667 / 0.9333 | 1.0 / 0.8667 / 0.9333 | ✅ |
| 指标对账 | 17 项一致 | 17 项一致 | ✅ |
| 单元测试 | 200 | 200 | ✅ |

**结论：可复现。** 随机种子固定在 `src/config.py`，模拟数据与实验分组均只读该种子。

---

## 三、干净 shell 复核（模拟"新终端"）

用 `env -i` 清空环境变量 + 登录 shell 重跑，验证不依赖当前会话的临时状态：

```
$ env -i HOME=$HOME PATH=/usr/bin:/bin:/usr/sbin:/sbin zsh -lc '...'
which python3: /usr/bin/python3
which brew:    /opt/homebrew/bin/brew                    ← 来自 ~/.zprofile
which mysql:   .../mysql-8.4.7-macos15-arm64/bin/mysql   ← 来自 ~/.zprofile
venv python:   Python 3.11.16 → .venv/bin/python
合计: PASS 27 项 / WARN 0 项 / FAIL 0 项
```

关键点：系统自带的 `python3` 是 3.x 但不是 3.11，**必须先 `source .venv/bin/activate`**；
`brew` 与 `mysql` 依赖 `~/.zprofile` 中的 PATH 配置，README 已注明。

---

## 四、仓库安全检查

| 检查项 | 结果 |
|---|---|
| `.env` 是否被跟踪 | 否（`git ls-files` 无此项，`.gitignore:3` 排除） |
| 提交历史是否含 API Key | 否（`git log --all -S` 搜索 key 前后两段，均 0 条） |
| 远端是否含 `.env` | 否（远端 tree 中仅 `.env.example`） |
| 远端代码搜索 key | 0 条 |
| `.venv/` 是否入库 | 否 |
| `data/chroma/` 是否入库 | 否 |
| 模型权重是否入库 | 否（缓存在 `~/.cache/huggingface`，不在仓库） |
| 原始 CSV 是否入库 | 否（`data/raw/*` 被忽略，由下载脚本获取） |
| 单个文件 > 2 MB | 无 |
| 提交前密钥钩子 | 已启用（`core.hooksPath=.githooks`），实测拦截 4 类泄露路径 |
| 全仓密钥扫描 | `✓ 已检查 119 个文件，未发现明文密钥` |

> 注：`.env` 需手工创建（`cp .env.example .env`）。示例文件只含占位符。

---

## 五、交付物核对

| 交付物 | 路径 | 状态 |
|---|---|---|
| 五个 SQL 脚本 | `sql/01_create_tables.sql`、`01b_load_from_staging.sql`、`02_kpi_analysis.sql`、`03_customer_segments.sql`、`04_ai_usage.sql` | ✅ |
| 指标模块 | `src/metrics.py`、`segmentation.py`、`ai_usage.py`、`ab_test.py`、`simulation.py` | ✅ |
| RAG 模块 | `src/rag/chunker.py`、`embedding.py`、`store.py`、`retriever.py`、`audit.py`、`assistant.py` | ✅ |
| LLM 客户端 | `src/llm_client.py`（glm / deepseek / mock 可切换） | ✅ |
| 白名单取数 | `src/ops_metrics.py`（三场景 + 四重参数校验） | ✅ |
| 脚本 | 18 个（含 BI 数据层与交互看板生成器） | ✅ |
| 知识库 | `knowledge_base/` 12 份文档 + `eval/questions.yaml`（18 题冻结） | ✅ |
| 文档 | 18 份（设计基线、数据字典、质量报告、数据清单、能力边界、指标字典、分层、策略、模拟方法、模拟清单、A/B、AI 使用、RAG 设计、RAG 评估、LLM 集成、项目范围、最终报告、验收清单） | ✅ |
| 生成物 | `outputs/tables/*.csv`（24 张）、`outputs/figures/*.png`（6 张）、`dashboard.html`、`reports/*.md`（3 份） | ✅ |
| 测试 | `tests/` 11 个文件，200 个用例 | ✅ |

---

## 六、已知未完成 / 受限项（如实记录）

| 项 | 状态 | 原因 |
|---|---|---|
| Tableau 看板 | 未做，改为自包含 ECharts 交互 HTML | Tableau 需桌面客户端且 .twb 手写 schema 脆弱，他人克隆仓库无法复现；ECharts 内联进单文件 HTML，双击可看、可交互筛选 |
| Jupyter Notebook | 未做，改为可执行脚本 | 任务书原计划 4 个 Notebook。实际全部分析以脚本形式提供：可被 `reproduce.sh` 串联、可被 pytest 覆盖、git diff 可读。Notebook 的隐藏状态与输出难以复核，不适合作为复现入口 |
| 真实注册→激活漏斗 | 未做 | T1 时间轴不连贯（490/500 账户首次使用早于注册日），改用「注册→首次付费订阅」 |
| 用户级指标（DAU/人均时长） | 未做 | 数据中无 user 级表 |
| 流失预测模型 | 未做 | 500 账户、无生命周期，做出来必过拟合 |
| RAG 留出测试集 | 未做 | 现有 18 题参与过调优，已在文档标注为形成性评估 |
| A/B 序列检验 | 未做 | 当前用固定样本量；"边跑边看"需 alpha spending，列为后续方向 |
| 数字审计的语义校验 | 部分 | 只能查"数字是否来自数据"，查不了"用错地方"（已给真实反例） |

---

## 七、复现失败时的排查顺序

1. **环境自检先跑**：`python scripts/check_environment.py`，退出码 2 表示 FAIL。
2. **MySQL 未启动**：本机由 LaunchAgent 托管，`launchctl kickstart -k gui/$(id -u)/com.local.mysql`。
3. **模型下载失败**：`huggingface.co` 直连在本机不可用，需 `HF_ENDPOINT=https://hf-mirror.com`
   与 `HF_HUB_DISABLE_XET=1`（已写入 `~/.zprofile`，README 有说明）。
4. **报告降级**：检查 `.env` 的 `GLM_API_KEY`；无 key 时加 `--mock` 应能跑通。
5. **检索指标异常**：先 `python scripts/build_knowledge_base.py --rebuild` 重建索引。
