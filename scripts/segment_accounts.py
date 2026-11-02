#!/usr/bin/env python3
"""阶段 2：客户分层 + 与 SQL 视图对账。

用法：
    python scripts/segment_accounts.py

前置：
    python scripts/run_analysis.py   # 生成 outputs/tables/account_profile.csv

产出：
    outputs/tables/account_segments.csv        一账户一行的分层结果
    outputs/tables/segment_summary.csv         分层汇总（规模/收入/使用/服务/流失）
    outputs/tables/segmentation_thresholds.csv 阈值及其来源，供文档引用
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pymysql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, segmentation  # noqa: E402

TABLE_DIR = PROJECT_ROOT / "outputs" / "tables"
PROFILE_CSV = TABLE_DIR / "account_profile.csv"

# 分层阈值两侧（Python 计算 / SQL 常量）必须完全一致，否则下游报告会自相矛盾
COMPARE_COLS = ["accounts", "mrr_total"]


def main() -> int:
    if "--sensitivity" in sys.argv:
        profile = pd.read_csv(PROFILE_CSV)
        table = segmentation.sensitivity_analysis(profile, segmentation.compute_thresholds(profile))
        print("阈值敏感性（关键阈值 ±20%）")
        print(table.to_string(index=False))
        return 0

    if not PROFILE_CSV.is_file():
        print(f"缺少 {PROFILE_CSV}，请先运行：python scripts/run_analysis.py", file=sys.stderr)
        return 1

    profile = pd.read_csv(PROFILE_CSV)
    thresholds = segmentation.compute_thresholds(profile)

    pd.DataFrame(
        [
            {"threshold": "mrr_p25", "value": thresholds["mrr_p25"], "source": "账户末月 MRR 分布的 25 分位"},
            {"threshold": "mrr_p50", "value": thresholds["mrr_p50"], "source": "账户末月 MRR 分布的 50 分位"},
            {"threshold": "mrr_p75", "value": thresholds["mrr_p75"], "source": "高价值分层界限：MRR 75 分位"},
            {"threshold": "features_p25", "value": thresholds["features_p25"], "source": "功能数 25 分位：浅层采用界限"},
            {"threshold": "features_p75", "value": thresholds["features_p75"], "source": "功能数 75 分位：深度采用界限"},
            {"threshold": "error_rate_p90", "value": thresholds["error_rate_p90"], "source": "错误率 90 分位：风险层取尾部，避免 P75 圈进 25% 变成垃圾桶层"},
            {"threshold": "csat_low", "value": thresholds["csat_low"], "source": "满意度最低档（实测取值 3~5）"},
            {"threshold": "escalation_high", "value": thresholds["escalation_high"], "source": "升级工单异常线（实测最大 3）"},
        ]
    ).to_csv(TABLE_DIR / "segmentation_thresholds.csv", index=False)

    segmented = segmentation.assign_segments(profile, thresholds)
    segmentation.validate(segmented)
    summary = segmentation.summarize_segments(segmented)

    segmented.to_csv(TABLE_DIR / "account_segments.csv", index=False)
    summary.to_csv(TABLE_DIR / "segment_summary.csv", index=False)

    print("=" * 92)
    print(f"客户分层（阈值：MRR P75={thresholds['mrr_p75']}｜功能数 P25={thresholds['features_p25']} "
          f"P75={thresholds['features_p75']}｜错误率 P90={thresholds['error_rate_p90']} "
          f"｜CSAT≤{thresholds['csat_low']}｜升级≥{thresholds['escalation_high']}）")
    print("=" * 92)
    display = summary[
        ["segment", "accounts", "account_share", "mrr_total", "revenue_share",
         "avg_mrr", "avg_features", "avg_tickets", "churn_event_rate"]
    ].copy()
    display["account_share"] = (display["account_share"] * 100).round(1).astype(str) + "%"
    display["revenue_share"] = (display["revenue_share"] * 100).round(1).astype(str) + "%"
    display["churn_event_rate"] = (display["churn_event_rate"] * 100).round(1).astype(str) + "%"
    print(display.to_string(index=False))
    print(f"\n合计账户 {len(segmented)}｜分层数 {segmented['segment'].nunique()}｜互斥校验通过")

    # ---- 把阈值写回数据库，让 SQL 视图与 Python 共用同一组数字 ----
    # 不做这一步的话，SQL 里的默认常量会与 Python 重算的分位数漂移，
    # 表现为"分层对账失败 3 个账户"这种很难定位的差异。
    conn = pymysql.connect(
        host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD, database=config.MYSQL_DATABASE, charset="utf8mb4",
        autocommit=True,
    )
    threshold_rows = [
        ("mrr_p75", thresholds["mrr_p75"], "账户末月 MRR 的 75 分位：高价值界限"),
        ("features_p25", float(thresholds["features_p25"]), "账户使用功能数的 25 分位：浅层采用界限"),
        ("features_p75", float(thresholds["features_p75"]), "账户使用功能数的 75 分位：深度采用界限"),
        ("error_rate_p90", thresholds["error_rate_p90"], "账户错误率的 90 分位：风险关注条件之一"),
        ("csat_low", thresholds["csat_low"], "满意度最低档"),
        ("escalation_high", float(thresholds["escalation_high"]), "升级工单异常线"),
    ]
    try:
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO segment_threshold (name, value, note) VALUES (%s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE value = new.value, note = new.note",
                threshold_rows,
            )
            cursor.execute("SELECT segment, accounts, mrr_total FROM v_segment_summary")
            sql_summary = pd.DataFrame(cursor.fetchall(), columns=["segment", "accounts", "mrr_total"])
    finally:
        conn.close()

    merged = summary[["segment", "accounts", "mrr_total"]].merge(
        sql_summary, on="segment", suffixes=("_py", "_sql"), how="outer"
    )
    print()
    print("=" * 92)
    print("分层结果 SQL / Pandas 对账")
    print("=" * 92)
    ok = True
    for col in COMPARE_COLS:
        left = merged[f"{col}_py"].fillna(-1).astype(float)
        right = merged[f"{col}_sql"].fillna(-1).astype(float)
        diff = (left - right).abs()
        bad = int((diff > 1e-6).sum())
        ok = ok and bad == 0
        print(f"  {col:12s} 比对 {len(merged)} 行，不一致 {bad} 行，最大差 {diff.max():.0f}")
    if not ok:
        print("\n分层口径不一致：最可能的原因是阈值常量与 Python 计算值不同步，"
              "请检查 sql/03_customer_segments.sql 的硬编码阈值。")
        return 1

    print("\n对账通过。产物：account_segments.csv / segment_summary.csv / segmentation_thresholds.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
