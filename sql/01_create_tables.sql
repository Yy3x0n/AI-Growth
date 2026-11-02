-- ============================================================================
-- 01_create_tables.sql
-- 建表脚本（MySQL 8.4）
--
-- 设计要点：
-- 1. 两级装载：先落 raw_*_stg（全部 VARCHAR），再 INSERT ... SELECT 转成强类型表。
--    原因：CSV 里的布尔值是 "True"/"False" 字符串、空串代表 NULL，
--    直接 LOAD 进 TINYINT/DOUBLE 会报错或被静默转成 0，属于典型的静默数据损失。
-- 2. feature_usage 用 row_id 作为物理主键，usage_id 只建普通索引。
--    原因：实测有 21 行 usage_id 重复（其余字段不同），若把 usage_id 设为主键
--    会导致装载失败或需要丢弃真实事件。见 docs/data_quality_report.md Q-05。
-- 3. 原始表不加任何业务约束（外键），保持"原始"语义；
--    口径约束放在视图层（02_kpi_analysis.sql），便于追溯。
-- 4. 字符集统一 utf8mb4，避免 feedback_text 里的中文/emoji 截断。
-- ============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS ai_growth_ops
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE ai_growth_ops;

-- ---------------------------------------------------------------------------
-- 0. 清理（可重复执行）
-- 视图依赖表，必须先删视图
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_kpi_activation_funnel;
DROP VIEW IF EXISTS v_kpi_feature_adoption;
DROP VIEW IF EXISTS v_account_feature_span;
DROP VIEW IF EXISTS v_kpi_monthly_active;
DROP VIEW IF EXISTS v_account_month;

DROP TABLE IF EXISTS raw_accounts_stg;
DROP TABLE IF EXISTS raw_subscriptions_stg;
DROP TABLE IF EXISTS raw_feature_usage_stg;
DROP TABLE IF EXISTS raw_support_tickets_stg;
DROP TABLE IF EXISTS raw_churn_events_stg;

DROP TABLE IF EXISTS raw_accounts;
DROP TABLE IF EXISTS raw_subscriptions;
DROP TABLE IF EXISTS raw_feature_usage;
DROP TABLE IF EXISTS raw_support_tickets;
DROP TABLE IF EXISTS raw_churn_events;

