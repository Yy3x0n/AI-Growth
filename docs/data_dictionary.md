# 数据字典（Data Dictionary）

> 版本：v1.0　｜　编制：2026-11-02　｜　对应阶段：1
> 数据来源与校验和见 [`data_manifest.md`](data_manifest.md)
> 质量问题与处置见 [`data_quality_report.md`](data_quality_report.md)
> **数据性质：公开合成 SaaS 数据集（非真实企业数据）。**

---

## 0. 总览

| 表 | 行数 | 列数 | 统计粒度 | 主键 |
|---|---:|---:|---|---|
| `ravenstack_accounts` | 500 | 10 | 一账户一行 | `account_id` |
| `ravenstack_subscriptions` | 5,000 | 14 | 一订阅一行（**账户可多条**） | `subscription_id` |
| `ravenstack_feature_usage` | 25,000 | 8 | 一次功能使用事件 | `usage_id`（**存在重复**） |
| `ravenstack_support_tickets` | 2,000 | 9 | 一工单一行 | `ticket_id` |
| `ravenstack_churn_events` | 600 | 9 | 一流失事件一行（**账户可多条**） | `churn_event_id` |
| 合计 | 33,100 | — | — | — |

**时间范围**：`2023-01-01` ~ `2024-12-31`（两年，覆盖 24 个自然月）。

---

## 1. accounts（账户画像）

| 字段 | 类型 | 说明 | 取值 / 范围 | 备注 |
|---|---|---|---|---|
| `account_id` | string | 账户主键 | `A-xxxxxx` | 全项目统计主体 |
| `account_name` | string | 账户名 | `Company_N` | 匿名化占位名，无业务含义 |
| `industry` | string | 行业 | DevTools 113 / FinTech 112 / Cybersecurity 100 / HealthTech 96 / EdTech 79 | 5 类 |
| `country` | string | 国家 | US 291 / UK 58 / IN 49 / AU 32 / DE 25 / CA 23 / FR 22 | 7 类，美国占 58% |
| `signup_date` | date | 注册日期 | 2023-01-02 ~ 2024-12-31 | **见质量问题 Q-23：与使用时间不连贯** |
| `referral_source` | string | 获客渠道 | organic 114 / other 103 / ads 98 / event 96 / partner 89 | 5 类，分布均匀 |
| `plan_tier` | string | 当前套餐 | Pro 178 / Basic 168 / Enterprise 154 | 与订阅表的套餐可能不一致 |
| `seats` | int | 席位数 | 1 ~ 163 | 账户级快照 |
| `is_trial` | bool | 是否试用 | True / False | |
| `churn_flag` | bool | 是否流失 | True 110 / False 390 | **与 churn_events 口径冲突，见 Q-17** |

---

## 2. subscriptions（订阅明细）

| 字段 | 类型 | 说明 | 取值 / 范围 | 备注 |
|---|---|---|---|---|
| `subscription_id` | string | 订阅主键 | 唯一，5,000 条 | |
| `account_id` | string | 外键 → accounts | 500 个账户 | 每账户中位 10 条，最多 19 条 |
| `start_date` | date | 订阅开始 | 2023-01-09 ~ 2024-12-31 | |
| `end_date` | date | 订阅结束 | 空值 4,514 条 | **空值 = 仍在生效**，非缺失 |
| `plan_tier` | string | 套餐 | Enterprise 1,723 / Pro 1,675 / Basic 1,602 | |
| `seats` | int | 席位 | 1 ~ 189 | 与账户级 `seats` 可不同 |
| `mrr_amount` | int | 月度经常性收入（美元） | 0 ~ 33,830，均值 2,267.75 | **778 条 ≤0**，多为试用 |
| `arr_amount` | int | 年度经常性收入（美元） | 同上量级 | 基本为 `mrr × 12` |
| `is_trial` | bool | 是否试用订阅 | | |
| `upgrade_flag` | bool | 本期升级 | | 用于扩张收入分析 |
| `downgrade_flag` | bool | 本期降级 | | 用于收缩收入分析 |
| `churn_flag` | bool | 该订阅是否流失 | | **订阅级**，与账户级流失不同 |
| `billing_frequency` | string | 计费周期 | monthly 2,539 / annual 2,461 | |
| `auto_renew_flag` | bool | 自动续费 | | 可作风险信号 |

**关键陷阱：订阅行数 ≠ 账户数。** 500 个账户对应 5,000 条订阅，任何"账户数"必须
`COUNT(DISTINCT account_id)`，任何"MRR"必须先按账户-月择一有效订阅（Q-19）。

---

## 3. feature_usage（功能使用事件）

