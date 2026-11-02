"""A/B 统计工具的口径测试。

重点不是覆盖率，而是锁住三件容易在重构中悄悄改变的事：
1. 样本量与 MDE 互为逆运算（改了一边不改另一边，实验设计就错了）；
2. 区间上下界的方向（把 treatment-control 写反，结论会整体反向）；
3. 决策框架的三个条件真的都会拦人。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src import ab_test


class TestSampleSize:
    def test_known_value_matches_reference(self):
        # 教科书基准：p1=0.10, p2=0.20, α=0.05, power=0.80 → 每臂约 199
        assert 190 <= ab_test.required_sample_size(0.10, 0.20) <= 205

    def test_smaller_effect_needs_more_samples(self):
        big = ab_test.required_sample_size(0.25, 0.35)
        small = ab_test.required_sample_size(0.25, 0.27)
        assert small > big * 10

    def test_project_design_parameters(self):
        # 设计文档冻结值：25% → 28.75%
        assert ab_test.required_sample_size(0.25, 0.2875) == 2193

    def test_identical_rates_rejected(self):
        with pytest.raises(ValueError):
            ab_test.required_sample_size(0.3, 0.3)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            ab_test.required_sample_size(0.0, 0.1)


class TestMde:
    def test_mde_inverts_sample_size(self):
        """MDE 与样本量必须互为逆运算，误差在 1e-4 内。"""
        n = ab_test.required_sample_size(0.25, 0.30)
        mde = ab_test.mde_for_n(n, 0.25)
        assert abs(mde - 0.05) < 1e-4

    def test_mde_shrinks_with_more_samples(self):
        assert ab_test.mde_for_n(5000, 0.25) < ab_test.mde_for_n(500, 0.25)

    def test_real_data_infeasible(self):
        """500 个真实账户每臂 250 时，只能检测约 46% 的相对提升。"""
        mde = ab_test.mde_for_n(250, 0.25)
        assert 0.10 < mde < 0.13
        assert mde / 0.25 > 0.40


class TestIntervals:
    def test_wilson_stays_in_unit_interval(self):
        for s, n in ((0, 50), (50, 50), (1, 1000), (999, 1000)):
            low, high = ab_test.wilson_interval(s, n)
            assert 0.0 <= low <= high <= 1.0

    def test_wilson_contains_point_estimate(self):
        low, high = ab_test.wilson_interval(120, 400)
        assert low < 0.30 < high

    def test_newcombe_direction(self):
        """treatment 更高时，差值区间应整体偏正。"""
        low, high = ab_test.newcombe_difference_ci(300, 1000, 200, 1000)
        assert low > 0
        assert abs((high + low) / 2 - 0.10) < 0.02

    def test_newcombe_narrows_with_more_data(self):
        narrow = ab_test.newcombe_difference_ci(3000, 10000, 2000, 10000)
        wide = ab_test.newcombe_difference_ci(30, 100, 20, 100)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


class TestSrm:
    def test_balanced_split_passes(self):
        assert not ab_test.srm_check(2193, 2193)["is_srm"]

    def test_severe_imbalance_detected(self):
        assert ab_test.srm_check(2400, 1986)["is_srm"]

    def test_ratio_reported(self):
        assert math.isclose(ab_test.srm_check(600, 400)["observed_ratio"], 0.6)


class TestProportionResult:
    def test_rates_and_lift(self):
        r = ab_test.ProportionResult("m", 250, 1000, 300, 1000)
        assert math.isclose(r.control_rate, 0.25)
        assert math.isclose(r.treatment_rate, 0.30)
        assert math.isclose(r.absolute_lift, 0.05)
        assert math.isclose(r.relative_lift, 0.20, rel_tol=1e-9)

    def test_clear_effect_significant(self):
        # 25% vs 30%，每臂 2000 应当显著
        assert ab_test.ProportionResult("m", 500, 2000, 600, 2000).is_significant

    def test_no_effect_not_significant(self):
        assert not ab_test.ProportionResult("m", 500, 2000, 505, 2000).is_significant


class TestDecision:
    def _primary(self, control_success, treatment_success, n=2193):
        return ab_test.ProportionResult("p", control_success, n, treatment_success, n)

    def test_all_conditions_met_supports_launch(self):
        # 25.1% → 29.2%，效应 4.1pp 既显著也超过设计 MDE 3.75pp
        primary = self._primary(550, 640)
        srm = ab_test.srm_check(2193, 2193)
        decision = ab_test.decide(primary, 0.0375, [{"metric": "g", "is_harm": False, "p_value": 0.5}], srm)
        assert decision["supported"], decision["reasons"]

    def test_non_significant_blocks(self):
        primary = self._primary(550, 555)
        decision = ab_test.decide(primary, 0.0375, [], ab_test.srm_check(2193, 2193))
        assert not decision["supported"]
        assert any("不显著" in r for r in decision["reasons"])

    def test_guardrail_harm_blocks_even_when_primary_wins(self):
        """这是最关键的一条：主指标达标，护栏恶化也不得上线。"""
        primary = self._primary(550, 640)
        decision = ab_test.decide(
            primary, 0.0375,
            [{"metric": "support_ticket_7d", "is_harm": True, "p_value": 0.0005}],
            ab_test.srm_check(2193, 2193),
        )
        assert not decision["supported"]
        assert any("护栏" in r for r in decision["reasons"])

    def test_srm_failure_voids_result(self):
        primary = self._primary(550, 630)
        decision = ab_test.decide(primary, 0.0375, [], ab_test.srm_check(2400, 1986))
        assert not decision["supported"]
        assert any("SRM" in r for r in decision["reasons"])

    def test_small_effect_below_mde_blocks(self):
        """显著但效应小于设计 MDE，仍不放行。

        构造：25% → 26%，差值 1pp。每臂 2 万时这个差值已经显著，
        但远小于设计 MDE 3.75pp，所以决策必须是"证据不足"。
        这正是"只看 p 值"最容易误判的一类结果。
        """
        n = 20000
        primary = ab_test.ProportionResult("p", 5000, n, 5200, n)
        decision = ab_test.decide(primary, 0.0375, [], ab_test.srm_check(n, n))
        assert primary.is_significant
        assert not decision["supported"]
        assert any("MDE" in r for r in decision["reasons"])


class TestWelch:
    def test_detects_mean_shift(self):
        rng = np.random.default_rng(0)
        a = rng.normal(0.10, 0.05, 3000)
        b = rng.normal(0.12, 0.05, 3000)
        assert ab_test.welch_test(a, b)["significant"]

    def test_no_shift_not_significant(self):
        rng = np.random.default_rng(1)
        a = rng.normal(0.10, 0.05, 3000)
        b = rng.normal(0.10, 0.05, 3000)
        assert not ab_test.welch_test(a, b)["significant"]
