"""指标计算（Pandas 实现）。

存在意义：**用两套独立实现互相验证**。
SQL 视图（sql/02_kpi_analysis.sql）是生产口径，本模块是与它完全独立的第二实现，
两者结果必须一致（scripts/run_analysis.py 会对账）。这样能挡住两类错误：
- SQL 写法问题（比如 LEFT JOIN 造成行放大、窗口函数排名边界）
- 逻辑理解偏差（比如"付费账户"到底是当月有订阅还是有收入）

设计约束：
1. 所有函数只接收 DataFrame、返回 DataFrame，不做任何 IO。
   这样单测可以喂 5 行小表，不需要数据库。
2. 不改变入参 DataFrame（避免调用方拿到被改过的数据）。
3. 关键口径写进注释，与视图注释一一对应。
"""

from __future__ import annotations

import pandas as pd

# 数据的最后一个月：账户-月快照用它取"当前 MRR"
LAST_MONTH = pd.Timestamp("2024-12-01")


def month_index(start: str = "2023-01-01", end: str = "2024-12-01") -> pd.DatetimeIndex:
    """生成数据覆盖的月份序列（每月 1 日）。"""
    return pd.date_range(start=start, end=end, freq="MS")


def usage_with_account(usage: pd.DataFrame, subs: pd.DataFrame) -> pd.DataFrame:
    """给使用明细补上 account_id。

    feature_usage 只有 subscription_id，而统计主体是 account，
    这一步是所有账户级指标的前置条件。若出现未匹配行直接抛错，
    而不是用 dropna 悄悄丢数据。
    """
    mapping = subs[["subscription_id", "account_id"]].drop_duplicates("subscription_id")
    merged = usage.merge(mapping, on="subscription_id", how="left")
    missing = merged["account_id"].isna().sum()
    if missing:
        raise ValueError(f"{missing} 条使用记录无法匹配 account_id，请先修外键")
    return merged


def account_month_snapshot(accounts: pd.DataFrame, subs: pd.DataFrame) -> pd.DataFrame:
    """账户-月快照：一账户一月一行，同月多条订阅按胜出规则取一条。

    胜出规则（与 SQL 视图 v_subscription_month 的 ROW_NUMBER 排序一致）：
        start_date 最新  →  mrr_amount 最高  →  subscription_id 最大

    为什么需要：89.3% 的账户-月存在多条同时生效的订阅，
    不去重会把 MRR 重复计入，收入指标整体虚高。
    """
    months = month_index()
    frames: list[pd.DataFrame] = []
    for month in months:
        month_end = month + pd.offsets.MonthEnd(0)
        active = subs[
            (subs["start_date"] <= month_end)
            & (subs["end_date"].isna() | (subs["end_date"] >= month))
        ]
        if active.empty:
            continue
        frames.append(active.assign(month_start=month))

    if frames:
        expanded = pd.concat(frames, ignore_index=True)
        expanded = expanded.sort_values(
            ["account_id", "month_start", "start_date", "mrr_amount", "subscription_id"],
            ascending=[True, True, False, False, False],
        )
        winner = expanded.drop_duplicates(["account_id", "month_start"], keep="first")
        winner = winner[
            ["account_id", "month_start", "subscription_id", "plan_tier", "seats",
             "mrr_amount", "upgrade_flag", "downgrade_flag", "billing_frequency"]
        ]
    else:
        winner = pd.DataFrame(
            columns=["account_id", "month_start", "subscription_id", "plan_tier", "seats",
                     "mrr_amount", "upgrade_flag", "downgrade_flag", "billing_frequency"]
        )

    # 完整脊柱：所有账户 × 所有月份，缺月补 0（否则月度环比会把缺失读成 0 增长）
    spine = pd.MultiIndex.from_product(
        [accounts["account_id"].unique(), months], names=["account_id", "month_start"]
    ).to_frame(index=False)

    snapshot = spine.merge(winner, on=["account_id", "month_start"], how="left")
    snapshot["mrr_amount"] = snapshot["mrr_amount"].fillna(0).astype(int)
    snapshot["has_active_sub"] = snapshot["subscription_id"].notna().astype(int)
    for col in ("upgrade_flag", "downgrade_flag"):
        snapshot[col] = snapshot[col].fillna(False).astype(bool)
    return snapshot


def monthly_kpi(snapshot: pd.DataFrame, usage_acct: pd.DataFrame) -> pd.DataFrame:
    """月度经营总览：活跃账户、付费账户、MRR、ARPA、升降级数。"""
    usage_month = usage_acct.assign(month_start=usage_acct["usage_date"].dt.to_period("M").dt.to_timestamp())
    active = usage_month.groupby("month_start")["account_id"].nunique().rename("active_accounts")

    paying = snapshot[snapshot["has_active_sub"] == 1]
    paying_agg = paying.groupby("month_start").agg(
        paying_accounts=("account_id", "nunique"),
        mrr_total=("mrr_amount", "sum"),
        upgrades=("upgrade_flag", "sum"),
        downgrades=("downgrade_flag", "sum"),
    )

    result = pd.DataFrame(index=month_index()).rename_axis("month_start")
    result = result.join(active).join(paying_agg).fillna(0)
    result["active_accounts"] = result["active_accounts"].astype(int)
    result["paying_accounts"] = result["paying_accounts"].astype(int)
    result["mrr_total"] = result["mrr_total"].astype(int)
    # ARPA 分母为 0 时返回 NaN，而不是 inf——inf 会污染后续均值计算
    result["arpa"] = (result["mrr_total"] / result["paying_accounts"].replace(0, pd.NA)).round(2)
    return result.reset_index()


