-- ============================================================================
-- 01b_load_from_staging.sql
-- 暂存表 → 强类型表
--
-- 单独成文件的原因：LOAD DATA LOCAL INFILE 必须由客户端发起（见 scripts/load_to_mysql.py），
-- 因此装载流程是三个阶段：
--   ① sql/01_create_tables.sql      建表
--   ② LOAD DATA → raw_*_stg         客户端灌入原始字符串
--   ③ sql/01b_load_from_staging.sql 转成强类型并清空暂存
-- 若把三段写在一个文件里，重跑时会在空暂存表上做转换，得到 0 行而不报错。
-- ============================================================================

USE ai_growth_ops;

-- ---------------------------------------------------------------------------
-- 3. 暂存 → 强类型（空串转 NULL，True/False 转 1/0）
-- 注意：MySQL 8 严格模式下，空串写进 DATE/DOUBLE 会报错，所以必须 NULLIF。
-- ---------------------------------------------------------------------------
INSERT INTO raw_accounts
SELECT
  account_id, account_name, industry, country,
  NULLIF(signup_date, ''), referral_source, plan_tier,
  NULLIF(seats, ''),
  CASE is_trial   WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE churn_flag WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END
FROM raw_accounts_stg;

INSERT INTO raw_subscriptions
SELECT
  subscription_id, account_id,
  NULLIF(start_date, ''), NULLIF(end_date, ''),
  plan_tier, NULLIF(seats, ''), NULLIF(mrr_amount, ''), NULLIF(arr_amount, ''),
  CASE is_trial       WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE upgrade_flag   WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE downgrade_flag WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE churn_flag     WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  billing_frequency,
  CASE auto_renew_flag WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END
FROM raw_subscriptions_stg;

INSERT INTO raw_feature_usage
  (usage_id, subscription_id, usage_date, feature_name,
   usage_count, usage_duration_secs, error_count, is_beta_feature)
SELECT
  usage_id, subscription_id, NULLIF(usage_date, ''), feature_name,
  NULLIF(usage_count, ''), NULLIF(usage_duration_secs, ''), NULLIF(error_count, ''),
  CASE is_beta_feature WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END
FROM raw_feature_usage_stg;

INSERT INTO raw_support_tickets
SELECT
  ticket_id, account_id,
  NULLIF(submitted_at, ''), NULLIF(closed_at, ''),
  NULLIF(resolution_time_hours, ''), priority, NULLIF(first_response_time_minutes, ''),
  NULLIF(satisfaction_score, ''),
  CASE escalation_flag WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END
FROM raw_support_tickets_stg;

INSERT INTO raw_churn_events
SELECT
  churn_event_id, account_id, NULLIF(churn_date, ''), reason_code,
  NULLIF(refund_amount_usd, ''),
  CASE preceding_upgrade_flag   WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE preceding_downgrade_flag WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  CASE is_reactivation          WHEN 'True' THEN 1 WHEN 'False' THEN 0 ELSE NULL END,
  NULLIF(feedback_text, '')
FROM raw_churn_events_stg;

-- 装载完成后暂存表即可清空（保留表结构以便重跑）
TRUNCATE TABLE raw_accounts_stg;
TRUNCATE TABLE raw_subscriptions_stg;
TRUNCATE TABLE raw_feature_usage_stg;
TRUNCATE TABLE raw_support_tickets_stg;
TRUNCATE TABLE raw_churn_events_stg;
