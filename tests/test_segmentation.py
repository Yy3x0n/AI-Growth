"""客户分层的单元测试。

分层规则最容易出的错不是"算错数"，而是"不互斥"——
一旦某条规则顺序写反，同一个账户会同时满足多个条件，
最终标签取决于 if 的书写顺序，而这种 bug 在汇总表里看不出来。
"""

from __future__ import annotations

import pandas as pd

from src import segmentation


def make_profile(**overrides) -> pd.DataFrame:
    """构造最小可用的账户画像；默认值是"稳健基础"层的典型账户。"""
    base = {
        "account_id": "A1",
        "mrr_latest": 1500,
        "features_used": 28,
        "usage_records": 50,
        "error_rate": 0.30,
        "ticket_cnt": 4,
        "escalation_cnt": 0,
        "avg_csat": 4.0,
        "has_churn_event": 0,
        "churn_flag": False,
    }
    base.update(overrides)
    return pd.DataFrame([base])


def thresholds() -> dict:
    return {
        "mrr_p25": 342.0,
        "mrr_p50": 1080.5,
        "mrr_p75": 3184.0,
        "features_p25": 24,
        "features_p75": 32,
        "error_rate_p90": 0.394,
        "csat_low": 3.0,
        "escalation_high": 2,
    }


def test_default_is_stable_base() -> None:
    df = segmentation.assign_segments(make_profile(), thresholds())
    assert df.iloc[0]["segment"] == "稳健基础"


def test_stopped_paying_overrides_high_value() -> None:
    """MRR=0 优先于一切——零 MRR 的账户即使功能用得多，也不能算高价值。"""
    df = segmentation.assign_segments(
        make_profile(mrr_latest=0, features_used=40), thresholds()
    )
    assert df.iloc[0]["segment"] == "零 MRR"


def test_risk_overrides_high_value() -> None:
    """有升级工单的高收入账户进风险层：先救火，再谈扩销。"""
    df = segmentation.assign_segments(
        make_profile(mrr_latest=9000, escalation_cnt=2), thresholds()
    )
    assert df.iloc[0]["segment"] == "风险关注"


def test_high_value_boundary_is_inclusive() -> None:
    df = segmentation.assign_segments(make_profile(mrr_latest=3184.0), thresholds())
    assert df.iloc[0]["segment"] == "高价值"


def test_deep_adoption_boundary() -> None:
    df = segmentation.assign_segments(make_profile(features_used=32, mrr_latest=1000), thresholds())
    assert df.iloc[0]["segment"] == "深度采用"


def test_shallow_adoption_boundary() -> None:
    df = segmentation.assign_segments(make_profile(features_used=24, mrr_latest=1000), thresholds())
    assert df.iloc[0]["segment"] == "浅层采用"


def test_missing_csat_does_not_trigger_risk() -> None:
    """满意度为空（34 个账户）不能被当成 0 分而误判为风险。"""
    df = segmentation.assign_segments(make_profile(avg_csat=float("nan")), thresholds())
    assert df.iloc[0]["segment"] == "稳健基础"


def test_all_accounts_assigned_and_unique() -> None:
    """分层必须完备且互斥：500 行造出来的账户一个都不能漏。"""
    rows = []
    for i in range(50):
        rows.append(
            {
                "account_id": f"A{i}",
                "mrr_latest": (i % 5) * 2000,
                "features_used": 10 + (i % 30),
                "usage_records": 20 + i,
                "error_rate": 0.2 + (i % 7) * 0.05,
                "ticket_cnt": i % 6,
                "escalation_cnt": i % 3,
                "avg_csat": 3.0 + (i % 5) * 0.5,
                "has_churn_event": i % 2,
                "churn_flag": bool(i % 4 == 0),
            }
        )
    df = pd.DataFrame(rows)
    result = segmentation.assign_segments(df, thresholds())
    segmentation.validate(result)

    assert len(result) == 50
    assert result["segment"].notna().all()
    assert set(result["segment"]) <= set(segmentation.SEGMENT_ORDER)
    # 每行恰好一个标签，汇总后账户数之和必须等于总数
    assert result.groupby("segment").size().sum() == 50


def test_validate_rejects_duplicate_accounts() -> None:
    df = pd.DataFrame(
        [
            {"account_id": "A1", "segment": "稳健基础"},
            {"account_id": "A1", "segment": "高价值"},
        ]
    )
    try:
        segmentation.validate(df)
    except AssertionError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复账户应触发校验失败")
