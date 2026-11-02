-- ============================================================================
-- 02_kpi_analysis.sql
-- 指标体系与经营监控（视图层）——**真实数据（T1）适用范围**
--
-- ⚠️ 适用范围声明
-- 阶段 1/2 的实测证明：本数据集**不具备账户生命周期**——
--   · 88% 的账户"首次使用"都落在数据起始月（左截断）
--   · 500 个账户每月活跃 410~440 个，24 个月无趋势
--   · 每账户已用功能数 9~40（均值 27.6），不存在"浅层→深度"的自然演化
-- 因此本文件**只定义截面与结构类指标**（收入、功能、服务、流失），
-- 漏斗 / 激活 / 同期群留存一律不在此定义——它们属于模拟数据（T2）的职责，
-- 见后续 sql/04_ai_lifecycle_simulated.sql。
-- 依据：docs/data_quality_report.md Q-23、docs/data_capability_assessment.md
--
-- 口径基准（与 data_quality_report 的 D-1 ~ D-7 一致）：
--   统计主体   = account
--   收入口径   = 账户-月快照 + 胜出订阅规则
--   流失主口径 = churn_events 去重账户
--   有效任务   = error_count = 0（代理口径）
-- ============================================================================

USE ai_growth_ops;

-- ---------------------------------------------------------------------------
-- 0. 月份维度
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_month;
CREATE TABLE dim_month (
  month_start DATE NOT NULL,
  month_end   DATE NOT NULL,
  PRIMARY KEY (month_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO dim_month (month_start, month_end)
WITH RECURSIVE m AS (
  SELECT DATE('2023-01-01') AS d
  UNION ALL
  SELECT DATE_ADD(d, INTERVAL 1 MONTH) FROM m WHERE d < '2024-12-01'
)
SELECT d, LAST_DAY(d) FROM m;

-- ---------------------------------------------------------------------------
-- 1. v_subscription_month：订阅打散到账户-月并排名
-- 粒度：账户 × 月 × 订阅
-- 必要性：5,257 个账户-月中 4,695 组（89.3%）有多条订阅同时生效
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_subscription_month;
CREATE VIEW v_subscription_month AS
SELECT
  s.account_id,
  m.month_start,
  s.subscription_id,
  s.plan_tier,
  s.seats,
  s.mrr_amount,
  s.start_date,
  s.end_date,
  s.upgrade_flag,
  s.downgrade_flag,
  s.billing_frequency,
  ROW_NUMBER() OVER (
    PARTITION BY s.account_id, m.month_start
    ORDER BY s.start_date DESC, s.mrr_amount DESC, s.subscription_id DESC
  ) AS rn
FROM raw_subscriptions s
JOIN dim_month m
  ON s.start_date <= m.month_end
 AND (s.end_date IS NULL OR s.end_date >= m.month_start);

-- ---------------------------------------------------------------------------
-- 2. v_account_month：账户-月快照（500 × 24 = 12,000 行）
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_account_month;
CREATE VIEW v_account_month AS
SELECT
  a.account_id,
  m.month_start,
  w.subscription_id,
  w.plan_tier,
  w.seats,
  COALESCE(w.mrr_amount, 0) AS mrr_amount,
  COALESCE(w.upgrade_flag, 0)   AS upgrade_flag,
  COALESCE(w.downgrade_flag, 0) AS downgrade_flag,
  w.billing_frequency,
  CASE WHEN w.subscription_id IS NULL THEN 0 ELSE 1 END AS has_active_sub
FROM raw_accounts a
CROSS JOIN dim_month m
LEFT JOIN v_subscription_month w
  ON w.account_id = a.account_id
 AND w.month_start = m.month_start
 AND w.rn = 1;

-- ---------------------------------------------------------------------------
-- 3. v_usage_account：使用明细补 account_id
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_usage_account;
CREATE VIEW v_usage_account AS
SELECT
  u.row_id,
  u.usage_id,
  u.usage_date,
  u.feature_name,
  u.usage_count,
  u.usage_duration_secs,
  u.error_count,
  u.is_beta_feature,
  s.account_id
FROM raw_feature_usage u
JOIN raw_subscriptions s ON s.subscription_id = u.subscription_id;

-- ---------------------------------------------------------------------------
-- 4. v_account_feature_span：账户级使用画像
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_account_feature_span;
CREATE VIEW v_account_feature_span AS
SELECT
  account_id,
  MIN(usage_date)                                    AS first_use_date,
  MAX(usage_date)                                    AS last_use_date,
  COUNT(*)                                           AS usage_records,
  COUNT(DISTINCT feature_name)                       AS features_used,
  SUM(usage_count)                                   AS total_usage_count,
  SUM(CASE WHEN error_count = 0 THEN 1 ELSE 0 END)   AS ok_records,
  SUM(CASE WHEN error_count > 0 THEN 1 ELSE 0 END)   AS error_records,
  ROUND(SUM(CASE WHEN error_count > 0 THEN 1 ELSE 0 END) / COUNT(*), 4) AS error_rate,
  ROUND(AVG(usage_duration_secs), 1)                 AS avg_duration_secs
FROM v_usage_account
GROUP BY account_id;

-- ---------------------------------------------------------------------------
-- 5. v_account_profile：一账户一行，分层的唯一输入
--
-- 这一层刻意做成"宽表"，因为分层规则需要同时看收入、使用、服务三类信号；
-- 让每个分层条件都去 join 五张表，既慢又容易口径不一致。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_account_profile;
CREATE VIEW v_account_profile AS
SELECT
  a.account_id,
  a.industry,
  a.country,
  a.referral_source,
  a.signup_date,
  a.seats                       AS signup_seats,
  a.churn_flag,
  COALESCE(f.features_used, 0)  AS features_used,
  COALESCE(f.usage_records, 0)  AS usage_records,
  COALESCE(f.ok_records, 0)     AS ok_records,
  COALESCE(f.error_records, 0)  AS error_records,
  COALESCE(f.error_rate, 0)     AS error_rate,
  f.first_use_date,
  f.last_use_date,
  COALESCE(s.ticket_cnt, 0)     AS ticket_cnt,
  COALESCE(s.escalation_cnt, 0) AS escalation_cnt,
  s.avg_csat,
  s.avg_resolution_hours,
  COALESCE(r.months_paying, 0)  AS months_paying,
  COALESCE(r.mrr_latest, 0)     AS mrr_latest,
  COALESCE(r.peak_mrr, 0)       AS peak_mrr,
  CASE WHEN c.account_id IS NULL THEN 0 ELSE 1 END AS has_churn_event,
  COALESCE(c.churn_events, 0)   AS churn_events
FROM raw_accounts a
LEFT JOIN v_account_feature_span f ON f.account_id = a.account_id
LEFT JOIN (
  SELECT
    account_id,
    COUNT(*)                                        AS ticket_cnt,
    SUM(CASE WHEN escalation_flag = 1 THEN 1 ELSE 0 END) AS escalation_cnt,
    ROUND(AVG(satisfaction_score), 2)               AS avg_csat,
    ROUND(AVG(resolution_time_hours), 2)            AS avg_resolution_hours
  FROM raw_support_tickets
  GROUP BY account_id
) s ON s.account_id = a.account_id
LEFT JOIN (
  SELECT
    account_id,
    SUM(has_active_sub)                                        AS months_paying,
    MAX(mrr_amount)                                            AS peak_mrr,
    MAX(CASE WHEN month_start = '2024-12-01' THEN mrr_amount END) AS mrr_latest
  FROM v_account_month
  GROUP BY account_id
) r ON r.account_id = a.account_id
LEFT JOIN (
  SELECT account_id, COUNT(*) AS churn_events
  FROM raw_churn_events
  GROUP BY account_id
) c ON c.account_id = a.account_id;

-- ---------------------------------------------------------------------------
-- 6. v_kpi_monthly：月度经营总览
-- 说明：active_accounts 是**当月有使用**的账户数，不是付费账户数
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_monthly;
CREATE VIEW v_kpi_monthly AS
SELECT
  m.month_start,
  COALESCE(act.active_accounts, 0) AS active_accounts,
  COALESCE(pay.paying_accounts, 0) AS paying_accounts,
  COALESCE(pay.mrr_total, 0)       AS mrr_total,
  ROUND(COALESCE(pay.mrr_total, 0) / NULLIF(pay.paying_accounts, 0), 2) AS arpa,
  COALESCE(up.upgrades, 0)         AS upgrades,
  COALESCE(dn.downgrades, 0)       AS downgrades
FROM dim_month m
LEFT JOIN (
  SELECT DATE_FORMAT(usage_date, '%Y-%m-01') AS mon,
         COUNT(DISTINCT account_id) AS active_accounts
  FROM v_usage_account GROUP BY mon
) act ON act.mon = m.month_start
LEFT JOIN (
  SELECT month_start,
         SUM(CASE WHEN has_active_sub = 1 THEN 1 ELSE 0 END) AS paying_accounts,
         SUM(mrr_amount) AS mrr_total
  FROM v_account_month GROUP BY month_start
) pay ON pay.month_start = m.month_start
LEFT JOIN (
  SELECT month_start, COUNT(*) AS upgrades FROM v_account_month
  WHERE upgrade_flag = 1 GROUP BY month_start
) up ON up.month_start = m.month_start
LEFT JOIN (
  SELECT month_start, COUNT(*) AS downgrades FROM v_account_month
  WHERE downgrade_flag = 1 GROUP BY month_start
) dn ON dn.month_start = m.month_start;

-- ---------------------------------------------------------------------------
-- 7. v_kpi_feature_month：功能渗透（月度）
-- 分母 = 当月活跃账户数；用全部账户做分母会把渗透率稀释成无意义的小数
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_feature_month;
CREATE VIEW v_kpi_feature_month AS
SELECT
  DATE_FORMAT(u.usage_date, '%Y-%m-01') AS month_start,
  u.feature_name,
  COUNT(DISTINCT u.account_id)          AS feature_users,
  active.active_accounts,
  ROUND(COUNT(DISTINCT u.account_id) / NULLIF(active.active_accounts, 0), 4) AS penetration
FROM v_usage_account u
JOIN (
  SELECT DATE_FORMAT(usage_date, '%Y-%m-01') AS mon,
         COUNT(DISTINCT account_id) AS active_accounts
  FROM v_usage_account GROUP BY mon
) active ON active.mon = DATE_FORMAT(u.usage_date, '%Y-%m-01')
GROUP BY month_start, u.feature_name, active.active_accounts;

-- ---------------------------------------------------------------------------
-- 8. v_kpi_feature_summary：功能整体画像（全期）
-- 用途：找出"高频高错"的功能——这是运营最该先修的地方
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_feature_summary;
CREATE VIEW v_kpi_feature_summary AS
SELECT
  feature_name,
  COUNT(DISTINCT account_id)                    AS user_accounts,
  COUNT(*)                                      AS records,
  SUM(usage_count)                              AS total_usage_count,
  ROUND(AVG(usage_count), 2)                    AS avg_usage_count,
  ROUND(AVG(usage_duration_secs), 1)            AS avg_duration_secs,
  ROUND(SUM(CASE WHEN error_count > 0 THEN 1 ELSE 0 END) / COUNT(*), 4) AS error_rate,
  SUM(is_beta_feature = 1)                      AS beta_records
FROM v_usage_account
GROUP BY feature_name;

-- ---------------------------------------------------------------------------
-- 9. v_kpi_plan_mix：套餐结构（月度）
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_plan_mix;
CREATE VIEW v_kpi_plan_mix AS
SELECT
  month_start,
  plan_tier,
  COUNT(*)        AS accounts,
  SUM(mrr_amount) AS mrr
FROM v_account_month
WHERE has_active_sub = 1
GROUP BY month_start, plan_tier;

-- ---------------------------------------------------------------------------
-- 10. v_kpi_support：服务负担（月度）
-- CSAT 分母只用非空评分（825/2,000 为空，占 41.25%），不得按 0 填充
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_support;
CREATE VIEW v_kpi_support AS
SELECT
  DATE_FORMAT(submitted_at, '%Y-%m-01')            AS month_start,
  COUNT(*)                                         AS tickets,
  COUNT(DISTINCT account_id)                       AS accounts_with_ticket,
  ROUND(AVG(first_response_time_minutes), 1)       AS avg_first_response_min,
  ROUND(AVG(resolution_time_hours), 2)             AS avg_resolution_hours,
  SUM(CASE WHEN escalation_flag = 1 THEN 1 ELSE 0 END) AS escalations,
  SUM(CASE WHEN satisfaction_score IS NOT NULL THEN 1 ELSE 0 END) AS csat_n,
  ROUND(AVG(satisfaction_score), 2)                AS avg_csat
FROM raw_support_tickets
GROUP BY month_start;

-- ---------------------------------------------------------------------------
-- 11. v_kpi_churn_reason：流失结构（按原因）
-- 主口径：排除复活事件（is_reactivation=1），避免同一账户被重复计为流失
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_churn_reason;
CREATE VIEW v_kpi_churn_reason AS
SELECT
  reason_code,
  COUNT(*)                                             AS events,
  COUNT(DISTINCT account_id)                           AS accounts,
  ROUND(SUM(refund_amount_usd), 2)                     AS refund_total_usd,
  SUM(CASE WHEN refund_amount_usd > 0 THEN 1 ELSE 0 END) AS refund_events,
  SUM(CASE WHEN preceding_downgrade_flag = 1 THEN 1 ELSE 0 END) AS prior_downgrade,
  SUM(CASE WHEN preceding_upgrade_flag = 1 THEN 1 ELSE 0 END)   AS prior_upgrade
FROM raw_churn_events
WHERE is_reactivation = 0
GROUP BY reason_code;

-- ---------------------------------------------------------------------------
-- 12. v_kpi_churn：流失口径对照（三种算法并列，强制暴露差异）
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_churn;
CREATE VIEW v_kpi_churn AS
SELECT
  (SELECT COUNT(DISTINCT account_id) FROM raw_churn_events)                     AS churned_by_events,
  (SELECT COUNT(DISTINCT account_id) FROM raw_churn_events WHERE is_reactivation = 0) AS churned_net,
  (SELECT COUNT(*) FROM raw_accounts WHERE churn_flag = 1)                      AS churned_by_flag,
  (SELECT COUNT(*) FROM raw_churn_events WHERE is_reactivation = 1)             AS reactivation_events,
  (SELECT COUNT(*) FROM raw_accounts)                                           AS total_accounts,
  ROUND((SELECT COUNT(DISTINCT account_id) FROM raw_churn_events WHERE is_reactivation = 0)
        / (SELECT COUNT(*) FROM raw_accounts), 4)                               AS churn_rate_net,
  ROUND((SELECT COUNT(*) FROM raw_accounts WHERE churn_flag = 1)
        / (SELECT COUNT(*) FROM raw_accounts), 4)                               AS churn_rate_flag;