def account_feature_span(usage_acct: pd.DataFrame) -> pd.DataFrame:
    """账户级使用画像：功能数、记录数、错误率、首末使用日。"""
    grouped = usage_acct.groupby("account_id")
    span = pd.DataFrame(
        {
            "first_use_date": grouped["usage_date"].min(),
            "last_use_date": grouped["usage_date"].max(),
            "usage_records": grouped.size(),
            "features_used": grouped["feature_name"].nunique(),
            "total_usage_count": grouped["usage_count"].sum(),
            "ok_records": usage_acct.assign(ok=usage_acct["error_count"] == 0).groupby("account_id")["ok"].sum(),
            "error_records": usage_acct.assign(err=usage_acct["error_count"] > 0).groupby("account_id")["err"].sum(),
        }
    )
    span["error_rate"] = (span["error_records"] / span["usage_records"]).round(4)
    return span.reset_index()


def account_profile(
    accounts: pd.DataFrame,
    span: pd.DataFrame,
    tickets: pd.DataFrame,
    snapshot: pd.DataFrame,
    churn: pd.DataFrame,
) -> pd.DataFrame:
    """一账户一行宽表：分层规则的唯一输入（对应 SQL 的 v_account_profile）。"""
    ticket_agg = tickets.groupby("account_id").agg(
        ticket_cnt=("ticket_id", "count"),
        escalation_cnt=("escalation_flag", "sum"),
        avg_csat=("satisfaction_score", "mean"),
        avg_resolution_hours=("resolution_time_hours", "mean"),
    )

    # 当前 MRR 取最后一个月快照；峰值取全部月份最大值
    latest = snapshot[snapshot["month_start"] == LAST_MONTH][["account_id", "mrr_amount"]]
    latest = latest.rename(columns={"mrr_amount": "mrr_latest"})
    peak = snapshot.groupby("account_id")["mrr_amount"].max().rename("peak_mrr")
    months_paying = snapshot.groupby("account_id")["has_active_sub"].sum().rename("months_paying")

    # 流失主口径：事件表去重账户（不排除复活，与 SQL v_kpi_churn.churned_by_events 一致）
    churn_agg = churn.groupby("account_id").size().rename("churn_events")
    churn_agg = churn_agg.to_frame()
    churn_agg["has_churn_event"] = 1

    profile = (
        accounts[["account_id", "industry", "country", "referral_source", "signup_date",
                  "seats", "churn_flag"]]
        .merge(span, on="account_id", how="left")
        .merge(ticket_agg, on="account_id", how="left")
        .merge(latest, on="account_id", how="left")
        .merge(peak, on="account_id", how="left")
        .merge(months_paying, on="account_id", how="left")
        .merge(churn_agg, on="account_id", how="left")
    )

    for col in ("features_used", "usage_records", "ok_records", "error_records",
                "ticket_cnt", "escalation_cnt", "months_paying", "mrr_latest", "peak_mrr", "churn_events"):
        profile[col] = profile[col].fillna(0).astype(int)
    profile["has_churn_event"] = profile["has_churn_event"].fillna(0).astype(int)
    profile["error_rate"] = profile["error_rate"].fillna(0.0)
    return profile


def feature_summary(usage_acct: pd.DataFrame) -> pd.DataFrame:
    """功能整体画像：用户数、记录数、错误率。用于定位"高频高错"的功能。"""
    grouped = usage_acct.groupby("feature_name")
    result = pd.DataFrame(
        {
            "user_accounts": grouped["account_id"].nunique(),
            "records": grouped.size(),
            "total_usage_count": grouped["usage_count"].sum(),
            "avg_usage_count": grouped["usage_count"].mean().round(2),
            "avg_duration_secs": grouped["usage_duration_secs"].mean().round(1),
            "error_rate": (
                usage_acct.assign(err=usage_acct["error_count"] > 0)
                .groupby("feature_name")["err"].sum()
                / grouped.size()
            ).round(4),
            "beta_records": usage_acct.assign(beta=usage_acct["is_beta_feature"] == 1)
            .groupby("feature_name")["beta"].sum(),
        }
    )
    return result.reset_index().sort_values("feature_name")


def churn_summary(churn: pd.DataFrame, accounts: pd.DataFrame) -> dict:
    """流失口径对照：三种算法并列，避免报告里只挑一个好看的数字。"""
    total = len(accounts)
    by_events = churn["account_id"].nunique()
    net = churn.loc[churn["is_reactivation"] == 0, "account_id"].nunique()
    by_flag = int(accounts["churn_flag"].sum())
    return {
        "churned_by_events": int(by_events),
        "churned_net": int(net),
        "churned_by_flag": by_flag,
        "reactivation_events": int((churn["is_reactivation"] == 1).sum()),
        "total_accounts": total,
        "churn_rate_net": round(net / total, 4),
        "churn_rate_flag": round(by_flag / total, 4),
    }
