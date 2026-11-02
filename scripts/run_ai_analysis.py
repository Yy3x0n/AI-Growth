#!/usr/bin/env python3
"""阶段 3：AI 使用分析（模块 E），并用 SQL 视图交叉验证。

用法：
    python scripts/run_ai_analysis.py
    python scripts/run_ai_analysis.py --no-reconcile   # 跳过 SQL 对账

产出：
    outputs/tables/ai_monthly_summary.csv       月度使用与成本
    outputs/tables/ai_feature_summary.csv       功能维度
    outputs/tables/ai_error_types.csv           错误类型分布
    outputs/tables/ai_account_month.csv         账户-月明细
    outputs/tables/ai_usage_concentration.csv   请求集中度
    outputs/tables/ai_incident_detection.csv    异常日检测
    outputs/tables/ai_sql_vs_pandas.csv         逐指标对账结果

口径提醒：所有数字来自**模拟数据**。成本是按公开单价快照的估算，
不是真实账单；不得写成"服务成本"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import ai_usage, config  # noqa: E402

DEFAULT_TOLERANCE = 1e-6


def tolerances_for(columns: list[str]) -> dict[str, float]:
    """按 SQL 侧的 ROUND 位数确定每列的比对容差。

    不做统一容差的原因：SQL 里 success_rate 保留 6 位、avg_latency 保留 1 位、
    cost 保留 4 位。用同一个 1e-6 会让"正确但被四舍五入过"的值全部报错，
    最后只能把容差放大到失去意义。
    """
    tol = {}
    for col in columns:
        if col in ("success_rate",):
            tol[col] = 5e-7
        elif col.endswith("_rate") or col in ("avg_input_tokens", "avg_output_tokens"):
            tol[col] = 5e-2
        elif col == "avg_latency_ms":
            tol[col] = 5e-2
        elif col.startswith("estimated_cost"):
            # SQL 与 Pandas 各自对结果 ROUND 到 4 位，两个独立的舍入最多差 1 个末位
            # （= 1e-4）。容差取 1.1e-4 才能容纳"两边都对"的情况。
            tol[col] = 1.1e-4
        elif col.startswith("p95") or col in (
            "requests", "accounts", "active_accounts", "success_requests",
            "input_tokens", "output_tokens", "effective_tasks",
        ):
            tol[col] = DEFAULT_TOLERANCE
        else:
            tol[col] = DEFAULT_TOLERANCE
    return tol


def compare(name: str, pandas_df: pd.DataFrame, sql_df: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    """逐格比对两张表，返回每个数值列的最大偏差。"""
    merged = pandas_df.merge(sql_df, on=key, suffixes=("_pd", "_sql"), how="outer", indicator=True)
    rows: list[dict] = []

    if (merged["_merge"] != "both").any():
        rows.append({
            "指标组": name, "列": "<行匹配>",
            "最大偏差": float("nan"), "容差": float("nan"),
            "行数_par": len(pandas_df), "行数_sql": len(sql_df), "结果": "行不匹配",
        })

    value_cols = [c for c in pandas_df.columns if c not in key]
    tol_map = tolerances_for(value_cols)
    for col in value_cols:
        left, right = merged.get(f"{col}_pd"), merged.get(f"{col}_sql")
        if left is None or right is None:
            continue
        diff = (left.astype(float) - right.astype(float)).abs()
        max_diff = float(np.nanmax(diff)) if len(diff) else 0.0
        tol = tol_map.get(col, DEFAULT_TOLERANCE)
        rows.append({
            "指标组": name, "列": col,
            "最大偏差": max_diff, "容差": tol,
            "行数_par": len(pandas_df), "行数_sql": len(sql_df),
            "结果": "一致" if max_diff <= tol else "不一致",
        })
    return pd.DataFrame(rows)


def fetch_sql_views() -> dict[str, pd.DataFrame]:
    from scripts.run_sql import connect

    queries = {
        "monthly": "SELECT * FROM v_ai_monthly ORDER BY month_start",
        "feature": "SELECT * FROM v_ai_feature ORDER BY feature_name",
        "error_types": (
            "SELECT month_start, feature_name, error_type, requests "
            "FROM v_ai_error_types ORDER BY month_start, feature_name, error_type"
        ),
        "account_month": (
            "SELECT account_id, month_start, requests, success_rate, effective_tasks, "
            "estimated_cost_usd_at_assumed_price FROM v_ai_account_month "
            "ORDER BY account_id, month_start"
        ),
    }
    conn = connect()
    try:
        out = {}
        with conn.cursor() as cursor:
            for name, sql in queries.items():
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description]
                out[name] = pd.DataFrame(cursor.fetchall(), columns=columns)
        return out
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 使用分析")
    parser.add_argument("--no-reconcile", action="store_true", help="跳过 SQL 对账")
    args = parser.parse_args()

    config.TABLE_DIR.mkdir(parents=True, exist_ok=True)

    print("载入模拟日志…")
    logs = ai_usage.load_logs()
    print(f"  {len(logs):,} 行 / {logs['account_id'].nunique()} 个账户 / "
          f"{logs['month_start'].nunique()} 个月")

    monthly = ai_usage.monthly_summary(logs)
    feature = ai_usage.feature_summary(logs)
    errors = ai_usage.error_type_summary(logs)
    acct_month = ai_usage.account_month(logs)
    concentration = ai_usage.usage_concentration(logs)
    incidents = ai_usage.incident_detection(logs)

    # ---- 落盘（先转日期，避免 CSV 里出现整数时间戳） ----
    for df, cols in ((monthly, ["month_start"]), (errors, ["month_start"]), (acct_month, ["month_start"])):
        for col in cols:
            df[col] = pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")

    monthly.to_csv(config.TABLE_DIR / "ai_monthly_summary.csv", index=False)
    feature.to_csv(config.TABLE_DIR / "ai_feature_summary.csv", index=False)
    errors.to_csv(config.TABLE_DIR / "ai_error_types.csv", index=False)
    acct_month.to_csv(config.TABLE_DIR / "ai_account_month.csv", index=False)
    concentration.to_csv(config.TABLE_DIR / "ai_usage_concentration.csv", index=False)
    incidents.to_csv(config.TABLE_DIR / "ai_incident_detection.csv", index=False)

    # ---- 交叉验证 ----
    reconcile_ok = True
    if not args.no_reconcile:
        print("\nSQL / Pandas 交叉验证")
        sql_views = fetch_sql_views()

        # SQL 返回的 month_start 已转成字符串，Pandas 侧同步处理后再比
        monthly_pd = monthly.copy()
        monthly_pd["month_start"] = pd.to_datetime(monthly_pd["month_start"])
        sql_monthly = sql_views["monthly"].copy()
        sql_monthly["month_start"] = pd.to_datetime(sql_monthly["month_start"])

        errors_pd = errors.copy()
        errors_pd["month_start"] = pd.to_datetime(errors_pd["month_start"])
        sql_errors = sql_views["error_types"].copy()
        sql_errors["month_start"] = pd.to_datetime(sql_errors["month_start"])

        acct_pd = acct_month.copy()
        acct_pd["month_start"] = pd.to_datetime(acct_pd["month_start"])
        sql_acct = sql_views["account_month"].copy()
        sql_acct["month_start"] = pd.to_datetime(sql_acct["month_start"])

        checks = pd.concat([
            compare("月度", monthly_pd, sql_monthly, ["month_start"]),
            compare("功能", feature, sql_views["feature"], ["feature_name"]),
            compare("错误类型", errors_pd, sql_errors, ["month_start", "feature_name", "error_type"]),
            compare("账户-月", acct_pd, sql_acct, ["account_id", "month_start"]),
        ], ignore_index=True)

        checks.to_csv(config.TABLE_DIR / "ai_sql_vs_pandas.csv", index=False)
        bad = checks[checks["结果"] != "一致"]
        reconcile_ok = bad.empty

        print(f"  比对 {len(checks)} 个指标列，覆盖 "
              f"{len(monthly)} 个月 / {len(feature)} 个功能 / {len(acct_pd):,} 个账户-月")
        if reconcile_ok:
            print("  全部一致")
        else:
            print("  存在不一致：")
            print(bad.to_string(index=False))

    # ---- 摘要 ----
    total_requests = len(logs)
    total_cost = logs["estimated_cost_usd_at_assumed_price"].sum()
    print("\n" + "=" * 74)
    print("AI 使用概况（模拟数据）")
    print("=" * 74)
    print(f"  请求总量 {total_requests:,}　账户 {logs['account_id'].nunique()}　"
          f"整体成功率 {logs['is_success'].mean():.2%}")
    print(f"  估算成本 ${total_cost:,.2f}（按 {config.PRICE_SNAPSHOT_DATE} 单价快照，非真实账单）")
    print(f"  成本/千次请求 ${total_cost / total_requests * 1000:,.2f}")

    print("\n  功能维度：")
    view = feature[["feature_name", "requests", "success_rate", "avg_latency_ms",
                    "p95_latency_ms", "estimated_cost_usd_at_assumed_price"]].copy()
    view["成本占比"] = (view["estimated_cost_usd_at_assumed_price"] / total_cost * 100).round(1)
    print(view.to_string(index=False))

    print("\n  请求集中度：")
    print(concentration.to_string(index=False))

    anomalies = incidents[incidents["is_anomaly"]]
    print(f"\n  异常日检测：命中 {len(anomalies)} 个「功能-日」组合"
          f"（阈值：低于功能基线 3 个二项标准误，且当日请求 ≥ 20）")
    if not anomalies.empty:
        print(anomalies.head(8).to_string(index=False))

    print(f"\n产物已写入 {config.TABLE_DIR.relative_to(PROJECT_ROOT)}/")
    return 0 if reconcile_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
