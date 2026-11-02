#!/usr/bin/env python3
"""阶段 2：指标计算 + SQL / Pandas 交叉验证。

用法：
    python scripts/run_analysis.py

产出：
    outputs/tables/core_kpis.csv            月度核心指标（SQL 为口径基准）
    outputs/tables/kpi_sql_vs_pandas.csv    逐项对账结果
    outputs/tables/feature_summary.csv      功能画像
    outputs/tables/account_profile.csv      账户宽表（分层输入）
    outputs/tables/churn_summary.csv        流失口径对照

为什么必须对账：
任务书要求"SQL 结果与 Python 抽样核对一致"。抽样太弱——抽到的行恰好没错很常见。
这里改成**全量逐项比对**：月度指标按月、功能指标按功能、画像按账户，逐格算绝对差，
超过容差即非零退出。这样任何一个 join 写错都藏不住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pymysql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, metrics  # noqa: E402
from src.data_loader import load_all  # noqa: E402

TABLE_DIR = PROJECT_ROOT / "outputs" / "tables"
TOLERANCE = 1e-6  # 金额与计数必须完全一致；比率允许浮点误差


def connect():
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
    )


def query(conn, sql: str) -> pd.DataFrame:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)


def compare(label: str, merged: pd.DataFrame, cols: list[str], tolerance: float = TOLERANCE) -> list[dict]:
    """逐列比对 SQL 与 Pandas 的结果，返回对账明细。"""
    results: list[dict] = []
    for col in cols:
        left, right = merged[f"{col}_sql"], merged[f"{col}_py"]
        # 统一成 float 再比，避免 Decimal / int64 / float64 类型差异造成假阳性
        diff = (pd.to_numeric(left, errors="coerce") - pd.to_numeric(right, errors="coerce")).abs()
        max_diff = float(diff.max()) if diff.notna().any() else 0.0
        mismatched = int((diff > tolerance).sum())
        results.append(
            {
                "模块": label,
                "指标": col,
                "比对行数": int(len(merged)),
                "不一致行数": mismatched,
                "最大绝对差": round(max_diff, 6),
                "结论": "一致" if mismatched == 0 else "不一致 !!",
            }
        )
    return results


def main() -> int:
    TABLE_DIR.mkdir(parents=True, exist_ok=True)
    tables = load_all()
    accounts, subs, usage = tables["accounts"], tables["subscriptions"], tables["feature_usage"]
    tickets, churn = tables["support_tickets"], tables["churn_events"]

    print("=" * 78)
    print("Pandas 侧计算")
    print("=" * 78)
    usage_acct = metrics.usage_with_account(usage, subs)
    snapshot = metrics.account_month_snapshot(accounts, subs)
    span = metrics.account_feature_span(usage_acct)
    monthly_py = metrics.monthly_kpi(snapshot, usage_acct)
    feature_py = metrics.feature_summary(usage_acct)
    profile_py = metrics.account_profile(accounts, span, tickets, snapshot, churn)
    churn_py = metrics.churn_summary(churn, accounts)
    print(f"账户-月快照 {len(snapshot):,} 行｜账户画像 {len(profile_py):,} 行｜功能 {len(feature_py)} 个")

    conn = connect()
    try:
        monthly_sql = query(conn, "SELECT * FROM v_kpi_monthly ORDER BY month_start")
        feature_sql = query(conn, "SELECT * FROM v_kpi_feature_summary ORDER BY feature_name")
        churn_sql = query(conn, "SELECT * FROM v_kpi_churn").iloc[0].to_dict()
        profile_sql = query(conn, "SELECT * FROM v_account_profile")
    finally:
        conn.close()

    checks: list[dict] = []

    # ---- 月度指标对账 ----
    monthly_sql["month_start"] = pd.to_datetime(monthly_sql["month_start"])
    merged = monthly_sql.merge(monthly_py, on="month_start", suffixes=("_sql", "_py"))
    checks += compare("月度", merged, ["active_accounts", "paying_accounts", "mrr_total", "upgrades", "downgrades"])

    # ---- 功能指标对账 ----
    merged_feat = feature_sql.merge(feature_py, on="feature_name", suffixes=("_sql", "_py"))
    checks += compare("功能", merged_feat, ["user_accounts", "records", "total_usage_count", "error_rate"], 1e-4)

    # ---- 账户画像对账 ----
    merged_prof = profile_sql.merge(
        profile_py, on="account_id", suffixes=("_sql", "_py")
    )
    checks += compare("画像", merged_prof, ["features_used", "usage_records", "ticket_cnt", "mrr_latest"])

    # ---- 流失口径对账 ----
    for key, py_key in [
        ("churned_by_events", "churned_by_events"),
        ("churned_net", "churned_net"),
        ("churned_by_flag", "churned_by_flag"),
        ("reactivation_events", "reactivation_events"),
    ]:
        sql_value = int(churn_sql[key])
        py_value = int(churn_py[py_key])
        checks.append(
            {
                "模块": "流失",
                "指标": key,
                "比对行数": 1,
                "不一致行数": 0 if sql_value == py_value else 1,
                "最大绝对差": abs(sql_value - py_value),
                "结论": "一致" if sql_value == py_value else f"不一致（SQL {sql_value} vs Py {py_value}）",
            }
        )

    report = pd.DataFrame(checks)
    report.to_csv(TABLE_DIR / "kpi_sql_vs_pandas.csv", index=False)

    print()
    print("=" * 78)
    print("SQL / Pandas 全量对账")
    print("=" * 78)
    print(report.to_string(index=False))

    # ---- 落盘业务产物 ----
    monthly_sql.to_csv(TABLE_DIR / "core_kpis.csv", index=False)
    feature_sql.to_csv(TABLE_DIR / "feature_summary.csv", index=False)
    profile_py.to_csv(TABLE_DIR / "account_profile.csv", index=False)
    pd.DataFrame([churn_py]).to_csv(TABLE_DIR / "churn_summary.csv", index=False)

    failed = int((report["不一致行数"] > 0).sum())
    print()
    if failed:
        print(f"对账失败：{failed} 项不一致。SQL 与 Pandas 口径存在分歧，必须先修再往下做。")
        return 1

    print(f"对账通过：{len(report)} 项全部一致（容差 {TOLERANCE}）。")
    print(f"产物目录：{TABLE_DIR.relative_to(PROJECT_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
