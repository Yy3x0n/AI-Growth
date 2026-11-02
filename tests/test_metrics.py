"""指标计算的单元测试。

测试策略：用 5~10 行构造数据，覆盖每个口径最容易出错的分支。
不连数据库——纯函数才能这样测，这也是 metrics.py 保持无 IO 的原因。

重点覆盖的三个坑：
1. 同一账户同月多条订阅 → 必须按胜出规则只取一条
2. 账户-月脊柱缺月 → 必须补 0，而不是留空
3. 使用记录挂不上账户 → 必须报错，不能静默丢弃
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import metrics


def make_accounts(*ids: str) -> pd.DataFrame:
    return pd.DataFrame({"account_id": list(ids), "churn_flag": [False] * len(ids)})


def make_subs(rows: list[tuple]) -> pd.DataFrame:
    """rows: (subscription_id, account_id, start, end, mrr)"""
    return pd.DataFrame(
        rows,
        columns=["subscription_id", "account_id", "start_date", "end_date", "mrr_amount"],
    ).assign(
        start_date=lambda d: pd.to_datetime(d["start_date"]),
        end_date=lambda d: pd.to_datetime(d["end_date"]),
        plan_tier="Basic",
        seats=1,
        upgrade_flag=False,
        downgrade_flag=False,
        billing_frequency="monthly",
    )


def test_usage_with_account_raises_on_orphan() -> None:
    """订阅映射缺失时必须抛错——静默丢数据会让所有账户级指标偏小。"""
    usage = pd.DataFrame({"subscription_id": ["S1", "S_MISSING"], "usage_date": pd.to_datetime(["2023-01-05"] * 2)})
    subs = make_subs([("S1", "A1", "2023-01-01", None, 100)])
    with pytest.raises(ValueError, match="无法匹配"):
        metrics.usage_with_account(usage, subs)


def test_snapshot_picks_latest_start_when_overlapping() -> None:
    """同账户同月两条订阅：取 start_date 更新的那条（胜出规则第一条）。"""
    subs = make_subs(
        [
            ("S1", "A1", "2023-01-01", None, 100),   # 更早开始
            ("S2", "A1", "2023-02-10", None, 500),   # 更晚开始 → 应胜出
        ]
    )
    snapshot = metrics.account_month_snapshot(make_accounts("A1"), subs)
    feb = snapshot[snapshot["month_start"] == pd.Timestamp("2023-02-01")].iloc[0]
    assert feb["subscription_id"] == "S2"
    assert feb["mrr_amount"] == 500


def test_snapshot_tie_breaks_by_mrr_then_id() -> None:
    """同一天开始的两条订阅：MRR 更高者胜出。"""
    subs = make_subs(
        [
            ("S1", "A1", "2023-01-01", None, 100),
            ("S2", "A1", "2023-01-01", None, 900),
        ]
    )
    snapshot = metrics.account_month_snapshot(make_accounts("A1"), subs)
    jan = snapshot[snapshot["month_start"] == pd.Timestamp("2023-01-01")].iloc[0]
    assert jan["mrr_amount"] == 900


def test_snapshot_keeps_zero_months_in_spine() -> None:
    """没有订阅的月份必须保留且 mrr=0，否则月度序列会缺月。"""
    subs = make_subs([("S1", "A1", "2023-06-01", "2023-07-31", 300)])
    snapshot = metrics.account_month_snapshot(make_accounts("A1"), subs)
    assert len(snapshot) == len(metrics.month_index())
    may = snapshot[snapshot["month_start"] == pd.Timestamp("2023-05-01")].iloc[0]
    assert may["mrr_amount"] == 0
    assert may["has_active_sub"] == 0


def test_snapshot_excludes_subscription_before_start() -> None:
    """订阅开始前的月份不应被计入付费月。"""
    subs = make_subs([("S1", "A1", "2023-06-15", None, 300)])
    snapshot = metrics.account_month_snapshot(make_accounts("A1"), subs)
    june = snapshot[snapshot["month_start"] == pd.Timestamp("2023-06-01")].iloc[0]
    # 6 月内开始，算 6 月有效
    assert june["has_active_sub"] == 1
    may = snapshot[snapshot["month_start"] == pd.Timestamp("2023-05-01")].iloc[0]
    assert may["has_active_sub"] == 0


def test_feature_span_error_rate() -> None:
    """错误率 = 有错误的记录数 / 总记录数，而不是错误次数求和。"""
    usage = pd.DataFrame(
        {
            "account_id": ["A1"] * 4,
            "usage_date": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04"]),
            "feature_name": ["f1", "f2", "f1", "f3"],
            "usage_count": [10, 10, 10, 10],
            "usage_duration_secs": [100, 100, 100, 100],
            "error_count": [0, 3, 0, 1],
        }
    )
    span = metrics.account_feature_span(usage)
    row = span.iloc[0]
    assert row["features_used"] == 3
    assert row["error_records"] == 2
    assert row["error_rate"] == 0.5


def test_churn_summary_reports_three_lenses() -> None:
    """三个口径必须同时返回，避免报告只挑一个好看的数字。"""
    accounts = pd.DataFrame({"account_id": ["A1", "A2", "A3", "A4"], "churn_flag": [True, True, False, False]})
    churn = pd.DataFrame(
        {
            "account_id": ["A1", "A1", "A2", "A3"],
            "is_reactivation": [False, True, False, False],
        }
    )
    result = metrics.churn_summary(churn, accounts)
    assert result["churned_by_events"] == 3   # A1/A2/A3 出现过事件
    assert result["churned_net"] == 3         # 排除复活后仍是 A1/A2/A3（A1 另有净事件）
    assert result["churned_by_flag"] == 2     # 快照标签只有 A1/A2
    assert result["reactivation_events"] == 1
