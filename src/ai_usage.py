"""AI 使用分析（模块 E）：Pandas 侧的独立实现。

存在的意义是**交叉验证**：`sql/04_ai_usage.sql` 里有一套用 SQL 写的口径，
这里有另一套用 Pandas 独立写的口径，两边逐格比对。任何一处 join 写错、
或者 P95 的取法不一致，都会在比对时暴露，而不是在报告里埋一个错数字。

三个必须对齐的口径细节：

1. **P95 用最近秩法**（rank = ceil(0.95n)），不用线性插值。
   MySQL 没有 PERCENTILE_CONT，SQL 侧只能用窗口排名；如果 Python 侧用默认的
   线性插值，两边会长期差 1~200 毫秒，而且差值看不出规律——这种"对不上但不报错"
   是最难查的一类问题。

2. **成本按行累加再求和**，不是"总 token × 单价"。
   单条请求的成本要先算再求和，否则 P95 之类按行筛选的结果无法复用。
   列名统一为 `estimated_cost_usd_at_assumed_price`，禁止简写成 cost。

3. **有效任务 = 成功 且 输出 token ≥ 阈值**，阈值从 config 读，不写字面量。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from src import config

AI_LOG_FILE = "ai_request_logs.csv"


def load_logs(path: Path | None = None) -> pd.DataFrame:
    """加载模拟日志并补齐派生列。"""
    path = path or (config.SIMULATED_DIR / AI_LOG_FILE)
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少模拟日志 {path}，请先运行 python scripts/simulate_data.py"
        )

    df = pd.read_csv(path, parse_dates=["request_time"])
    if (df["data_source"] != "simulated").any():
        raise ValueError("AI 日志中出现非 simulated 来源的行，拒绝与真实数据混用")

    df["month_start"] = df["request_time"].dt.to_period("M").dt.to_timestamp()
    df["is_success"] = df["status"].eq("success")
    df["is_effective_task"] = df["is_success"] & (
        df["output_tokens"] >= config.EFFECTIVE_TASK_MIN_OUTPUT_TOKENS
    )
    df["estimated_cost_usd_at_assumed_price"] = (
        df["input_tokens"] / 1_000_000 * config.PRICE_USD_PER_MILLION_TOKENS["input_cache_miss"]
        + df["output_tokens"] / 1_000_000 * config.PRICE_USD_PER_MILLION_TOKENS["output"]
    )
    return df


def p95_nearest_rank(values: np.ndarray | pd.Series) -> int:
    """最近秩法 P95：排序后取第 ceil(0.95n) 个值。

    与 sql/04_ai_usage.sql 的 `rn = CEIL(0.95 * n)` 严格对应。
    """
    arr = np.sort(np.asarray(values))
    n = len(arr)
    if n == 0:
        return 0
    rank = math.ceil(0.95 * n)
    return int(arr[rank - 1])


def monthly_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for month, g in df.groupby("month_start", sort=True):
        rows.append({
            "month_start": month,
            "requests": len(g),
            "active_accounts": g["account_id"].nunique(),
            "success_requests": int(g["is_success"].sum()),
            "success_rate": round(g["is_success"].mean(), 6),
            "avg_latency_ms": round(g["latency_ms"].mean(), 1),
            "p95_latency_ms": p95_nearest_rank(g["latency_ms"]),
            "input_tokens": int(g["input_tokens"].sum()),
            "output_tokens": int(g["output_tokens"].sum()),
            "effective_tasks": int(g["is_effective_task"].sum()),
            "estimated_cost_usd_at_assumed_price": round(
                g["estimated_cost_usd_at_assumed_price"].sum(), 4
            ),
        })
    return pd.DataFrame(rows)


def feature_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature, g in df.groupby("feature_name", sort=True):
        rows.append({
            "feature_name": feature,
            "requests": len(g),
            "accounts": g["account_id"].nunique(),
            "success_rate": round(g["is_success"].mean(), 6),
            "avg_latency_ms": round(g["latency_ms"].mean(), 1),
            "p95_latency_ms": p95_nearest_rank(g["latency_ms"]),
            "avg_input_tokens": round(g["input_tokens"].mean(), 1),
            "avg_output_tokens": round(g["output_tokens"].mean(), 1),
            "estimated_cost_usd_at_assumed_price": round(
                g["estimated_cost_usd_at_assumed_price"].sum(), 4
            ),
        })
    return pd.DataFrame(rows)


def error_type_summary(df: pd.DataFrame) -> pd.DataFrame:
    err = df[df["status"] == "error"]
    return (
        err.groupby(["month_start", "feature_name", "error_type"])
        .size()
        .reset_index(name="requests")
        .sort_values(["month_start", "feature_name", "error_type"])
        .reset_index(drop=True)
    )


def account_month(df: pd.DataFrame) -> pd.DataFrame:
    agg = (
        df.groupby(["account_id", "month_start"])
        .agg(
            requests=("request_id", "size"),
            success_rate=("is_success", "mean"),
            effective_tasks=("is_effective_task", "sum"),
            estimated_cost_usd_at_assumed_price=("estimated_cost_usd_at_assumed_price", "sum"),
        )
        .reset_index()
    )
    agg["success_rate"] = agg["success_rate"].round(6)
    agg["estimated_cost_usd_at_assumed_price"] = agg[
        "estimated_cost_usd_at_assumed_price"
    ].round(4)
    return agg.sort_values(["account_id", "month_start"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 分析维度（这些是"结论"，不是"口径"，所以只在 Pandas 侧做，不与 SQL 对账）
# ---------------------------------------------------------------------------


def usage_concentration(df: pd.DataFrame) -> pd.DataFrame:
    """账户请求量集中度：多少比例的账户贡献了多少请求。

    为什么值得看：AI 成本由少数重度账户决定，如果 20% 账户贡献 70% 请求，
    那么"按账户限额"和"按请求限额"的运营含义完全不同。
    """
    totals = df.groupby("account_id").size().sort_values(ascending=False)
    cum = totals.cumsum() / totals.sum()
    rows = []
    for pct in (0.1, 0.2, 0.3, 0.5):
        k = max(1, int(round(len(totals) * pct)))
        rows.append({
            "account_share": f"{pct:.0%}",
            "accounts": k,
            "request_share": round(float(cum.iloc[k - 1]), 4),
        })
    return pd.DataFrame(rows)


def incident_detection(df: pd.DataFrame) -> pd.DataFrame:
    """按「功能 × 日」计算成功率，标出显著低于该功能基线的工作日。

    判定：日成功率 < 该功能全期均值 - 3 个二项标准误，且当日请求数 ≥ 20。
    这不是要"复现"预设的故障窗口，而是检验一套能自动发现异常的规则——
    否则监控就只是把已知答案抄一遍。
    """
    daily = (
        df.groupby(["feature_name", df["request_time"].dt.date])
        .agg(requests=("request_id", "size"), success=("is_success", "sum"))
        .reset_index()
        .rename(columns={"request_time": "day"})
    )
    baseline = df.groupby("feature_name")["is_success"].mean().to_dict()

    rows = []
    for _, r in daily.iterrows():
        base = baseline[r["feature_name"]]
        n = int(r["requests"])
        if n < 20:
            continue
        se = math.sqrt(base * (1 - base) / n)
        rate = r["success"] / n
        rows.append({
            "day": r["day"],
            "feature_name": r["feature_name"],
            "requests": n,
            "success_rate": round(rate, 4),
            "baseline_rate": round(base, 4),
            "sigma_below": round((base - rate) / se, 2) if se > 0 else 0.0,
            "is_anomaly": bool(rate < base - 3 * se),
        })
    out = pd.DataFrame(rows)
    return out.sort_values("sigma_below", ascending=False).reset_index(drop=True)