| 字段 | 类型 | 说明 | 取值 / 范围 | 备注 |
|---|---|---|---|---|
| `usage_id` | string | 事件 ID | 24,979 个唯一值 / 25,000 行 | **21 行 ID 重复**，见 Q-05 |
| `subscription_id` | string | 外键 → subscriptions | 无孤子 | **本表无 `account_id`**，账户级分析须经订阅表映射 |
| `usage_date` | date | 使用日期 | 2023-01-01 ~ 2024-12-31 | **76.6% 早于所属订阅开始日**，见 Q-15 / Q-23 |
| `feature_name` | string | 功能名 | `feature_1` ~ `feature_40`，40 个 | **编号占位名，无 AI 语义**，见 Q-22 |
| `usage_count` | int | 当期使用次数 | 0 ~ 26，均值 10.02 | 2 条 ≤0 |
| `usage_duration_secs` | int | 使用时长（秒） | 数值型 | |
| `error_count` | int | 期内错误次数 | 数值型 | **唯一可用的质量信号**，无 `task_success` 字段 |
| `is_beta_feature` | bool | 是否灰度功能 | | |

**关键陷阱：**

1. **没有 `task_success` 字段。** 任务书模块 A/B 要求的"首次有效任务完成率"在真实数据上
   **无法直接计算**，只能用 `error_count = 0` 作为代理口径，且必须显式标注为代理。
2. **`feature_name` 是编号而非业务功能名。** 不能把 `feature_12` 称作「AI 问答」。

---

## 4. support_tickets（支持工单）

| 字段 | 类型 | 说明 | 取值 / 范围 | 备注 |
|---|---|---|---|---|
| `ticket_id` | string | 工单主键 | 唯一，2,000 条 | |
| `account_id` | string | 外键 → accounts | 无孤子 | 一账户可多工单 |
| `submitted_at` | datetime | 提交时间 | 2023-01-02 ~ 2024-12-31 | |
| `closed_at` | datetime | 关闭时间 | | 未关闭为空的处理需在分析时明确 |
| `resolution_time_hours` | float | 解决时长（小时） | | |
| `priority` | string | 优先级 | urgent 514 / high 510 / medium 491 / low 485 | 分布均匀，无明显倾斜 |
| `first_response_time_minutes` | int | 首次响应（分钟） | | 可做工单服务指标 |
| `satisfaction_score` | float | 满意度评分 | **空值 825 条（41.25%）**，非空范围 3~5，均值 3.98 | **分母只能用非空值**，不得按 0 填充 |
| `escalation_flag` | bool | 是否升级 | | 可作客户健康风险信号 |

---

## 5. churn_events（流失事件）

| 字段 | 类型 | 说明 | 取值 / 范围 | 备注 |
|---|---|---|---|---|
| `churn_event_id` | string | 事件主键 | 唯一，600 条 | 注意列名不是 `event_id` |
| `account_id` | string | 外键 → accounts | 无孤子，**352 个唯一账户** | 一账户可多次流失/复活 |
| `churn_date` | date | 流失日期 | 2023-01-25 ~ 2024-12-31 | |
| `reason_code` | string | 流失原因 | features 114 / support 104 / budget 104 / unknown 95 / competitor 92 / pricing 91 | 6 类 |
| `refund_amount_usd` | float | 退款金额（美元） | 0 ~ 392.92，**仅 142 条非零** | 大量 0 值，做退款分析需先过滤 |
| `preceding_upgrade_flag` | bool | 流失前是否升级 | | 反直觉信号，可作分析点 |
| `preceding_downgrade_flag` | bool | 流失前是否降级 | | 强流失预警信号 |
| `is_reactivation` | bool | 是否为复活事件 | True 61 / 600 | **含复活事件，故行数 ≠ 净流失账户** |
| `feedback_text` | string | 客户反馈原文 | 空值 148 条 | 定性材料，非结构化 |

**关键陷阱：事件行数 600 ≠ 流失账户数 352 ≠ `accounts.churn_flag=True` 的 110。**
三者口径不同，不可混用（Q-17）。

---

## 6. 表间关系

```
accounts (account_id)
   ├──1:N── subscriptions (account_id)         500 → 5,000
   │            └──1:N── feature_usage (subscription_id)   5,000 → 25,000
   ├──1:N── support_tickets (account_id)       500 → 2,000
   └──1:N── churn_events (account_id)          500 → 600（352 个账户）

外键完整性：四组关联均为 0 条孤子（见 Q-07 ~ Q-10）。
```

**连接代价提示：** `feature_usage` 到 `account_id` 必须经 `subscription_id` 中转。
由于一款账户平均 10 条订阅，直接三表 join 会产生行数放大，聚合前需先明确粒度。

---

## 7. 衍生字段口径（阶段 2 实施）

| 衍生字段 | 定义 | 依赖 | 限制 |
|---|---|---|---|
| `account_month_snapshot` | 账户-月末快照 | subscriptions | 同一账户同月多条订阅时按「`start_date` 最新 → `mrr` 最高 → `subscription_id`」择一 |
| `is_active_account` | 当月是否有 ≥1 条 usage 记录 | feature_usage | 不含 `usage_count=0` 的口径需单独定义 |
| `has_error_free_usage` | `error_count = 0` 的记录 | feature_usage | 「有效任务」的**代理口径**，非真实任务成功率 |
| `first_use_date` | 账户首次使用日 | feature_usage | **仅此锚点时间自洽**，见 Q-23 |
| `account_churned` | 是否存在净流失 | churn_events / churn_flag | 二者冲突，阶段 2 需择一并写明 |
