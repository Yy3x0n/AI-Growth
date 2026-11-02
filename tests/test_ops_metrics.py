"""白名单指标函数的测试：参数校验与口径正确性。

这里不测"数字好不好看"，只测两件事：
1. 非法参数必须被拒绝（这是"模型不能拼 SQL"的最后一道闸）。
2. 分母口径必须正确（右删失、去重规则），因为这类错误不会报错、只会让结论悄悄偏移。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import config, ops_metrics


class TestParamValidation:
    def test_rejects_bad_date_format(self):
        with pytest.raises(ops_metrics.ParamError, match="格式错误"):
            ops_metrics.run_scenario(
                "activation_diagnosis", start_date="2024/01/01", end_date="2024-03-01"
            )

    def test_rejects_reversed_range(self):
        with pytest.raises(ops_metrics.ParamError, match="不能晚于"):
            ops_metrics.run_scenario(
                "activation_diagnosis", start_date="2024-06-01", end_date="2024-01-01"
            )

    def test_rejects_out_of_observation_window(self):
        with pytest.raises(ops_metrics.ParamError, match="超出观察期|无数据"):
            ops_metrics.run_scenario("low_activity_customer", as_of_date="2026-01-01")

    def test_rejects_unknown_scenario(self):
        """未知场景必须直接拒绝，不能模糊匹配到某个默认场景。"""
        with pytest.raises(ops_metrics.ParamError, match="未知场景"):
            ops_metrics.run_scenario("drop_all_tables", start_date="2024-01-01", end_date="2024-03-01")

    def test_rejects_extra_params(self):
        """多传参数（例如模型试图塞 SQL）必须报错，而不是被静默忽略。"""
        with pytest.raises(ops_metrics.ParamError, match="不接受参数"):
            ops_metrics.run_scenario(
                "activation_diagnosis",
                start_date="2024-01-01",
                end_date="2024-03-01",
                sql="SELECT * FROM accounts",
            )

    def test_rejects_missing_required(self):
        with pytest.raises(ops_metrics.ParamError, match="缺少必填参数"):
            ops_metrics.run_scenario("activation_diagnosis", start_date="2024-01-01")

    def test_rejects_window_out_of_bounds(self):
        with pytest.raises(ops_metrics.ParamError, match="window_days"):
            ops_metrics.run_scenario(
                "activation_diagnosis",
                start_date="2024-01-01",
                end_date="2024-03-01",
                window_days=365,
            )

    def test_rejects_bad_source(self):
        with pytest.raises(ops_metrics.ParamError, match="source"):
            ops_metrics.run_scenario(
                "feature_adoption_diagnosis",
                start_date="2024-10-01",
                end_date="2024-12-31",
                source="production",
            )


class TestActivationMetrics:
    """激活口径的关键在于右删失处理，算错不会报错但结论会偏。"""

    def test_censored_accounts_excluded_from_denominator(self):
        """临近数据截止日注册的账户观察不足，不能算进分母。"""
        # 2024-12-25 注册、窗口 30 天 → 截止 2024-12-31 只有 6 天，必然被删失
        result = ops_metrics.run_scenario(
            "activation_diagnosis",
            start_date="2024-12-20",
            end_date="2024-12-31",
            window_days=30,
        )
        metrics = result["metrics"]
        assert metrics["censored_size"] > 0
        assert metrics["eligible_size"] == metrics["cohort_size"] - metrics["censored_size"]
        assert metrics["eligible_size"] < metrics["cohort_size"]

    def test_fully_observed_cohort_has_no_censoring(self):
        """距今足够久的区间不应有删失。"""
        result = ops_metrics.run_scenario(
            "activation_diagnosis",
            start_date="2024-01-01",
            end_date="2024-03-31",
            window_days=7,
        )
        assert result["metrics"]["censored_size"] == 0

    def test_conversion_rate_matches_counts(self):
        result = ops_metrics.run_scenario(
            "activation_diagnosis",
            start_date="2024-07-01",
            end_date="2024-09-30",
            window_days=7,
        )
        m = result["metrics"]
        assert m["converted"] <= m["eligible_size"]
        expected = round(m["converted"] / m["eligible_size"], 4)
        assert abs(m["conversion_rate"] - expected) < 1e-9

    def test_never_subscribed_plus_late_plus_converted_equals_eligible(self):
        """三类互斥且完备，否则说明判定条件写漏了。"""
        m = ops_metrics.run_scenario(
            "activation_diagnosis",
            start_date="2024-07-01",
            end_date="2024-09-30",
            window_days=7,
        )["metrics"]
        total = m["converted"] + m["never_subscribed"] + m["subscribed_but_late"]
        assert total == m["eligible_size"]

    def test_result_carries_definitions_and_caveats(self):
        """只返回数字的接口会诱使模型自行解释口径，必须带上口径与限制。"""
        result = ops_metrics.run_scenario(
            "activation_diagnosis", start_date="2024-01-01", end_date="2024-03-31"
        )
        assert result["data_source"] == "real"
        assert result["definitions"]
        assert result["caveats"]
        assert any("注册→首次付费订阅" in c for c in result["caveats"])


class TestFeatureAdoption:
    def test_simulated_source_default(self):
        result = ops_metrics.run_scenario(
            "feature_adoption_diagnosis", start_date="2024-10-01", end_date="2024-12-31"
        )
        assert result["data_source"] == "simulated"
        assert result["metrics"]["feature_count"] == 3
        names = {row["feature_name"] for row in result["breakdown"]}
        assert names == {"AI 问答", "文档总结", "AI 写作"}

    def test_adoption_rate_is_account_level(self):
        """采纳率必须是"用过该功能的账户数 / 活跃账户数"，不是请求数占比。"""
        result = ops_metrics.run_scenario(
            "feature_adoption_diagnosis", start_date="2024-10-01", end_date="2024-12-31"
        )
        active = result["metrics"]["active_accounts"]
        for row in result["breakdown"]:
            assert abs(row["adoption_rate"] - round(row["accounts"] / active, 4)) < 1e-6

    def test_real_source_uses_numbered_features(self):
        """T1 的功能名是编号，不能解释成 AI 能力。"""
        result = ops_metrics.run_scenario(
            "feature_adoption_diagnosis",
            start_date="2024-10-01",
            end_date="2024-12-31",
            source="real",
        )
        assert result["data_source"] == "real"
        assert all(row["feature_name"].startswith("feature_") for row in result["breakdown"])
        assert any("不得解释为具体 AI 能力" in c for c in result["caveats"])


class TestLowActivity:
    def test_counts_are_consistent(self):
        result = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", inactive_days=30
        )
        m = result["metrics"]
        assert m["dormant_accounts"] <= m["known_accounts"]
        assert abs(m["dormant_share"] - round(m["dormant_accounts"] / m["known_accounts"], 4)) < 1e-6
        assert m["mrr_at_risk"] <= m["mrr_total"]

    def test_never_used_accounts_are_separately_counted(self):
        """从未使用与"用了后来停了"运营动作不同，不能合并。"""
        result = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", inactive_days=30
        )
        assert "never_used_accounts" in result["metrics"]

    def test_threshold_affects_result_monotonically(self):
        """阈值放宽（天数变小）时，沉睡账户数不应减少。"""
        loose = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", inactive_days=7
        )["metrics"]["dormant_accounts"]
        strict = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", inactive_days=90
        )["metrics"]["dormant_accounts"]
        assert loose >= strict

    def test_top_n_limits_sample_size(self):
        result = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", top_n=3
        )
        assert len(result["breakdown"]) == 3

    def test_sample_sorted_by_mrr_desc(self):
        """清单按收入风险排序，否则运营无法决定先救谁。"""
        sample = ops_metrics.run_scenario(
            "low_activity_customer", as_of_date="2024-12-31", top_n=10
        )["breakdown"]
        mrrs = [row["mrr_amount"] for row in sample]
        assert mrrs == sorted(mrrs, reverse=True)
