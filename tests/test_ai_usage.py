"""AI 使用分析口径的测试。

最需要锁住的是 P95 的取法：MySQL 没有 PERCENTILE_CONT，SQL 侧只能用窗口排名，
如果 Python 侧换成默认的线性插值，两边会长期差 1~200 毫秒且看不出规律。
"""

from __future__ import annotations

import numpy as np
import pytest

from src import ai_usage, config


@pytest.fixture(scope="module")
def logs():
    """整份日志只加载一次：69k 行反复读盘会让测试从 5 秒变成 30 秒。"""
    return ai_usage.load_logs()


class TestP95:
    def test_nearest_rank_small(self):
        # n=20 → rank=19 → 第 19 小的值（下标 18）
        values = np.arange(1, 21)
        assert ai_usage.p95_nearest_rank(values) == 19

    def test_matches_numpy_without_interpolation_effect(self):
        """n=100 时 ceil(0.95*100)=95，应等于第 95 小的值。"""
        rng = np.random.default_rng(0)
        values = rng.integers(1, 10_000, 100)
        assert ai_usage.p95_nearest_rank(values) == int(np.sort(values)[94])

    def test_single_value(self):
        assert ai_usage.p95_nearest_rank(np.array([42])) == 42

    def test_empty_returns_zero(self):
        assert ai_usage.p95_nearest_rank(np.array([])) == 0

    def test_always_an_observed_value(self):
        """最近秩法的结果必须是样本中出现过的值，不能是插值出来的新值。"""
        values = np.array([10, 20, 30, 40, 50])
        assert ai_usage.p95_nearest_rank(values) in values


class TestLoadAndDerive:
    def test_month_start_is_first_day(self, logs):
        assert (logs["month_start"].dt.day == 1).all()

    def test_effective_task_rule(self, logs):
        """有效任务 = 成功 且 输出 token ≥ 阈值。"""
        manual = logs["is_success"] & (logs["output_tokens"] >= config.EFFECTIVE_TASK_MIN_OUTPUT_TOKENS)
        assert (logs["is_effective_task"] == manual).all()

    def test_failed_requests_never_effective(self, logs):
        assert not logs.loc[~logs["is_success"], "is_effective_task"].any()

    def test_cost_formula(self, logs):
        """成本必须按行算：输入与输出分别乘各自单价后相加。"""
        row = logs.iloc[0]
        expected = (
            row["input_tokens"] / 1_000_000 * config.PRICE_USD_PER_MILLION_TOKENS["input_cache_miss"]
            + row["output_tokens"] / 1_000_000 * config.PRICE_USD_PER_MILLION_TOKENS["output"]
        )
        assert abs(row["estimated_cost_usd_at_assumed_price"] - expected) < 1e-12

    def test_missing_file_raises(self):
        # 不用 tmp_path：当前执行环境对 pytest 的临时目录创建有限制，
        # 直接指向一个确定不存在的路径同样能验证异常分支。
        missing = config.SIMULATED_DIR / "__definitely_missing__.csv"
        with pytest.raises(FileNotFoundError):
            ai_usage.load_logs(missing)


class TestSummaries:
    def test_monthly_totals_match_raw(self, logs):
        monthly = ai_usage.monthly_summary(logs)
        assert monthly["requests"].sum() == len(logs)
        assert monthly["effective_tasks"].sum() == int(logs["is_effective_task"].sum())

    def test_feature_totals_match_raw(self, logs):
        feature = ai_usage.feature_summary(logs)
        assert feature["requests"].sum() == len(logs)
        assert feature["accounts"].max() <= logs["account_id"].nunique()

    def test_success_rate_bounded(self, logs):
        for frame in (ai_usage.monthly_summary(logs), ai_usage.feature_summary(logs)):
            assert frame["success_rate"].between(0, 1).all()

    def test_error_summary_only_errors(self, logs):
        errors = ai_usage.error_type_summary(logs)
        assert errors["requests"].sum() == int((logs["status"] == "error").sum())

    def test_account_month_totals(self, logs):
        acct = ai_usage.account_month(logs)
        assert acct["requests"].sum() == len(logs)
        assert not acct.duplicated(subset=["account_id", "month_start"]).any()


class TestConcentration:
    def test_shares_monotonic(self):
        logs = ai_usage.load_logs()
        conc = ai_usage.usage_concentration(logs)
        shares = conc["request_share"].tolist()
        assert shares == sorted(shares), "账户占比越大，请求占比不应下降"
        assert shares[-1] <= 1.0


class TestIncidentDetection:
    def test_flags_known_window(self):
        """预设的故障窗口应被自动检出，而不是把答案抄一遍。"""
        logs = ai_usage.load_logs()
        detected = ai_usage.incident_detection(logs)
        anomalies = detected[detected["is_anomaly"]]
        assert not anomalies.empty

        inc = config.SIMULATED_INCIDENT
        window = anomalies[
            (anomalies["feature_name"] == inc["feature"])
            & (anomalies["day"].astype(str) >= inc["start"])
            & (anomalies["day"].astype(str) <= inc["end"])
        ]
        # 故障窗口内应出现多个异常日
        assert len(window) >= 5

    def test_requires_minimum_volume(self):
        """请求过少的「功能-日」不参与判定，避免小样本噪声刷出假异常。"""
        logs = ai_usage.load_logs()
        detected = ai_usage.incident_detection(logs)
        assert (detected["requests"] >= 20).all()
