-- ============================================================================
-- 03_customer_segments.sql
-- 客户分层
--
-- 分层原则：
-- 1. **互斥**：按优先级命中即停，六层账户数之和必须等于总账户数。
-- 2. **阈值来自实际分布**：全部取分位数或业务边界，记录在 segment_threshold 表中。
--    不写"行业标准"——本项目没有行业基准可引用。
-- 3. **每层对应一个可执行动作**，否则分层只是给数据贴标签。
--
-- 阈值不硬编码在 SQL 里，而是从 segment_threshold 表读取：
-- 硬编码过一次，Python 侧重算分位数后两边就对不上（实测差 3 个账户），
-- 且报错信息只会说"对账失败"，很难定位到底是哪条规则漂了。
-- 默认值由本文件写入；scripts/segment_accounts.py 会用实际计算值覆盖。
-- ============================================================================

USE ai_growth_ops;

-- ---------------------------------------------------------------------------
-- 0. 阈值配置表（单一数据源）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segment_threshold (
  name        VARCHAR(32)  NOT NULL,
  value       DOUBLE       NOT NULL,
  note        VARCHAR(128),
  updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 默认值：基于 2026-11-02 实测分布，实际运行时由脚本覆盖为精确值
INSERT INTO segment_threshold (name, value, note) VALUES
  ('mrr_p75',         3184.0, '账户末月 MRR 的 75 分位：高价值界限'),
  ('features_p25',    24.0,   '账户使用功能数的 25 分位：浅层采用界限'),
  ('features_p75',    32.0,   '账户使用功能数的 75 分位：深度采用界限'),
  ('error_rate_p90',  0.394,  '账户错误率的 90 分位：风险层取尾部，避免圈进 25%'),
  ('csat_low',        3.0,    '满意度最低档（实测取值 3~5）'),
  ('escalation_high', 2.0,    '升级工单异常线（实测最大 3）')
AS new
ON DUPLICATE KEY UPDATE value = new.value, note = new.note;

-- ---------------------------------------------------------------------------
-- 1. v_account_segment：一账户一行唯一分层
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_account_segment;
CREATE VIEW v_account_segment AS
SELECT
  p.account_id,
  p.industry,
  p.country,
  p.mrr_latest,
  p.features_used,
  p.usage_records,
  p.error_rate,
  p.ticket_cnt,
  p.escalation_cnt,
  p.avg_csat,
  p.has_churn_event,
  p.churn_flag,
  CASE
    -- ① 末月无有效订阅：比 churn_flag 更接近"实际停付"，优先归类
    WHEN p.mrr_latest = 0 THEN '已停付'
    -- ② 服务或质量亮红灯：升级工单、低满意度、高错误率任一命中
    WHEN p.escalation_cnt >= (SELECT value FROM segment_threshold WHERE name = 'escalation_high')
      OR (p.avg_csat IS NOT NULL
          AND p.avg_csat <= (SELECT value FROM segment_threshold WHERE name = 'csat_low'))
      OR p.error_rate >= (SELECT value FROM segment_threshold WHERE name = 'error_rate_p90')
      THEN '风险关注'
    -- ③ 收入贡献前 25%
    WHEN p.mrr_latest >= (SELECT value FROM segment_threshold WHERE name = 'mrr_p75') THEN '高价值'
    -- ④ 功能覆盖面前 25%
    WHEN p.features_used >= (SELECT value FROM segment_threshold WHERE name = 'features_p75') THEN '深度采用'
    -- ⑤ 功能覆盖面后 25%
    WHEN p.features_used <= (SELECT value FROM segment_threshold WHERE name = 'features_p25') THEN '浅层采用'
    ELSE '稳健基础'
  END AS segment
FROM v_account_profile p;

-- ---------------------------------------------------------------------------
-- 2. v_segment_summary：分层汇总
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_segment_summary;
CREATE VIEW v_segment_summary AS
SELECT
  segment,
  COUNT(*)                                   AS accounts,
  ROUND(COUNT(*) / (SELECT COUNT(*) FROM v_account_segment), 4) AS account_share,
  SUM(mrr_latest)                            AS mrr_total,
  ROUND(SUM(mrr_latest) / (SELECT NULLIF(SUM(mrr_latest), 0) FROM v_account_segment), 4) AS revenue_share,
  ROUND(AVG(mrr_latest), 2)                  AS avg_mrr,
  ROUND(AVG(features_used), 1)               AS avg_features,
  ROUND(AVG(usage_records), 1)               AS avg_usage_records,
  ROUND(AVG(ticket_cnt), 2)                  AS avg_tickets,
  SUM(escalation_cnt)                        AS escalations,
  ROUND(AVG(avg_csat), 2)                    AS avg_csat,
  ROUND(AVG(error_rate), 4)                  AS avg_error_rate,
  SUM(has_churn_event)                       AS with_churn_event,
  ROUND(SUM(has_churn_event) / COUNT(*), 4)  AS churn_event_rate
FROM v_account_segment
GROUP BY segment;

-- ---------------------------------------------------------------------------
-- 3. v_churn_leading_signals：流失前兆信号
-- 注意：别用 signal 作别名，SIGNAL 是 MySQL 保留字（SQLSTATE 用），会报 1064。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_churn_leading_signals;
CREATE VIEW v_churn_leading_signals AS
SELECT
  CASE WHEN e.preceding_downgrade_flag = 1 THEN '流失前先降级'
       WHEN e.preceding_upgrade_flag = 1   THEN '流失前先升级'
       ELSE '无前兆信号' END AS signal_type,
  COUNT(*)                           AS events,
  COUNT(DISTINCT e.account_id)       AS accounts,
  ROUND(AVG(e.refund_amount_usd), 2) AS avg_refund_usd
FROM raw_churn_events e
WHERE e.is_reactivation = 0
GROUP BY signal_type;
