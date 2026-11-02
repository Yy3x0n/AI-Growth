"""三个白名单指标函数 —— 唯一的取数入口。

为什么必须白名单

任务书 §6.4 明确：**不允许模型拼接任意 SQL 并直接访问数据库**。
因此 LLM 从头到尾拿不到连接对象，它只能"选择场景"，参数由本模块校验后
换算成固定的 Pandas 计算。即使提示词被注入，能造成的最大后果也只是
"用合法参数多查一次"，不存在 DROP TABLE 或全表导出的可能。

参数校验的四条硬规则

1. 日期必须是 `YYYY-MM-DD`，不接受相对时间（"上个月"这类由调用方换算）。
2. 必须落在观察期内（2023-01-01 ~ 2024-12-31）——超出范围会得到空结果，
   那不是"数据为 0"，而是"无数据"，两者必须区分。
3. `start <= end`，且跨度不超过 730 天，避免一次拉全量。
4. 所有函数返回**指标 + 口径 + 限制**三件套。只给数字的接口会诱使模型
   自行解释口径，那是最容易出错的地方。

每个返回值都带 `data_source`：`real`（T1 真实表）或 `simulated`（T2/T3 模拟）。
这份标记会一路传到报告正文，不允许在下游被抹掉。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from . import config, data_loader, metrics

# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

DATE_FORMAT = "%Y-%m-%d"
MAX_SPAN_DAYS = 730


class ParamError(ValueError):
    """参数不合法。调用方应把它当作"拒绝执行"，而不是重试。"""


def _parse_date(value: str, field: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ParamError(f"{field} 必须是字符串（格式 YYYY-MM-DD），收到 {type(value).__name__}")
    try:
        parsed = dt.datetime.strptime(value.strip(), DATE_FORMAT)
    except ValueError as exc:
        raise ParamError(f"{field} 格式错误：{value!r}，要求 YYYY-MM-DD") from exc
    return pd.Timestamp(parsed)


def _validate_range(start_date: str, end_date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    obs_start = pd.Timestamp(config.OBSERVATION_START)
    obs_end = pd.Timestamp(config.OBSERVATION_END)

    if start > end:
        raise ParamError(f"start_date({start_date}) 不能晚于 end_date({end_date})")
    if end < obs_start or start > obs_end:
        raise ParamError(
            f"区间 [{start_date}, {end_date}] 完全落在观察期 "
            f"[{config.OBSERVATION_START}, {config.OBSERVATION_END}] 之外，无数据可查"
        )
    if (end - start).days > MAX_SPAN_DAYS:
        raise ParamError(f"区间跨度 {(end - start).days} 天超过上限 {MAX_SPAN_DAYS} 天")

    # 越界不报错而是裁剪：区间部分重叠时裁剪更符合直觉，但要在结果里披露
    return max(start, obs_start), min(end, obs_end)


def _envelope(
    scenario: str,
    params: dict[str, Any],
    period: dict[str, str],
    data_source: str,
    metrics_payload: dict[str, Any],
    definitions: list[dict[str, str]],
    caveats: list[str],
    breakdown: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """统一返回结构。所有字段名都是英文，值可以是中文，便于程序处理。"""
    return {
        "scenario": scenario,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "params": params,
        "period": period,
        "data_source": data_source,
        "metrics": metrics_payload,
        "breakdown": breakdown or [],
        "definitions": definitions,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# 场景 1：新客激活
# ---------------------------------------------------------------------------

ACTIVATION_DEFINITIONS = [
    {
        "metric": "cohort_size",
        "definition": "本区间内注册的账户数（accounts.signup_date 落在区间内）",
        "denominator": "—",
    },
    {
        "metric": "eligible_size",
        "definition": "可判定转化的账户数：注册日 + 观察窗口 <= 数据截止日。"
                      "注册太晚、观察期不足的账户被剔除（右删失），不计入分母",
        "denominator": "—",
    },
    {
        "metric": "converted",
        "definition": "首个付费订阅的开始日 - 注册日 <= 观察窗口 的账户数",
        "denominator": "eligible_size",
    },
    {
        "metric": "conversion_rate",
        "definition": "观察窗口内完成首次付费订阅的账户占比。"
                      "这是「注册→付费」转化，不是「注册→首次使用」",
        "denominator": "eligible_size（非 cohort_size）",
    },
]


def get_activation_metrics(
    start_date: str,
    end_date: str,
    window_days: int = 7,
) -> dict[str, Any]:
    """新客激活：注册后 N 日内完成首次付费订阅的比例。

    **口径说明（重要）**：任务书原始设计是「注册 → 首次请求 → 首次有效任务」。
    但阶段 1 已实测：T1 的 feature_usage 与注册日时间轴不连贯（490/500 账户首次使用
    早于注册日），该漏斗无法构造，见 docs/data_capability_assessment.md。
    因此改用「注册 → 首次付费订阅」，这是这套数据唯一能支撑、且业务上同样成立的激活定义。
    """
    if not isinstance(window_days, int) or not 1 <= window_days <= 90:
        raise ParamError(f"window_days 必须是 1~90 的整数，收到 {window_days!r}")

    start, end = _validate_range(start_date, end_date)
    obs_end = pd.Timestamp(config.OBSERVATION_END)

    accounts = data_loader.load_raw("accounts")
    subs = data_loader.load_raw("subscriptions")

    first_sub = subs.groupby("account_id")["start_date"].min().rename("first_sub_date")

    cohort = accounts[(accounts["signup_date"] >= start) & (accounts["signup_date"] <= end)]
    cohort = cohort[["account_id", "signup_date"]].merge(first_sub, on="account_id", how="left")

    # 右删失：注册日晚于「截止日 - 窗口」的账户还没被观察够，不能算作"没转化"
    cohort["is_observed"] = cohort["signup_date"] + pd.Timedelta(days=window_days) <= obs_end
    eligible = cohort[cohort["is_observed"]].copy()
    censored = cohort[~cohort["is_observed"]]

    eligible["days_to_first_sub"] = (eligible["first_sub_date"] - eligible["signup_date"]).dt.days
    eligible["converted"] = eligible["days_to_first_sub"].notna() & (
        eligible["days_to_first_sub"] <= window_days
    )

    converted = int(eligible["converted"].sum())
    eligible_n = int(len(eligible))
    rate = round(converted / eligible_n, 4) if eligible_n else 0.0

    # 未转化里区分「真的没订阅」和「订阅了但太慢」，运营动作完全不同
    no_sub = int(eligible["first_sub_date"].isna().sum())
    late = eligible_n - converted - no_sub

    converted_rows = eligible[eligible["converted"]]
    median_days = (
        float(converted_rows["days_to_first_sub"].median()) if len(converted_rows) else None
    )

    # 月度分解：看趋势，而不是只看一个总数
    breakdown: list[dict[str, Any]] = []
    if eligible_n:
        monthly = eligible.assign(month=eligible["signup_date"].dt.to_period("M").astype(str))
        for month, grp in monthly.groupby("month"):
            breakdown.append(
                {
                    "month": month,
                    "eligible": int(len(grp)),
                    "converted": int(grp["converted"].sum()),
                    "conversion_rate": round(float(grp["converted"].mean()), 4),
                }
            )

    # 上一等长区间对照：用来判断"本次是异常还是一直如此"
    span = (end - start).days + 1
    prev_end = start - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=span - 1)
    previous: dict[str, Any] | None = None
    if prev_start >= pd.Timestamp(config.OBSERVATION_START):
        prev_cohort = accounts[
            (accounts["signup_date"] >= prev_start) & (accounts["signup_date"] <= prev_end)
        ][["account_id", "signup_date"]].merge(first_sub, on="account_id", how="left")
        prev_cohort["is_observed"] = (
            prev_cohort["signup_date"] + pd.Timedelta(days=window_days) <= obs_end
        )
        prev_eligible = prev_cohort[prev_cohort["is_observed"]].copy()
        if len(prev_eligible):
            prev_eligible["days"] = (
                prev_eligible["first_sub_date"] - prev_eligible["signup_date"]
            ).dt.days
            prev_conv = int(
                (prev_eligible["days"].notna() & (prev_eligible["days"] <= window_days)).sum()
            )
            prev_rate = round(prev_conv / len(prev_eligible), 4)
            previous = {
                "period": f"{prev_start.date()} ~ {prev_end.date()}",
                "eligible": int(len(prev_eligible)),
                "converted": prev_conv,
                "conversion_rate": prev_rate,
                "rate_delta_pp": round((rate - prev_rate) * 100, 2),
            }

    return _envelope(
        scenario="activation_diagnosis",
        params={"start_date": str(start.date()), "end_date": str(end.date()), "window_days": window_days},
        period={"start": str(start.date()), "end": str(end.date()), "as_of": str(obs_end.date())},
        data_source="real",
        metrics_payload={
            "cohort_size": int(len(cohort)),
            "eligible_size": eligible_n,
            "censored_size": int(len(censored)),
            "converted": converted,
            "conversion_rate": rate,
            "never_subscribed": no_sub,
            "subscribed_but_late": max(late, 0),
            "median_days_to_first_subscription": median_days,
            "previous_period": previous,
        },
        definitions=ACTIVATION_DEFINITIONS,
        caveats=[
            "口径为「注册→首次付费订阅」，不是「注册→首次使用」。"
            "T1 的使用记录与注册日时间轴不连贯（490/500 账户首次使用早于注册日），"
            "注册→使用漏斗无法构造，理由见 docs/data_capability_assessment.md。",
            f"注册日晚于 {obs_end.date()} 减 {window_days} 天的账户因观察不足被剔除（右删失），"
            "不计入分母。因此 cohort_size 与 eligible_size 不相等是正常的。",
            "本数据集的注册日期为模拟生成，季度间转化率差异可能来自生成分布而非真实业务变化。",
        ],
        breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# 场景 2：AI 功能采用
# ---------------------------------------------------------------------------

ADOPTION_DEFINITIONS = [
    {
        "metric": "active_accounts",
        "definition": "区间内至少发起过 1 次请求的账户数",
        "denominator": "—",
    },
    {
        "metric": "adoption_rate",
        "definition": "使用过该功能的账户数 / 区间活跃账户数。按账户去重，不按请求数",
        "denominator": "active_accounts",
    },
    {
        "metric": "requests_per_active_account",
        "definition": "区间总请求数 / 区间活跃账户数",
        "denominator": "active_accounts",
    },
    {
        "metric": "change_pp",
        "definition": "本区间采纳率 - 上一等长区间采纳率，单位百分点",
        "denominator": "—",
    },
]


def get_feature_adoption_metrics(
    start_date: str,
    end_date: str,
    source: str = "simulated",
) -> dict[str, Any]:
    """功能采用情况，含与上一等长区间的对比。

    source='simulated'（默认）读 T2 AI 请求日志，有 AI 问答/文档总结/AI 写作三个语义功能。
    source='real' 读 T1 feature_usage，功能名是 feature_1..feature_40，**无 AI 语义**。

    默认用 simulated 的原因：本场景问的是「AI 功能采用」，而 T1 的 feature_* 是编号占位，
    把 feature_12 说成"AI 问答"属于编造。真实功能渗透请显式传 source='real'，
    且报告里必须改称「功能渗透」而非「AI 采用」。
    """
    if source not in ("simulated", "real"):
        raise ParamError(f"source 只能是 simulated 或 real，收到 {source!r}")

    start, end = _validate_range(start_date, end_date)
    span = (end - start).days + 1
    prev_end = start - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=span - 1)

    if source == "simulated":
        from . import ai_usage

        logs = ai_usage.load_logs()
        frame = logs[["account_id", "request_time", "feature_name", "status"]].copy()
        frame["date"] = frame["request_time"].dt.normalize()
        date_col = "date"
        data_source = "simulated"
        unit_note = "功能为 AI 问答 / 文档总结 / AI 写作，来自模拟日志"
    else:
        usage = data_loader.load_raw("feature_usage")
        subs = data_loader.load_raw("subscriptions")
        frame = metrics.usage_with_account(usage, subs)[
            ["account_id", "usage_date", "feature_name", "error_count"]
        ].copy()
        frame["status"] = frame["error_count"].gt(0).map({True: "error", False: "success"})
        frame["date"] = frame["usage_date"].dt.normalize()
        date_col = "date"
        data_source = "real"
        unit_note = "功能为 feature_1..feature_40 编号，无 AI 语义"

    def _aggregate(lo: pd.Timestamp, hi: pd.Timestamp) -> dict[str, Any]:
        window = frame[(frame[date_col] >= lo) & (frame[date_col] <= hi)]
        active = int(window["account_id"].nunique())
        if not active:
            return {"active_accounts": 0, "requests": 0, "features": {}}

        per_feature: dict[str, Any] = {}
        for name, grp in window.groupby("feature_name"):
            users = int(grp["account_id"].nunique())
            requests_n = int(len(grp))
            per_feature[str(name)] = {
                "accounts": users,
                "requests": requests_n,
                "adoption_rate": round(users / active, 4),
                "requests_per_active_account": round(requests_n / active, 2),
                "success_rate": round(float((grp["status"] == "success").mean()), 4),
            }
        return {
            "active_accounts": active,
            "requests": int(len(window)),
            "features": per_feature,
        }

    current = _aggregate(start, end)
    previous = _aggregate(prev_start, prev_end) if prev_start >= pd.Timestamp(config.OBSERVATION_START) else None

    # 合并两期，算采纳率变化。差异要能一眼看出，所以放在同一行里
    rows: list[dict[str, Any]] = []
    for name, cur in sorted(current["features"].items(), key=lambda kv: -kv[1]["requests"]):
        prev = (previous or {}).get("features", {}).get(name)
        rows.append(
            {
                "feature_name": name,
                "accounts": cur["accounts"],
                "requests": cur["requests"],
                "adoption_rate": cur["adoption_rate"],
                "success_rate": cur["success_rate"],
                "previous_adoption_rate": prev["adoption_rate"] if prev else None,
                "change_pp": (
                    round((cur["adoption_rate"] - prev["adoption_rate"]) * 100, 2) if prev else None
                ),
            }
        )

    declining = [r["feature_name"] for r in rows if r["change_pp"] is not None and r["change_pp"] < 0]

    return _envelope(
        scenario="feature_adoption_diagnosis",
        params={"start_date": str(start.date()), "end_date": str(end.date()), "source": source},
        period={"start": str(start.date()), "end": str(end.date)},
        data_source=data_source,
        metrics_payload={
            "active_accounts": current["active_accounts"],
            "requests": current["requests"],
            "requests_per_active_account": (
                round(current["requests"] / current["active_accounts"], 2)
                if current["active_accounts"]
                else 0
            ),
            "feature_count": len(rows),
            "declining_features": declining,
            "previous_period": previous is not None,
            "previous_period_range": f"{prev_start.date()} ~ {prev_end.date()}" if previous else None,
        },
        definitions=ADOPTION_DEFINITIONS,
        caveats=[
            f"数据源为 {data_source}；{unit_note}。",
            "采纳率按账户去重计算，因此一个账户用多个功能会让各功能采纳率之和超过 100%，这是正常的。",
            (
                "模拟日志的功能占比与成功率由 src/config.py 的 AI_FEATURES 显式设定，"
                "因此「采用下降」的幅度反映的是设定参数，不是真实业务变化。"
                if data_source == "simulated"
                else "T1 的功能名为 feature_1..feature_40 编号，不得解释为具体 AI 能力。"
            ),
            "上一等长区间若早于观察期起点，则 change_pp 为空，不做对比。",
        ],
        breakdown=rows,
    )


# ---------------------------------------------------------------------------
# 场景 3：低活跃客户
# ---------------------------------------------------------------------------

LOW_ACTIVITY_DEFINITIONS = [
    {
        "metric": "known_accounts",
        "definition": "截至 as_of_date 已注册的账户数，作为沉睡率的分母。"
                      "不是「活跃账户数」，两者不可混用",
        "denominator": "—",
    },
    {
        "metric": "inactive_days",
        "definition": "截至 as_of_date，距该账户最后一次有功能使用记录的天数",
        "denominator": "—",
    },
    {
        "metric": "dormant_accounts",
        "definition": f"inactive_days >= 阈值 或 从未有使用记录的账户数",
        "denominator": "—",
    },
    {
        "metric": "mrr_at_risk",
        "definition": "沉睡账户在 as_of 所属月份的 MRR 合计（胜出订阅规则去重）",
        "denominator": "—",
    },
]


def get_low_activity_accounts(
    as_of_date: str,
    inactive_days: int = 30,
    top_n: int = 10,
) -> dict[str, Any]:
    """低活跃（沉睡）客户清单。

    「最后一次使用」按 usage → subscription → account 映射得到。
    注意：未活跃不等于流失，也可能只是没在用该功能模块，报告里不能等同。
    """
    if not isinstance(inactive_days, int) or not 1 <= inactive_days <= 365:
        raise ParamError(f"inactive_days 必须是 1~365 的整数，收到 {inactive_days!r}")
    if not isinstance(top_n, int) or not 1 <= top_n <= 50:
        raise ParamError(f"top_n 必须是 1~50 的整数，收到 {top_n!r}")

    as_of = _parse_date(as_of_date, "as_of_date")
    obs_start = pd.Timestamp(config.OBSERVATION_START)
    obs_end = pd.Timestamp(config.OBSERVATION_END)
    if as_of < obs_start or as_of > obs_end:
        raise ParamError(
            f"as_of_date({as_of_date}) 超出观察期 "
            f"[{config.OBSERVATION_START}, {config.OBSERVATION_END}]"
        )

    accounts = data_loader.load_raw("accounts")
    subs = data_loader.load_raw("subscriptions")
    usage = data_loader.load_raw("feature_usage")

    # 只保留 as_of 之前已注册的账户，否则"新注册还没用"会被误判为沉睡
    known = accounts[accounts["signup_date"] <= as_of][["account_id", "signup_date"]].copy()

    usage_acct = metrics.usage_with_account(usage, subs)
    usage_acct = usage_acct[usage_acct["usage_date"] <= as_of]
    last_use = usage_acct.groupby("account_id")["usage_date"].max().rename("last_usage_date")

    known = known.merge(last_use, on="account_id", how="left")
    known["inactive_days"] = (as_of - known["last_usage_date"]).dt.days

    # as_of 所属月份的 MRR：用胜出订阅规则，避免重叠订阅重复计入
    month_start = as_of.to_period("M").to_timestamp()
    snapshot = metrics.account_month_snapshot(accounts, subs)
    mrr = snapshot[snapshot["month_start"] == month_start][["account_id", "mrr_amount", "has_active_sub"]]
    known = known.merge(mrr, on="account_id", how="left")
    known["mrr_amount"] = known["mrr_amount"].fillna(0).astype(int)

    # 从未使用过 = 没有最后使用日；这类要单列，因为运营动作不同（可能根本没上手）
    never_used = known["last_usage_date"].isna()
    dormant_mask = never_used | (known["inactive_days"] >= inactive_days)
    dormant = known[dormant_mask].copy()

    # 分档：30 天内 / 30-59 / 60-89 / 90+ / 从未使用
    def _bucket(row: pd.Series) -> str:
        if pd.isna(row["last_usage_date"]):
            return "从未使用"
        d = row["inactive_days"]
        if d < 30:
            return "<30 天"
        if d < 60:
            return "30-59 天"
        if d < 90:
            return "60-89 天"
        return "90 天以上"

    known["inactive_bucket"] = known.apply(_bucket, axis=1)
    bucket_counts = {
        str(k): int(v) for k, v in known["inactive_bucket"].value_counts().sort_index().items()
    }

    at_risk = dormant.sort_values(["mrr_amount", "inactive_days"], ascending=[False, False]).head(top_n)
    sample = [
        {
            "account_id": str(row["account_id"]),
            "inactive_days": None if pd.isna(row["last_usage_date"]) else int(row["inactive_days"]),
            "last_usage_date": (
                None if pd.isna(row["last_usage_date"]) else str(row["last_usage_date"].date())
            ),
            "mrr_amount": int(row["mrr_amount"]),
            "has_active_sub": bool(row["has_active_sub"]) if not pd.isna(row["has_active_sub"]) else False,
        }
        for _, row in at_risk.iterrows()
    ]

    active_n = int(len(known))
    return _envelope(
        scenario="low_activity_customer",
        params={"as_of_date": str(as_of.date()), "inactive_days": inactive_days, "top_n": top_n},
        period={"as_of": str(as_of.date())},
        data_source="real",
        metrics_payload={
            # 命名刻意不用 active_base：这是「截至 as_of 已注册的账户」，
            # 不是「活跃账户」。两者混用会让沉睡率的分母凭空变大、指标显得更好看。
            "known_accounts": active_n,
            "dormant_accounts": int(len(dormant)),
            "dormant_share": round(len(dormant) / active_n, 4) if active_n else 0.0,
            "never_used_accounts": int(never_used.sum()),
            "mrr_at_risk": int(dormant["mrr_amount"].sum()),
            "mrr_total": int(known["mrr_amount"].sum()),
            "mrr_at_risk_share": (
                round(dormant["mrr_amount"].sum() / known["mrr_amount"].sum(), 4)
                if known["mrr_amount"].sum()
                else 0.0
            ),
            "median_inactive_days_of_dormant": (
                float(dormant.loc[~dormant["last_usage_date"].isna(), "inactive_days"].median())
                if (~dormant["last_usage_date"].isna()).any()
                else None
            ),
        },
        definitions=LOW_ACTIVITY_DEFINITIONS,
        caveats=[
            "「低活跃」按功能使用记录判定，不等于流失：账户可能仍在付费但未使用该功能模块。",
            "MRR 取 as_of 所属月份，按胜出订阅规则去重（start_date 最新 > mrr 最高 > id 最大）。",
            "从未使用过的账户单独计数，其运营动作与「用了后来停了」不同，不应混为一谈。",
            "T1 的使用记录与订阅时间轴存在不连贯（详见 docs/data_quality_report.md Q-23），"
            "inactive_days 的绝对值偏大，宜看相对排序而非绝对天数。",
        ],
        breakdown=sample,
    )


# ---------------------------------------------------------------------------
# 场景注册表：LLM 只能从这里选，不能自定义
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, dict[str, Any]] = {
    "activation_diagnosis": {
        "func": get_activation_metrics,
        "label": "新客激活诊断",
        "required": ["start_date", "end_date"],
        "optional": {"window_days": 7},
        "question_hint": "新客为什么没有完成激活",
    },
    "feature_adoption_diagnosis": {
        "func": get_feature_adoption_metrics,
        "label": "AI 功能采用诊断",
        "required": ["start_date", "end_date"],
        "optional": {"source": "simulated"},
        "question_hint": "AI 功能采用率为什么下降",
    },
    "low_activity_customer": {
        "func": get_low_activity_accounts,
        "label": "低活跃客户运营",
        "required": ["as_of_date"],
        "optional": {"inactive_days": 30, "top_n": 10},
        "question_hint": "哪些客户快睡着了，怎么救",
    },
}


def run_scenario(scenario: str, **params: Any) -> dict[str, Any]:
    """按场景名执行白名单函数。未知场景直接拒绝，不做模糊匹配。"""
    if scenario not in SCENARIOS:
        raise ParamError(f"未知场景 {scenario!r}。可选：{', '.join(SCENARIOS)}")

    spec = SCENARIOS[scenario]
    missing = [k for k in spec["required"] if k not in params]
    if missing:
        raise ParamError(f"场景 {scenario} 缺少必填参数：{missing}")

    allowed = set(spec["required"]) | set(spec["optional"])
    unexpected = [k for k in params if k not in allowed]
    if unexpected:
        raise ParamError(f"场景 {scenario} 不接受参数 {unexpected}，允许：{sorted(allowed)}")

    merged = {**spec["optional"], **params}
    return spec["func"](**merged)
