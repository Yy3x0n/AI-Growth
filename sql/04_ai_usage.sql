-- ============================================================================
-- 04_ai_usage.sql
-- T2 模拟 AI 请求日志：建表 + 成本假设 + 指标视图
--
-- 数据性质声明：本文件处理的是**模拟数据**（data_source='simulated'），
-- 不得与 T1 真实数据混算。成本为按公开单价快照的估算值，不是真实账单。
--
-- 三个必须在 SQL 里解决的技术约束：
--   1. MySQL 视图定义中不允许 WITH（CTE），所以 p95 必须拆成"排名视图 + 聚合视图"两步；
--   2. MySQL 没有 PERCENTILE_CONT，p95 统一用最近秩法（rank = CEIL(0.95n)），
--      与 src/ai_usage.py 的实现严格一致，否则 SQL/Pandas 对账会长期挂着 1~2 的差额；
--   3. 计费单价与"有效任务"阈值不写死在视图里，统一存配置表，
--      避免重演阶段 2 分层阈值在 SQL 与 Python 之间漂移的问题。
-- ============================================================================

USE ai_growth_ops;

-- ---------------------------------------------------------------------------
-- 1. 明细表
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS raw_ai_request_logs;
CREATE TABLE raw_ai_request_logs (
  request_id    VARCHAR(16)  NOT NULL,
  account_id    VARCHAR(16)  NOT NULL,
  request_time  DATETIME     NOT NULL,
  feature_name  VARCHAR(32)  NOT NULL,
  status        VARCHAR(16)  NOT NULL,
  error_type    VARCHAR(32)  NULL,
  latency_ms    INT          NOT NULL,
  input_tokens  INT          NOT NULL,
  output_tokens INT          NOT NULL,
  data_source   VARCHAR(16)  NOT NULL,
  PRIMARY KEY (request_id),
  KEY idx_ai_time (request_time),
  KEY idx_ai_account (account_id),
  KEY idx_ai_feature (feature_name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = 'T2 模拟 AI 请求日志（非真实服务数据）';

-- ---------------------------------------------------------------------------
-- 2. 配置表：成本单价与指标阈值（唯一来源）
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS cost_assumption;
CREATE TABLE cost_assumption (
  name             VARCHAR(40)   NOT NULL,
  usd_per_million  DECIMAL(12,6) NOT NULL,
  snapshot_date    DATE          NOT NULL,
  note             VARCHAR(200)  NOT NULL,
  PRIMARY KEY (name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '估算成本用的单价快照，由 scripts/seed_config_tables.py 从 src/config.py 写入';

INSERT INTO cost_assumption (name, usd_per_million, snapshot_date, note) VALUES
  ('input_cache_miss', 0.150000, '2026-09-18', '输入 token 未命中缓存的公开单价（美元/百万）'),
  ('output',           0.600000, '2026-09-18', '输出 token 的公开单价（美元/百万）')
ON DUPLICATE KEY UPDATE usd_per_million = VALUES(usd_per_million);

DROP TABLE IF EXISTS metric_config;
CREATE TABLE metric_config (
  name   VARCHAR(60)   NOT NULL,
  value  DECIMAL(12,4) NOT NULL,
  note   VARCHAR(200)  NOT NULL,
  PRIMARY KEY (name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4
  COMMENT = '指标阈值配置，避免在 SQL 与 Python 各写一份';

INSERT INTO metric_config (name, value, note) VALUES
  ('effective_task_min_output_tokens', 50, '「有效任务」的最低输出 token 数：成功且输出≥该值')
ON DUPLICATE KEY UPDATE value = VALUES(value);

-- ---------------------------------------------------------------------------
-- 3. 派生视图
-- ---------------------------------------------------------------------------

-- 单条请求的成本与有效任务标记
DROP VIEW IF EXISTS v_ai_request_cost;
CREATE VIEW v_ai_request_cost AS
SELECT
  l.request_id,
  l.account_id,
  l.request_time,
  DATE_FORMAT(l.request_time, '%Y-%m-01') AS month_start,
  l.feature_name,
  l.status,
  l.error_type,
  l.latency_ms,
  l.input_tokens,
  l.output_tokens,
  l.data_source,
  (l.status = 'success'
     AND l.output_tokens >= (SELECT value FROM metric_config
                             WHERE name = 'effective_task_min_output_tokens')) AS is_effective_task,
  l.input_tokens  / 1000000 * (SELECT usd_per_million FROM cost_assumption
                               WHERE name = 'input_cache_miss')
  + l.output_tokens / 1000000 * (SELECT usd_per_million FROM cost_assumption
                                 WHERE name = 'output') AS estimated_cost_usd_at_assumed_price
FROM raw_ai_request_logs l;

-- P95 的中间层：按月排名。视图里不能用 CTE，只能用窗口函数后二次聚合。
DROP VIEW IF EXISTS v_ai_latency_rank_month;
CREATE VIEW v_ai_latency_rank_month AS
SELECT
  DATE_FORMAT(request_time, '%Y-%m-01') AS month_start,
  latency_ms,
  ROW_NUMBER() OVER (PARTITION BY DATE_FORMAT(request_time, '%Y-%m-01')
                     ORDER BY latency_ms) AS rn,
  COUNT(*) OVER (PARTITION BY DATE_FORMAT(request_time, '%Y-%m-01')) AS n
FROM raw_ai_request_logs;

DROP VIEW IF EXISTS v_ai_latency_rank_feature;
CREATE VIEW v_ai_latency_rank_feature AS
SELECT
  feature_name,
  latency_ms,
  ROW_NUMBER() OVER (PARTITION BY feature_name ORDER BY latency_ms) AS rn,
  COUNT(*) OVER (PARTITION BY feature_name) AS n
FROM raw_ai_request_logs;

DROP VIEW IF EXISTS v_ai_monthly;
CREATE VIEW v_ai_monthly AS
SELECT
  c.month_start,
  COUNT(*)                                                  AS requests,
  COUNT(DISTINCT c.account_id)                              AS active_accounts,
  SUM(c.status = 'success')                                 AS success_requests,
  -- 必须 CAST 成 DOUBLE：MySQL 的 DECIMAL 除法受 div_precision_increment 控制
  -- （默认 4），SUM(...)/COUNT(*) 只保留 4 位小数，外层的 ROUND(...,6) 拿不到第 5、6 位。
  -- 不转类型的结果是"声明 6 位、实际 4 位"，且与 Pandas 侧长期差 1e-5。
  ROUND(CAST(SUM(c.status = 'success') AS DOUBLE) / COUNT(*), 6) AS success_rate,
  ROUND(AVG(c.latency_ms), 1)                               AS avg_latency_ms,
  (SELECT MAX(p.latency_ms) FROM v_ai_latency_rank_month p
    WHERE p.month_start = c.month_start
      AND p.rn = CEIL(0.95 * p.n))                          AS p95_latency_ms,
  SUM(c.input_tokens)                                       AS input_tokens,
  SUM(c.output_tokens)                                      AS output_tokens,
  SUM(c.is_effective_task)                                  AS effective_tasks,
  ROUND(SUM(c.estimated_cost_usd_at_assumed_price), 4)      AS estimated_cost_usd_at_assumed_price
FROM v_ai_request_cost c
GROUP BY c.month_start;

DROP VIEW IF EXISTS v_ai_feature;
CREATE VIEW v_ai_feature AS
SELECT
  c.feature_name,
  COUNT(*)                                                  AS requests,
  COUNT(DISTINCT c.account_id)                              AS accounts,
  ROUND(CAST(SUM(c.status = 'success') AS DOUBLE) / COUNT(*), 6) AS success_rate,
  ROUND(AVG(c.latency_ms), 1)                               AS avg_latency_ms,
  (SELECT MAX(p.latency_ms) FROM v_ai_latency_rank_feature p
    WHERE p.feature_name = c.feature_name
      AND p.rn = CEIL(0.95 * p.n))                          AS p95_latency_ms,
  ROUND(AVG(c.input_tokens), 1)                             AS avg_input_tokens,
  ROUND(AVG(c.output_tokens), 1)                            AS avg_output_tokens,
  ROUND(SUM(c.estimated_cost_usd_at_assumed_price), 4)      AS estimated_cost_usd_at_assumed_price
FROM v_ai_request_cost c
GROUP BY c.feature_name;

DROP VIEW IF EXISTS v_ai_error_types;
CREATE VIEW v_ai_error_types AS
SELECT
  month_start,
  feature_name,
  error_type,
  COUNT(*) AS requests
FROM v_ai_request_cost
WHERE status = 'error'
GROUP BY month_start, feature_name, error_type;

-- 账户-月粒度：用于使用深度分层与集中度分析
DROP VIEW IF EXISTS v_ai_account_month;
CREATE VIEW v_ai_account_month AS
SELECT
  account_id,
  month_start,
  COUNT(*)                                              AS requests,
  ROUND(CAST(SUM(status = 'success') AS DOUBLE) / COUNT(*), 6) AS success_rate,
  SUM(is_effective_task)                                AS effective_tasks,
  ROUND(SUM(estimated_cost_usd_at_assumed_price), 4)    AS estimated_cost_usd_at_assumed_price
FROM v_ai_request_cost
GROUP BY account_id, month_start;