-- ---------------------------------------------------------------------------
-- 1. 暂存表（全部 VARCHAR，承接到强类型表的桥梁）
-- ---------------------------------------------------------------------------
CREATE TABLE raw_accounts_stg (
  account_id       VARCHAR(32),
  account_name     VARCHAR(128),
  industry         VARCHAR(64),
  country          VARCHAR(16),
  signup_date      VARCHAR(16),
  referral_source  VARCHAR(32),
  plan_tier        VARCHAR(32),
  seats            VARCHAR(16),
  is_trial         VARCHAR(16),
  churn_flag       VARCHAR(16)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_subscriptions_stg (
  subscription_id    VARCHAR(32),
  account_id         VARCHAR(32),
  start_date         VARCHAR(16),
  end_date           VARCHAR(16),
  plan_tier          VARCHAR(32),
  seats              VARCHAR(16),
  mrr_amount         VARCHAR(16),
  arr_amount         VARCHAR(16),
  is_trial           VARCHAR(16),
  upgrade_flag       VARCHAR(16),
  downgrade_flag     VARCHAR(16),
  churn_flag         VARCHAR(16),
  billing_frequency  VARCHAR(32),
  auto_renew_flag    VARCHAR(16)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_feature_usage_stg (
  usage_id             VARCHAR(32),
  subscription_id      VARCHAR(32),
  usage_date           VARCHAR(16),
  feature_name         VARCHAR(64),
  usage_count          VARCHAR(16),
  usage_duration_secs  VARCHAR(16),
  error_count          VARCHAR(16),
  is_beta_feature      VARCHAR(16)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_support_tickets_stg (
  ticket_id                    VARCHAR(32),
  account_id                   VARCHAR(32),
  submitted_at                 VARCHAR(32),
  closed_at                    VARCHAR(32),
  resolution_time_hours        VARCHAR(32),
  priority                     VARCHAR(16),
  first_response_time_minutes  VARCHAR(16),
  satisfaction_score           VARCHAR(16),
  escalation_flag              VARCHAR(16)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_churn_events_stg (
  churn_event_id            VARCHAR(32),
  account_id                VARCHAR(32),
  churn_date                VARCHAR(16),
  reason_code               VARCHAR(32),
  refund_amount_usd         VARCHAR(16),
  preceding_upgrade_flag    VARCHAR(16),
  preceding_downgrade_flag  VARCHAR(16),
  is_reactivation           VARCHAR(16),
  feedback_text             TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. 强类型原始表
-- ---------------------------------------------------------------------------
CREATE TABLE raw_accounts (
  account_id       VARCHAR(32)  NOT NULL,
  account_name     VARCHAR(128),
  industry         VARCHAR(64),
  country          VARCHAR(16),
  signup_date      DATE,
  referral_source  VARCHAR(32),
  plan_tier        VARCHAR(32),
  seats            INT,
  is_trial         TINYINT(1),
  churn_flag       TINYINT(1),
  PRIMARY KEY (account_id),
  KEY idx_accounts_signup (signup_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_subscriptions (
  subscription_id    VARCHAR(32) NOT NULL,
  account_id         VARCHAR(32) NOT NULL,
  start_date         DATE,
  end_date           DATE NULL,
  plan_tier          VARCHAR(32),
  seats              INT,
  mrr_amount         INT,
  arr_amount         INT,
  is_trial           TINYINT(1),
  upgrade_flag       TINYINT(1),
  downgrade_flag     TINYINT(1),
  churn_flag         TINYINT(1),
  billing_frequency  VARCHAR(32),
  auto_renew_flag    TINYINT(1),
  PRIMARY KEY (subscription_id),
  KEY idx_subs_account (account_id),
  KEY idx_subs_start (start_date),
  KEY idx_subs_account_start (account_id, start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- row_id 为物理主键；usage_id 允许重复（实测 21 行），只建普通索引
CREATE TABLE raw_feature_usage (
  row_id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  usage_id             VARCHAR(32),
  subscription_id      VARCHAR(32) NOT NULL,
  usage_date           DATE,
  feature_name         VARCHAR(64),
  usage_count          INT,
  usage_duration_secs  INT,
  error_count          INT,
  is_beta_feature      TINYINT(1),
  PRIMARY KEY (row_id),
  KEY idx_usage_usage_id (usage_id),
  KEY idx_usage_sub (subscription_id),
  KEY idx_usage_date (usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_support_tickets (
  ticket_id                    VARCHAR(32) NOT NULL,
  account_id                   VARCHAR(32) NOT NULL,
  submitted_at                 DATETIME,
  closed_at                    DATETIME NULL,
  resolution_time_hours        DOUBLE NULL,
  priority                     VARCHAR(16),
  first_response_time_minutes  INT,
  satisfaction_score           DOUBLE NULL,
  escalation_flag              TINYINT(1),
  PRIMARY KEY (ticket_id),
  KEY idx_tickets_account (account_id),
  KEY idx_tickets_submitted (submitted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE raw_churn_events (
  churn_event_id            VARCHAR(32) NOT NULL,
  account_id                VARCHAR(32) NOT NULL,
  churn_date                DATE,
  reason_code               VARCHAR(32),
  refund_amount_usd         DOUBLE,
  preceding_upgrade_flag    TINYINT(1),
  preceding_downgrade_flag  TINYINT(1),
  is_reactivation           TINYINT(1),
  feedback_text             TEXT,
  PRIMARY KEY (churn_event_id),
  KEY idx_churn_account (account_id),
  KEY idx_churn_date (churn_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
