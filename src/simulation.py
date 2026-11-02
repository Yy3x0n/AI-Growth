"""模拟数据生成（T2 AI 请求日志 / T3 A/B 实验）。

设计原则

1. **能锚定在真实数据上的字段，一律不用随机数造。**
   AI 请求的发生时间不是凭空撒的：先用 T1 的 `subscriptions` 展开出「账户-月」活跃网格，
   只有真实订阅有效的月份才可能产生 AI 请求。这样得到的账户生命周期与真实付费行为一致，
   而不是像 T1 的 `feature_usage` 那样与订阅脱节（见 docs/data_quality_report.md Q-23）。

2. **只补数据里缺的那一层，不改写已有结论。**
   T1 没有任何 AI 语义字段（`feature_1..40`），所以 AI 功能维度只能由模拟层提供，
   且必须带 `data_source = "simulated"` 列，禁止与真实数据混算。

3. **参数全部来自 config，函数只读。**
   调参等于换实验设计，必须改 config 并在文档留痕，不允许在脚本里临时改数字。

4. **随机流互相独立。**
   用 `SeedSequence` 按流名派生，改 AI 日志的生成逻辑不会移动 A/B 实验的随机数，
   反之亦然。否则每改一次模拟就要重跑全部下游分析。
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from src import config, data_loader

# 模拟故障窗口里的错误类型分布：故障期以超时为主
ERROR_TYPES = ("timeout", "rate_limit", "model_error")
ERROR_TYPE_PROBS = (0.55, 0.25, 0.20)


def _rng(stream: str) -> np.random.Generator:
    """按流名派生独立随机数发生器。

    用 crc32 而不是内置 hash()：后者受 PYTHONHASHSEED 随机化影响，
    同一份代码两次运行会得到不同结果，种子就失去意义了。
    """
    seq = np.random.SeedSequence([config.RANDOM_SEED, zlib.crc32(stream.encode("utf-8"))])
    return np.random.default_rng(seq)


# ---------------------------------------------------------------------------
# 真实订阅 → 可产生 AI 请求的「账户-月」网格
# ---------------------------------------------------------------------------


def active_account_months(
    subscriptions: pd.DataFrame | None = None,
    churn_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """由真实订阅与真实流失事件展开「账户-月」活跃网格。

    两条判定叠加，缺一不可：

    1. **付费状态**：某订阅覆盖当月 15 日。取 15 日而非月初/月末，避免
       "订阅在月末开始"被算成整月可用。
    2. **流失截断**：账户在真实 `churn_events` 中的最后流失日之后不再产生请求。
       这一步不能省——`subscriptions.end_date` 有 4,514/5,000 为空，若统一填成
       观察期末，活跃账户曲线会只涨不跌，等于把流失抹掉。

    复活记录（61 行 `is_reactivation`）按"最终流失日"处理：中间流失后又回来的账户
    保持连续，避免把复活前的月份误切成两段。

    返回列：account_id, month（月初）, first_active_month
    """
    if subscriptions is None:
        subscriptions = data_loader.load_raw("subscriptions")
    if churn_events is None:
        churn_events = data_loader.load_raw("churn_events")

    start = pd.Timestamp(config.OBSERVATION_START)
    end = pd.Timestamp(config.OBSERVATION_END)

    # 最终流失日：取最大值而非最小值，理由见上（复活记录）
    churn_last = (
        churn_events.groupby("account_id")["churn_date"].max()
        if not churn_events.empty
        else pd.Series(dtype="datetime64[ns]")
    )

    sub = subscriptions[["account_id", "start_date", "end_date"]].copy()
    # 未填结束日期 = 至今有效，截到观察期末
    sub["end_date"] = sub["end_date"].fillna(end)
    sub["start_date"] = sub["start_date"].clip(lower=start)
    sub["end_date"] = sub["end_date"].clip(upper=end)
    # 流失日早于观察期末的，把订阅可覆盖终点收到流失日
    sub["end_date"] = [
        min(e, churn_last[a]) if a in churn_last.index else e
        for a, e in zip(sub["account_id"], sub["end_date"])
    ]
    sub = sub[sub["start_date"] <= sub["end_date"]]

    rows: list[pd.DataFrame] = []
    for account_id, s, e in zip(sub["account_id"], sub["start_date"], sub["end_date"]):
        months = pd.period_range(s.to_period("M"), e.to_period("M"), freq="M")
        if len(months) == 0:
            continue
        # 逐条订阅独立判定：同一账户的多条订阅必须各自展开再合并，
        # 不能按 account_id 取一条代表——那样会漏掉其他订阅覆盖的月份。
        day15 = months.to_timestamp() + pd.Timedelta(days=14)
        covered = (day15 >= s) & (day15 <= e)
        if not covered.any():
            continue
        rows.append(
            pd.DataFrame({"account_id": account_id, "month": day15[covered] - pd.Timedelta(days=14)})
        )

    if not rows:
        return pd.DataFrame(columns=["account_id", "month", "first_active_month"])

    grid = pd.concat(rows, ignore_index=True).drop_duplicates()
    grid["first_active_month"] = grid.groupby("account_id")["month"].transform("min")
    return grid.sort_values(["account_id", "month"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# T2：AI 请求日志
# ---------------------------------------------------------------------------


def simulate_ai_request_logs(grid: pd.DataFrame | None = None) -> pd.DataFrame:
    """生成 AI 请求日志。

    生成模型（逐层叠加，每一层都在 docs/data_simulation_method.md 写明）：

        月请求数 ~ Poisson( 分层基准 × 起步爬坡 × 日历增长 × 账户个体系数 )
        功能     ~ Categorical(config.AI_FEATURES 的 mix)
        成功率   ~ 该功能基线；落在故障窗口内的问答请求降级
        延迟     ~ LogNormal(中位, sigma)；失败请求按"快速失败"缩短
        Token    ~ LogNormal(中位, sigma)
    """
    if grid is None:
        grid = active_account_months()

    rng = _rng("ai_request_logs")
    features = list(config.AI_FEATURES)
    mix = np.array([config.AI_FEATURES[f]["mix"] for f in features])
    mix = mix / mix.sum()

    # ---- 账户级属性：是否采用 AI、使用强度分层、个体系数 ----
    accounts = np.sort(grid["account_id"].unique())
    adopted = rng.random(len(accounts)) < config.AI_ADOPTION_RATE
    adopted_accounts = set(accounts[adopted])

    tier_names = list(config.AI_USAGE_TIERS)
    tier_probs = np.array([config.AI_USAGE_TIERS[t]["prob"] for t in tier_names])
    tier_probs = tier_probs / tier_probs.sum()
    tier_of = dict(
        zip(accounts, rng.choice(tier_names, size=len(accounts), p=tier_probs))
    )
    scale_of = dict(zip(accounts, rng.uniform(0.6, 1.6, size=len(accounts))))

    base_month = pd.Timestamp(config.OBSERVATION_START)

    # ---- 逐账户-月抽请求数 ----
    g = grid[grid["account_id"].isin(adopted_accounts)].copy()
    # 起步爬坡：采用后第 1 个月只跑到 45%，第 3 个月起满负荷
    month_idx = (
        (g["month"].dt.year - g["first_active_month"].dt.year) * 12
        + (g["month"].dt.month - g["first_active_month"].dt.month)
    )
    ramp = np.minimum(1.0, 0.45 + 0.275 * month_idx.to_numpy())
    calendar_idx = (g["month"].dt.year - base_month.year) * 12 + (
        g["month"].dt.month - base_month.month
    )
    growth = np.power(config.AI_MONTHLY_GROWTH, calendar_idx.to_numpy())

    tier_base = g["account_id"].map(
        lambda a: config.AI_USAGE_TIERS[tier_of[a]]["monthly_requests"]
    ).to_numpy(dtype=float)
    account_scale = g["account_id"].map(scale_of).to_numpy(dtype=float)

    lam = tier_base * ramp * growth * account_scale
    counts = rng.poisson(lam)

    g = g.assign(n=counts)
    g = g[g["n"] > 0]
    if g.empty:
        return pd.DataFrame()

    # ---- 展开成请求级 ----
    reps = np.repeat(g.index.to_numpy(), g["n"].to_numpy())
    log = g.loc[reps, ["account_id", "month"]].reset_index(drop=True)
    n_req = len(log)

    log["feature_name"] = rng.choice(features, size=n_req, p=mix)

    # 请求时刻：月内均匀；同月内排序后再编号，保证 request_id 与时间同序
    month_start = log["month"].to_numpy()
    log["request_time"] = pd.to_datetime(month_start) + pd.to_timedelta(
        rng.integers(0, 30 * 24 * 3600, size=n_req), unit="s"
    )
    log = log.sort_values("request_time").reset_index(drop=True)

    # ---- 成功率：功能基线 + 故障窗口降级 ----
    p_success = log["feature_name"].map(
        {f: config.AI_FEATURES[f]["success_rate"] for f in features}
    ).to_numpy(dtype=float)

    inc = config.SIMULATED_INCIDENT
    in_window = (
        (log["feature_name"] == inc["feature"])
        & (log["request_time"] >= pd.Timestamp(inc["start"]))
        & (log["request_time"] <= pd.Timestamp(inc["end"]) + pd.Timedelta(days=1))
    ).to_numpy()
    p_success = np.where(in_window, inc["degraded_success_rate"], p_success)

    log["status"] = np.where(rng.random(n_req) < p_success, "success", "error")
    failed = log["status"].to_numpy() == "error"
    log["error_type"] = ""
    log.loc[failed, "error_type"] = rng.choice(
        ERROR_TYPES, size=int(failed.sum()), p=ERROR_TYPE_PROBS
    )

    # ---- 延迟：对数正态；失败请求按快速失败缩短 ----
    latency = np.empty(n_req)
    for f in features:
        mask = (log["feature_name"] == f).to_numpy()
        if not mask.any():
            continue
        params = config.AI_FEATURES[f]
        latency[mask] = params["latency_median_ms"] * np.exp(
            params["latency_sigma"] * rng.standard_normal(int(mask.sum()))
        )
    latency = latency * np.where(failed, 0.35, 1.0)
    # .clip 只接受 a_min/a_max；写 lower= 会报 TypeError（numpy 2.x）
    actual_latency: np.ndarray = np.maximum(np.round(latency), 40).astype(int)
    log["latency_ms"] = actual_latency

    # ---- Token ----
    input_tokens = np.empty(n_req)
    output_tokens = np.empty(n_req)
    for f in features:
        mask = (log["feature_name"] == f).to_numpy()
        if not mask.any():
            continue
        params = config.AI_FEATURES[f]
        k = int(mask.sum())
        input_tokens[mask] = params["input_tokens_median"] * np.exp(
            0.60 * rng.standard_normal(k)
        )
        output_tokens[mask] = params["output_tokens_median"] * np.exp(
            0.55 * rng.standard_normal(k)
        )
    # 失败请求通常只消耗输入，输出接近 0
    output_tokens = np.where(failed, output_tokens * 0.15, output_tokens)
    log["input_tokens"] = np.maximum(input_tokens.round(), 1).astype(int)
    log["output_tokens"] = np.maximum(output_tokens.round(), 0).astype(int)

    log["request_id"] = [f"REQ-{i:07d}" for i in range(1, n_req + 1)]
    log["data_source"] = "simulated"

    cols = [
        "request_id", "account_id", "request_time", "feature_name", "status",
        "error_type", "latency_ms", "input_tokens", "output_tokens", "data_source",
    ]
    return log[cols].sort_values("request_time").reset_index(drop=True)


# ---------------------------------------------------------------------------
# T3：A/B 实验
# ---------------------------------------------------------------------------


def simulate_ab_test(n_arm: int) -> pd.DataFrame:
    """生成账户级 A/B 实验结果。

    account_id 为合成 ID（前缀 SIM-AB），**不取真实 ID**。原因：
    真实数据集只有 500 个账户，每臂 250 时只能检测约 47% 的相对提升（见
    docs/ab_test_report.md），无法承载一个现实规模的实验；若把真实 ID 复用两遍
    又会破坏"一个账户只能进一组"的独立性。此处是 C-13 记录的设计偏离。

    返回账户级明细，含主指标与两个护栏指标。
    """
    rng = _rng("ab_test")
    total = n_arm * 2

    ids = np.array([f"SIM-AB-{i:05d}" for i in range(1, total + 1)])
    # 先打乱再对半：完全均衡分配，SRM 按构造必然通过
    # （SRM 检验的有效性在 scripts/validate_ab_harness.py 里用人为 52/48 的分组验证过）
    perm = rng.permutation(total)
    group = np.empty(total, dtype=object)
    group[perm[:n_arm]] = "control"
    group[perm[n_arm:]] = "treatment"

    p1 = config.EXPERIMENT["baseline_rate"]
    p2 = config.EXPERIMENT["treatment_rate"]
    p = np.where(group == "control", p1, p2)
    primary = rng.random(total) < p

    # 首次有效任务的达成天数：达成了才有值，分布偏向 1~3 天
    days = np.where(primary, rng.integers(1, config.ACTIVATION_WINDOW_DAYS + 1, total), np.nan)

    df = pd.DataFrame({
        "account_id": ids,
        "group": group,
        "first_effective_task_7d": primary.astype(int),
        "days_to_first_task": days,
    })

    # 护栏 1：7 日后请求错误率（连续），按组均值抽 Beta 近似
    for name, spec in config.EXPERIMENT["guardrails"].items():
        if name == "post_7d_error_rate":
            mean = np.where(group == "control", spec["control"], spec["treatment"])
            # 用 Beta 分布保证取值落在 [0,1]，方差随均值变化（贴近真实比例型指标）
            conc = 40.0
            a = mean * conc
            b = (1 - mean) * conc
            df["post_7d_error_rate"] = rng.beta(a, b).round(4)
        elif name == "support_ticket_7d":
            prob = np.where(group == "control", spec["control"], spec["treatment"])
            df["support_ticket_7d"] = (rng.random(total) < prob).astype(int)

    df["data_source"] = "simulated"
    return df.sort_values("account_id").reset_index(drop=True)
