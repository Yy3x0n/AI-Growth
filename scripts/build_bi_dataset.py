#!/usr/bin/env python
"""把项目的分析结果整理成看板可直接消费的数据层（维度表 + 事实表）。

为什么要单独做一层
    outputs/tables 里的 CSV 是"分析产物"，列名是给 Python 读的、粒度也混在一起
    （既有月度汇总，又有账户级宽表，还有长宽不一的实验表）。直接喂给 Tableau 有两个问题：
      1. 同一张表里既有维度又有度量、粒度不一致，拖进工作表会算错（行级重复计数）；
      2. 中英文混排 + 下划线列名，在 Tableau 界面上不便于业务方阅读。

    因此这里做一次"面向 BI"的再加工：
      - 一张维度表 dim_account（账户粒度，500 行）；
      - 四张事实表，各自单一粒度（月度 / 账户月 / 功能 / 实验臂）；
      - 列名保持英文（Tableau 内部引用稳定），中文名放进 .twb 的 caption，
        既不影响版本管理与人眼 diff，界面上又显示中文。

    这些 CSV 全部由上游产物派生，不含新的计算口径——任何数字都应与
    outputs/tables 里同名列逐项相等（脚本末尾有断言做自校验）。

对应设计文档：docs/tableau_dashboard.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config  # noqa: E402
from src.segmentation import SEGMENT_ORDER  # noqa: E402


def _read(name: str) -> pd.DataFrame:
    path = config.TABLE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"缺少上游产物 {path}，请先跑 scripts/run_analysis.py")
    # utf-8-sig：部分产物由 Excel 链路写出，带 BOM
    return pd.read_csv(path, encoding="utf-8-sig")


def build_dim_account() -> pd.DataFrame:
    """账户维度：一行一个 account_id，是分层看板与散点图的主表。"""
    src = _read("account_segments.csv")
    ai = _read("ai_account_month.csv")

    # 账户级 AI 用量（把账户月事实表聚到账户粒度，避免两表 join 后行数翻倍）
    ai_by_account = (
        ai.groupby("account_id", as_index=False)
        .agg(ai_requests=("requests", "sum"), ai_cost_usd=("estimated_cost_usd_at_assumed_price", "sum"))
    )

    dim = src.merge(ai_by_account, on="account_id", how="left")
    dim[["ai_requests", "ai_cost_usd"]] = dim[["ai_requests", "ai_cost_usd"]].fillna(0)

    # 分层排序键：Tableau 里默认按字母序排，中文会乱；给一个显式序位列，
    # 在 Tableau 端把 segment_order 设为"排序依据"即可稳定呈现业务优先级。
    order = {s: i + 1 for i, s in enumerate(SEGMENT_ORDER)}
    dim["segment_order"] = dim["segment"].map(order)

    dim = dim.rename(
        columns={
            "churn_flag": "is_churned",
            "has_churn_event": "has_churn_event",
            "mrr_latest": "mrr_latest",
        }
    )
    dim["is_churned"] = dim["is_churned"].astype(int)
    dim["has_churn_event"] = dim["has_churn_event"].astype(int)

    keep = [
        "account_id", "industry", "country", "referral_source",
        "signup_date", "seats", "segment", "segment_order", "segment_reason",
        "is_churned", "has_churn_event",
        "mrr_latest", "peak_mrr", "months_paying",
        "usage_records", "features_used", "total_usage_count",
        "error_records", "error_rate",
        "ticket_cnt", "escalation_cnt", "avg_csat", "avg_resolution_hours",
        "ai_requests", "ai_cost_usd",
    ]
    dim = dim[keep].sort_values(["segment_order", "mrr_latest"], ascending=[True, False])
    return dim.reset_index(drop=True)


def build_fact_kpi_monthly() -> pd.DataFrame:
    """月度经营事实：24 行，月份是唯一粒度。"""
    df = _read("core_kpis.csv")
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m-%d")
    df["month_label"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m")
    df["mrr_per_active"] = (df["mrr_total"] / df["active_accounts"]).round(2)
    cols = [
        "month_start", "month_label", "active_accounts", "paying_accounts",
        "mrr_total", "arpa", "mrr_per_active", "upgrades", "downgrades",
    ]
    return df[cols].sort_values("month_start").reset_index(drop=True)


def build_fact_ai_monthly() -> pd.DataFrame:
    """AI 运营月度事实：24 行。成本字段沿用项目的假设单价，口径见数据字典。"""
    df = _read("ai_monthly_summary.csv")
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m-%d")
    df["month_label"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m")
    df = df.rename(columns={"estimated_cost_usd_at_assumed_price": "cost_usd"})
    df["total_tokens"] = df["input_tokens"] + df["output_tokens"]
    df["cost_per_request"] = (df["cost_usd"] / df["requests"]).round(6)
    cols = [
        "month_start", "month_label", "requests", "active_accounts",
        "success_requests", "success_rate", "avg_latency_ms", "p95_latency_ms",
        "input_tokens", "output_tokens", "total_tokens", "effective_tasks",
        "cost_usd", "cost_per_request",
    ]
    return df[cols].sort_values("month_start").reset_index(drop=True)


def build_fact_ai_feature() -> pd.DataFrame:
    """功能粒度事实：3 行。用于"功能对比"与成本结构。"""
    df = _read("ai_feature_summary.csv").rename(
        columns={"estimated_cost_usd_at_assumed_price": "cost_usd"}
    )
    df["cost_share"] = (df["cost_usd"] / df["cost_usd"].sum()).round(4)
    df["request_share"] = (df["requests"] / df["requests"].sum()).round(4)
    # 单位经济：每个"有效任务"摊到的成本，用来解释"为什么请求多不等于成本高"
    df["cost_per_1k_requests"] = (df["cost_usd"] / df["requests"] * 1000).round(4)
    return df.sort_values("requests", ascending=False).reset_index(drop=True)


def build_fact_ab_arm() -> pd.DataFrame:
    """A/B 实验长表：每个指标 × 每条臂一行，方便 Tableau 按 arm 着色对比。

    原始产物是"一指标一行、两组横排"的宽表，Tableau 里画分组柱状图很不顺手，
    这里转成 (metric, arm) 长表，一列 rate、一列 n，直接拖即可。
    """
    rows = []

    primary = _read("ab_test_primary.csv").iloc[0]
    for arm, rate_key, n_key, n in [
        ("对照组", "对照率", "对照样本", int(primary["对照样本"])),
        ("实验组", "实验率", "实验样本", int(primary["实验样本"])),
    ]:
        rows.append({
            "metric": "primary_7d_activation",
            "metric_label": "7 日激活率（主指标）",
            "metric_type": "primary",
            "arm": arm,
            "rate": float(primary[rate_key]),
            "n": n,
            "ci_low": float(primary["CI_下限"]) if arm == "实验组" else pd.NA,
            "ci_high": float(primary["CI_上限"]) if arm == "实验组" else pd.NA,
            "p_value": float(primary["p值"]) if arm == "实验组" else pd.NA,
            "is_harm": False,
        })

    guard = _read("ab_test_guardrails.csv")
    label_map = {
        "post_7d_error_rate": "7 日内错误率",
        "support_ticket_7d": "7 日内工单率",
    }
    for _, g in guard.iterrows():
        for arm, rate_key in [("对照组", "control_rate"), ("实验组", "treatment_rate")]:
            rows.append({
                "metric": g["metric"],
                "metric_label": label_map.get(g["metric"], g["metric"]),
                "metric_type": "guardrail",
                "arm": arm,
                "rate": float(g[rate_key]),
                "n": int(primary["对照样本"]),
                "ci_low": float(g["ci_low"]) if arm == "实验组" else pd.NA,
                "ci_high": float(g["ci_high"]) if arm == "实验组" else pd.NA,
                "p_value": float(g["p_value"]) if arm == "实验组" else pd.NA,
                "is_harm": bool(g["is_harm"]),
            })

    df = pd.DataFrame(rows)
    df["lift_pp"] = df.groupby("metric")["rate"].transform(lambda s: (s.iloc[1] - s.iloc[0]) * 100).round(4)
    return df


def build_fact_ab_power() -> pd.DataFrame:
    """功效敏感性：基线 × 目标提升 → 实测功效，用于解释"为什么不换种子"。"""
    df = _read("ab_power_sensitivity.csv")
    df.columns = ["baseline_rate", "target_rate", "power"]
    df["relative_lift"] = ((df["target_rate"] / df["baseline_rate"] - 1) * 100).round(2)
    df["baseline_label"] = (df["baseline_rate"] * 100).round(1).astype(str) + "%"
    return df


def build_fact_rag_variant() -> pd.DataFrame:
    """RAG 检索策略对照：3 种配置 × 6 个指标，转长表便于一张图对比。"""
    df = _read("rag_eval_summary.csv")
    df.columns = [
        "variant", "questions", "hit_at_3", "top1_accuracy", "recall_at_3", "mrr",
        "single_doc_hit_at_3", "cross_doc_hit_at_3", "cross_doc_recall_at_3",
        "refusal_distance_mean", "answerable_distance_mean",
    ]
    # 变体名里带"·"，Tableau 排序会乱，拆成策略 + 限额两个维度
    df["strategy"] = df["variant"].str.split("·").str[0]
    df["doc_limit"] = df["variant"].str.split("·").str[1]
    metrics = ["hit_at_3", "top1_accuracy", "recall_at_3", "mrr",
               "cross_doc_hit_at_3", "cross_doc_recall_at_3"]
    long = df.melt(
        id_vars=["variant", "strategy", "doc_limit"],
        value_vars=metrics,
        var_name="metric",
        value_name="score",
    )
    label = {
        "hit_at_3": "Hit@3", "top1_accuracy": "Top1 准确率", "recall_at_3": "Recall@3",
        "mrr": "MRR", "cross_doc_hit_at_3": "跨文档 Hit@3", "cross_doc_recall_at_3": "跨文档 Recall@3",
    }
    long["metric_label"] = long["metric"].map(label)
    return long.sort_values(["variant", "metric"]).reset_index(drop=True)


def build_fact_ai_account_month() -> pd.DataFrame:
    """账户 × 月 AI 事实：3026 行。用于帕累托与"高频账户"下钻。"""
    df = _read("ai_account_month.csv")
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m-%d")
    df["month_label"] = pd.to_datetime(df["month_start"]).dt.strftime("%Y-%m")
    df = df.rename(columns={"estimated_cost_usd_at_assumed_price": "cost_usd"})
    return df.sort_values(["account_id", "month_start"]).reset_index(drop=True)


WRITERS = {
    "dim_account": build_dim_account,
    "fact_kpi_monthly": build_fact_kpi_monthly,
    "fact_ai_monthly": build_fact_ai_monthly,
    "fact_ai_feature": build_fact_ai_feature,
    "fact_ai_account_month": build_fact_ai_account_month,
    "fact_ab_arm": build_fact_ab_arm,
    "fact_ab_power": build_fact_ab_power,
    "fact_rag_variant": build_fact_rag_variant,
}


def verify(tables: dict[str, pd.DataFrame]) -> None:
    """自校验：BI 层不能引入新的口径漂移，关键总量必须与上游一致。"""
    checks = []

    core = _read("core_kpis.csv")
    kpi = tables["fact_kpi_monthly"]
    checks.append(("MRR 合计", float(core["mrr_total"].sum()), float(kpi["mrr_total"].sum())))

    seg = _read("segment_summary.csv")
    dim = tables["dim_account"]
    checks.append(("账户数", float(seg["accounts"].sum()), float(len(dim))))
    checks.append(("账户 MRR 合计", float(seg["mrr_total"].sum()), float(dim["mrr_latest"].sum())))

    ai = _read("ai_monthly_summary.csv")
    aim = tables["fact_ai_monthly"]
    checks.append(("AI 请求合计", float(ai["requests"].sum()), float(aim["requests"].sum())))
    checks.append(("AI 成本合计", float(ai["estimated_cost_usd_at_assumed_price"].sum()), float(aim["cost_usd"].sum())))

    feat = _read("ai_feature_summary.csv")
    checks.append(("功能请求合计", float(feat["requests"].sum()), float(tables["fact_ai_feature"]["requests"].sum())))

    failed = False
    for name, expected, actual in checks:
        ok = abs(expected - actual) < 1e-6
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: 上游 {expected:,.4f} / BI 层 {actual:,.4f}")
        failed = failed or not ok
    if failed:
        raise AssertionError("BI 层与上游结果不一致，请检查聚合逻辑")


def main() -> None:
    out_dir = config.BI_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print("生成 Tableau 数据源 ...")
    tables = {}
    for name, fn in WRITERS.items():
        df = fn()
        df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8")
        tables[name] = df
        print(f"  {name}.csv  {len(df):>6,} 行 × {len(df.columns):>2} 列")

    print("\n自校验：")
    verify(tables)

    print(f"\n完成，输出目录 {out_dir}")


if __name__ == "__main__":
    main()
