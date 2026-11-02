#!/usr/bin/env python3
"""阶段 1：数据画像与质量检查。

用法：
    source .venv/bin/activate
    python scripts/profile_data.py

产出：
    outputs/tables/data_profile.csv          逐列画像（类型/空值/唯一值/范围）
    outputs/tables/data_quality_checks.csv   质量检查结果（规则ID/命中行数/处置）

设计原则：
1. **只报不改**：本脚本不修改任何数据，只量化问题。清洗规则由阶段 2 实施，
   并在 docs/data_quality_report.md 中登记影响行数。
2. 每条检查带规则 ID 与处置建议，保证"问题—证据—处置"三者可追溯。
3. 已知陷阱优先检查：usage_id 重复、订阅一对多、churn 口径冲突。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_loader import load_all  # noqa: E402

TABLE_DIR = PROJECT_ROOT / "outputs" / "tables"


def build_profile(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """逐列画像：类型、非空数、空值率、唯一值数、数值范围或示例值。"""
    rows: list[dict] = []
    for table, df in tables.items():
        for col in df.columns:
            series = df[col]
            non_null = int(series.notna().sum())
            record = {
                "table": table,
                "column": col,
                "dtype": str(series.dtype),
                "n_rows": len(df),
                "n_non_null": non_null,
                "null_rate": round(1 - non_null / len(df), 4) if len(df) else None,
                "n_unique": int(series.nunique(dropna=True)),
            }
            if pd.api.types.is_numeric_dtype(series) and non_null:
                record["min"] = float(series.min())
                record["max"] = float(series.max())
                record["mean"] = round(float(series.mean()), 4)
            elif pd.api.types.is_datetime64_any_dtype(series) and non_null:
                record["min"] = str(series.min())
                record["max"] = str(series.max())
            else:
                # 类别列给出前 3 个高频值，便于快速识别枚举域
                top = series.value_counts().head(3)
                record["min"] = " / ".join(str(v) for v in top.index)
                record["max"] = f"top1_count={int(top.iloc[0]) if len(top) else 0}"
            rows.append(record)
    return pd.DataFrame(rows)


def check_quality(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """执行质量检查，返回规则表。

    severity 取值：
        high   影响核心指标口径，必须处理或明确降级说明
        mid    影响部分分析，需在报告中披露
        low    属于正常业务语义，仅登记
    """
    acc = tables["accounts"]
    sub = tables["subscriptions"]
    use = tables["feature_usage"]
    tic = tables["support_tickets"]
    ch = tables["churn_events"]

    checks: list[dict] = []

    def add(rule: str, table: str, item: str, hits: int, total: int, severity: str, action: str) -> None:
        checks.append(
            {
                "rule_id": rule,
                "table": table,
                "check": item,
                "hits": hits,
                "total": total,
                "rate": round(hits / total, 4) if total else None,
                "severity": severity,
                "action": action,
            }
        )

    # ---- Q-01 主键唯一性 ----
    add("Q-01", "accounts", "account_id 唯一", int(len(acc) - acc.account_id.nunique()), len(acc), "high", "主键冲突则无法作为账户维度使用")
    add("Q-02", "subscriptions", "subscription_id 唯一", int(len(sub) - sub.subscription_id.nunique()), len(sub), "high", "主键冲突会重复计入 MRR")
    add("Q-03", "support_tickets", "ticket_id 唯一", int(len(tic) - tic.ticket_id.nunique()), len(tic), "high", "主键冲突会重复计工单量")
    add("Q-04", "churn_events", "churn_event_id 唯一", int(len(ch) - ch.churn_event_id.nunique()), len(ch), "high", "主键冲突会重复计流失事件")

    # ---- Q-05 usage_id 重复（上游 README 已明示）----
    dup_extra = int(len(use) - use.usage_id.nunique())
    fully_dup = int(use.duplicated().sum())
    add(
        "Q-05", "feature_usage", "usage_id 重复（超额行数）", dup_extra, len(use), "high",
        "不删除：重复 usage_id 的其余字段不同，属业务事件而非脏复制；下游用整行去重或保留并标记",
    )
    add(
        "Q-06", "feature_usage", "整行完全重复", fully_dup, len(use), "high",
        "若为 0 则说明重复仅发生在 ID 维度，可用整行去重安全处理",
    )

    # ---- Q-07 外键完整性 ----
    use_subs = set(sub.subscription_id)
    acc_ids = set(acc.account_id)
    add("Q-07", "feature_usage", "subscription_id 无法关联 subscriptions",
        int((~use.subscription_id.isin(use_subs)).sum()), len(use), "high", "孤子记录无法归属账户，必须排查")
    add("Q-08", "subscriptions", "account_id 无法关联 accounts",
        int((~sub.account_id.isin(acc_ids)).sum()), len(sub), "high", "同上")
    add("Q-09", "support_tickets", "account_id 无法关联 accounts",
        int((~tic.account_id.isin(acc_ids)).sum()), len(tic), "high", "同上")
    add("Q-10", "churn_events", "account_id 无法关联 accounts",
        int((~ch.account_id.isin(acc_ids)).sum()), len(ch), "high", "同上")

    # ---- Q-11 空值 ----
    add("Q-11", "subscriptions", "end_date 为空（在营订阅）", int(sub.end_date.isna().sum()), len(sub), "low",
        "空值代表订阅仍在生效，不是缺失；月度快照按空值视为未完结算")
    add("Q-12", "support_tickets", "satisfaction_score 为空", int(tic.satisfaction_score.isna().sum()), len(tic), "mid",
        "CSAT 分母只能取有评分工单，报告须披露实际样本量，不得按 0 填充")
    add("Q-13", "churn_events", "feedback_text 为空", int(ch.feedback_text.isna().sum()), len(ch), "low",
        "定性字段缺失，不影响定量指标")

    # ---- Q-14 日期逻辑 ----
    bad_range = int(((sub.end_date.notna()) & (sub.end_date < sub.start_date)).sum())
    add("Q-14", "subscriptions", "end_date 早于 start_date", bad_range, len(sub), "high", "倒挂区间会使时长与留存计算为负")

    usage_join = use.merge(sub[["subscription_id", "start_date", "end_date"]], on="subscription_id", how="left")
    before_start = int((usage_join.usage_date < usage_join.start_date).sum())
    after_end = int(
        (usage_join.end_date.notna() & (usage_join.usage_date > usage_join.end_date)).sum()
    )
    add("Q-15", "feature_usage", "usage_date 早于订阅开始", before_start, len(use), "high",
        "超出订阅周期的使用记录会让活跃口径失真")
    add("Q-16", "feature_usage", "usage_date 晚于订阅结束", after_end, len(use), "mid",
        "可能是宽限期或数据生成误差，需在口径中说明是否纳入")

    # ---- Q-17 churn 口径冲突（本数据集最关键的语义问题）----
    churn_accounts = int(ch.account_id.nunique())
    flag_accounts = int(acc.churn_flag.sum())
    add("Q-17", "accounts/churn_events", "两处流失账户数不一致",
        abs(churn_accounts - flag_accounts), churn_accounts, "high",
        "churn_events 含复活事件，账户数会大于 churn_flag；流失率口径必须择一并写明")

    # ---- Q-18 订阅一对多与同月重叠 ----
    per_account = sub.groupby("account_id").size()
    add("Q-18", "subscriptions", "账户存在多条订阅（行数-账户数）",
        int(len(sub) - sub.account_id.nunique()), len(sub), "high",
        "禁止用订阅行数当账户数；MRR 需按账户-月择一有效订阅")

    # 展开订阅到月份，统计同一账户同一月出现多条有效订阅的情况
    months = pd.date_range("2023-01-01", "2024-12-31", freq="MS")
    exp_rows = []
    for row in sub[["account_id", "subscription_id", "start_date", "end_date", "mrr_amount"]].itertuples(index=False):
        end = row.end_date if pd.notna(row.end_date) else pd.Timestamp("2024-12-31")
        for m in months:
            m_end = m + pd.offsets.MonthEnd(0)
            if row.start_date <= m_end and m <= end:
                exp_rows.append((row.account_id, m, row.subscription_id))
    if exp_rows:
        exp = pd.DataFrame(exp_rows, columns=["account_id", "month", "subscription_id"])
        overlap_pairs = int((exp.groupby(["account_id", "month"]).size() > 1).sum())
        add("Q-19", "subscriptions", "同一账户同一月存在多条有效订阅",
            overlap_pairs, int(exp.groupby(["account_id", "month"]).ngroups), "high",
            "MRR 会重复计入；必须定义胜出规则：start_date 最新 > mrr 最高 > subscription_id")

    # ---- Q-20 数值异常 ----
    nonpositive_mrr = int((sub.mrr_amount <= 0).sum())
    add("Q-20", "subscriptions", "mrr_amount <= 0", nonpositive_mrr, len(sub), "mid",
        "零元订阅多为试用，收入类指标需排除或单列")
    zero_usage = int((use.usage_count <= 0).sum())
    add("Q-21", "feature_usage", "usage_count <= 0", zero_usage, len(use), "mid",
        "有记录但无使用次数，计算活跃时需明确是否算作活跃")

    # ---- Q-22 功能维度语义 ----
    n_features = int(use.feature_name.nunique())
    add("Q-22", "feature_usage", f"feature_name 取值域（共 {n_features} 个）", n_features, n_features, "high",
        "取值为 feature_1..feature_N 编号，无 AI 问答/总结/写作语义；"
        "涉及 AI 功能的分析只能用模拟日志，不得把编号功能称为 AI 功能")

    # ---- Q-23 / Q-24 时间轴连贯性（本数据集最根本的限制）----
    # 把 usage 经 subscription 映射到 account，再取该账户的最早使用日
    usage_acct = use.merge(sub[["subscription_id", "account_id"]], on="subscription_id", how="left")
    first_use = usage_acct.groupby("account_id").usage_date.min().rename("first_use")
    with_signup = acc.set_index("account_id").join(first_use)
    before_signup = int((with_signup.first_use < with_signup.signup_date).sum())
    add("Q-23", "accounts/feature_usage", "账户首次使用日早于注册日", before_signup, len(with_signup), "high",
        "生成器未保证时间轴连贯；据此无法构建「注册→首次使用」的激活漏斗，"
        "激活与留存只能改用「首次使用日」为锚点，或改用模拟数据")

    # 使用记录是否落在该账户的任一订阅区间内
    acct_windows = usage_acct.merge(
        sub[["account_id", "start_date", "end_date"]], on="account_id", how="left"
    )
    in_window = (acct_windows.usage_date >= acct_windows.start_date) & (
        acct_windows.end_date.isna() | (acct_windows.usage_date <= acct_windows.end_date)
    )
    outside = int((~in_window).sum())
    add("Q-24", "feature_usage", "使用记录不在该账户任一订阅区间内", outside, len(acct_windows), "high",
        "分母为 usage × 该账户订阅数（会放大），此处仅用于判断时间一致性是否成立")

    return pd.DataFrame(checks)


def main() -> int:
    TABLE_DIR.mkdir(parents=True, exist_ok=True)
    tables = load_all()

    print("=" * 78)
    print("阶段 1 数据画像")
    print("=" * 78)
    for name, df in tables.items():
        print(f"{name:16s} {len(df):>7,} 行 × {df.shape[1]:>2} 列")
    print(f"{'合计':16s} {sum(len(df) for df in tables.values()):>7,} 行")

    profile = build_profile(tables)
    profile_path = TABLE_DIR / "data_profile.csv"
    profile.to_csv(profile_path, index=False)

    checks = check_quality(tables)
    checks_path = TABLE_DIR / "data_quality_checks.csv"
    checks.to_csv(checks_path, index=False)

    print()
    print("=" * 78)
    print("质量检查（仅登记，不修改数据）")
    print("=" * 78)
    for row in checks.itertuples(index=False):
        flag = {"high": "!!", "mid": "! ", "low": "  "}[row.severity]
        print(f"[{flag}] {row.rule_id} {row.table:22s} {row.check:34s} 命中 {row.hits:>6,}")

    high = int((checks.severity == "high").sum())
    print()
    print(f"共 {len(checks)} 项检查，其中 high {high} 项。")
    print(f"逐列画像 → {profile_path.relative_to(PROJECT_ROOT)}")
    print(f"检查结果 → {checks_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
