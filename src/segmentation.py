"""客户分层（Pandas 实现）。

与 sql/03_customer_segments.sql 是同一套规则的两个实现，scripts/segment_accounts.py
会对两者的层级规模做逐项比对，确保口径没有漂移。

分层为什么必须互斥：
六层如果允许重叠，各层账户数相加会超过总数，占比就失去意义，
也无法回答"优先运营哪一层"这种排序问题。这里采用**优先级命中即停**。

阈值为什么取分位数：
本项目没有任何行业基准可比（数据是合成的），若拍一个"次月留存 < 60% 即低活跃"
之类的阈值，无法自证依据。改用实际分布的 P25/P75，并把分布写进文档，
至少能回答"为什么是这个数"。
"""

from __future__ import annotations

import pandas as pd

# 分层优先级：顺序即优先级，命中即停
#
# 第一层原名「已停付」，阶段 6 复核时发现**命名与事实不符**：
# 那 69 个账户全部持有有效订阅，只是 mrr_amount = 0（全表 778/5000 条订阅金额 <= 0，
# 其中甚至包含 Enterprise 套餐）。所以它们不是"停止付费"，而是"订阅金额为零"。
# 若沿用旧名，任何人核对数据都会发现"已停付却有有效订阅"的矛盾。
SEGMENT_ORDER = ["零 MRR", "风险关注", "高价值", "深度采用", "浅层采用", "稳健基础"]


def compute_thresholds(profile: pd.DataFrame) -> dict:
    """从实际分布计算阈值。返回的字典会被写入 CSV 与文档，保证可复现。

    注意：风险层刻意用 **P90** 而不是 P75。
    P75 按定义必然圈进 25% 的账户，会让「风险关注」变成垃圾桶层，
    失去"优先处理谁"的排序意义；P90 表示"尾部 10%"，才符合风险的含义。
    """
    return {
        "mrr_p25": round(float(profile["mrr_latest"].quantile(0.25)), 2),
        "mrr_p50": round(float(profile["mrr_latest"].quantile(0.50)), 2),
        "mrr_p75": round(float(profile["mrr_latest"].quantile(0.75)), 2),
        "features_p25": int(profile["features_used"].quantile(0.25)),
        "features_p75": int(profile["features_used"].quantile(0.75)),
        "error_rate_p90": round(float(profile["error_rate"].quantile(0.90)), 4),
        "csat_low": 3.0,          # 实测取值下限，代表"最差档"
        "escalation_high": 2,     # 实测最大 3，≥2 已属异常
    }


def assign_segments(profile: pd.DataFrame, thresholds: dict) -> pd.DataFrame:
    """按优先级给每个账户打上唯一分层标签。

    返回的 DataFrame 保留原始列并新增 segment / segment_reason 两列，
    reason 记录命中的具体条件——被问"这个账户为什么在这层"时能直接答。
    """
    df = profile.copy()
    df["segment"] = "稳健基础"
    df["segment_reason"] = "未命中任何高优先条件"

    # 逆序赋值：先写低优先级，高优先级覆盖，天然实现"命中即停"
    conditions = [
        ("浅层采用", df["features_used"] <= thresholds["features_p25"],
         f"使用功能数 ≤ P25({thresholds['features_p25']})"),
        ("深度采用", df["features_used"] >= thresholds["features_p75"],
         f"使用功能数 ≥ P75({thresholds['features_p75']})"),
        ("高价值", df["mrr_latest"] >= thresholds["mrr_p75"],
         f"当前 MRR ≥ P75({thresholds['mrr_p75']})"),
        ("风险关注",
         (df["escalation_cnt"] >= thresholds["escalation_high"])
         | (df["avg_csat"].notna() & (df["avg_csat"] <= thresholds["csat_low"]))
         | (df["error_rate"] >= thresholds["error_rate_p90"]),
         f"升级工单≥{thresholds['escalation_high']} 或 满意度≤{thresholds['csat_low']} "
         f"或 错误率≥P90({thresholds['error_rate_p90']})"),
        ("零 MRR", df["mrr_latest"] == 0, "末月 MRR=0（仍持有效订阅，金额为 0）"),
    ]
    for name, mask, reason in conditions:
        df.loc[mask, "segment"] = name
        df.loc[mask, "segment_reason"] = reason

    return df


def summarize_segments(df: pd.DataFrame) -> pd.DataFrame:
    """分层汇总：规模、收入、使用、服务、流失，用于判断每层该做什么。"""
    total_mrr = df["mrr_latest"].sum()
    total_accounts = len(df)

    summary = (
        df.groupby("segment")
        .agg(
            accounts=("account_id", "count"),
            mrr_total=("mrr_latest", "sum"),
            avg_mrr=("mrr_latest", "mean"),
            avg_features=("features_used", "mean"),
            avg_usage_records=("usage_records", "mean"),
            avg_tickets=("ticket_cnt", "mean"),
            escalations=("escalation_cnt", "sum"),
            avg_csat=("avg_csat", "mean"),
            avg_error_rate=("error_rate", "mean"),
            with_churn_event=("has_churn_event", "sum"),
        )
        .reset_index()
    )
    summary["account_share"] = (summary["accounts"] / total_accounts).round(4)
    summary["revenue_share"] = (summary["mrr_total"] / total_mrr if total_mrr else 0).round(4)
    summary["churn_event_rate"] = (summary["with_churn_event"] / summary["accounts"]).round(4)

    # 按业务优先级排序，而不是按账户数排序——报告要直接回答"先做哪一层"
    summary["_order"] = summary["segment"].map({s: i for i, s in enumerate(SEGMENT_ORDER)})
    summary = summary.sort_values("_order").drop(columns="_order")

    numeric = ["avg_mrr", "avg_features", "avg_usage_records", "avg_tickets", "avg_csat", "avg_error_rate"]
    summary[numeric] = summary[numeric].round(2)
    return summary.reset_index(drop=True)


def validate(df: pd.DataFrame) -> None:
    """分层自检：互斥且完备。任何一条不满足都说明规则写错了，直接抛错。"""
    if df["segment"].isna().any():
        raise AssertionError("存在未分层的账户")
    if set(df["segment"]) > set(SEGMENT_ORDER):
        raise AssertionError(f"出现未定义的分层标签：{set(df['segment']) - set(SEGMENT_ORDER)}")
    # 每个账户只能有一个标签（DataFrame 一账户一行，天然满足；这里防未来误改）
    if df["account_id"].duplicated().any():
        raise AssertionError("账户存在重复行，分层结果不再互斥")


def sensitivity_analysis(profile: pd.DataFrame, thresholds: dict, factors=(0.8, 1.0, 1.2)) -> pd.DataFrame:
    """阈值敏感性：把关键阈值上下浮动 20%，看各层规模是否稳定。

    为什么必须做：features_used 的 P25=24、P75=32，中间只有 8 个取值点，
    阈值稍微一动，整层规模就会翻几倍。不做这个检查，
    报告里"深度采用 63 户"这种数字会被误读成稳定事实。
    """
    rows: list[dict] = []
    for factor in factors:
        scaled = dict(thresholds)
        scaled["mrr_p75"] = round(thresholds["mrr_p75"] * factor, 2)
        scaled["features_p75"] = int(thresholds["features_p75"] * factor)
        counts = assign_segments(profile, scaled)["segment"].value_counts()
        rows.append({"阈值浮动": f"{factor:.0%}", **{seg: int(counts.get(seg, 0)) for seg in SEGMENT_ORDER}})
    return pd.DataFrame(rows)
