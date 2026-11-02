"""模拟数据生成器的测试。

这里测的不是"数据长得像不像真的"，而是三件会影响结论正确性的事：
1. 生成必须可复现（同种子 → 同结果），否则报告里的数字无法被复核；
2. 生成必须尊重真实约束（流失后不再产生请求、时间不越界）；
3. 参数必须是"可恢复的"（用生成结果能反推出配置），否则生成器与配置脱节了。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import config, simulation


@pytest.fixture(scope="module")
def small_grid() -> pd.DataFrame:
    return simulation.active_account_months()


class TestActiveGrid:
    def test_grid_within_observation_window(self, small_grid):
        start = pd.Timestamp(config.OBSERVATION_START)
        end = pd.Timestamp(config.OBSERVATION_END)
        assert small_grid["month"].min() >= start
        assert small_grid["month"].max() <= end

    def test_grid_month_is_first_day(self, small_grid):
        assert (small_grid["month"].dt.day == 1).all()

    def test_no_duplicate_account_month(self, small_grid):
        dup = small_grid.duplicated(subset=["account_id", "month"]).sum()
        assert dup == 0, "同一账户同一月只能出现一次，否则请求量会翻倍"

    def test_first_active_month_is_min(self, small_grid):
        first = small_grid.groupby("account_id")["month"].min()
        sample = small_grid["account_id"].iloc[0]
        assert small_grid.loc[small_grid["account_id"] == sample, "first_active_month"].iloc[0] == first[sample]


class TestGridRespectsChurn:
    def test_churn_truncates_activity(self):
        """流失后不应再有活跃月——这是真实数据里最容易被忽略的约束。"""
        subs = pd.DataFrame({
            "account_id": ["ACC-1"],
            "start_date": [pd.Timestamp("2023-01-05")],
            "end_date": [pd.NaT],
        })
        churn = pd.DataFrame({
            "account_id": ["ACC-1"],
            "churn_date": [pd.Timestamp("2023-04-10")],
        })
        grid = simulation.active_account_months(subscriptions=subs, churn_events=churn)
        assert grid["month"].max() <= pd.Timestamp("2023-05-01")
        assert len(grid) <= 5

    def test_multiple_subscriptions_all_covered(self):
        """同一账户的多条订阅必须各自展开，不能只取其中一条。"""
        subs = pd.DataFrame({
            "account_id": ["ACC-2", "ACC-2"],
            "start_date": [pd.Timestamp("2023-01-10"), pd.Timestamp("2023-06-10")],
            "end_date": [pd.Timestamp("2023-02-20"), pd.Timestamp("2023-07-20")],
        })
        churn = pd.DataFrame({"account_id": [], "churn_date": []}).astype({"churn_date": "datetime64[ns]"})
        grid = simulation.active_account_months(subscriptions=subs, churn_events=churn)
        months = set(grid["month"])
        assert pd.Timestamp("2023-01-01") in months
        assert pd.Timestamp("2023-02-01") in months
        assert pd.Timestamp("2023-06-01") in months
        assert pd.Timestamp("2023-07-01") in months
        # 3~5 月两段订阅都不覆盖，不应出现
        assert pd.Timestamp("2023-04-01") not in months

    def test_subscription_not_covering_15th_excluded(self):
        """月末开始的订阅不应把整月算作活跃。"""
        subs = pd.DataFrame({
            "account_id": ["ACC-3"],
            "start_date": [pd.Timestamp("2023-03-28")],
            "end_date": [pd.Timestamp("2023-03-30")],
        })
        churn = pd.DataFrame({"account_id": [], "churn_date": []}).astype({"churn_date": "datetime64[ns]"})
        grid = simulation.active_account_months(subscriptions=subs, churn_events=churn)
        assert grid.empty


@pytest.fixture(scope="module")
def logs(small_grid):
    return simulation.simulate_ai_request_logs(small_grid)


@pytest.fixture(scope="module")
def ab():
    return simulation.simulate_ab_test(300)


class TestAiLogs:
    def test_deterministic(self, small_grid, logs):
        """同一种子必须复现同一份数据，否则报告无法被复核。"""
        again = simulation.simulate_ai_request_logs(small_grid)
        assert again.equals(logs)

    def test_required_columns(self, logs):
        expected = {
            "request_id", "account_id", "request_time", "feature_name", "status",
            "error_type", "latency_ms", "input_tokens", "output_tokens", "data_source",
        }
        assert set(logs.columns) == expected

    def test_data_source_marked(self, logs):
        assert (logs["data_source"] == "simulated").all()

    def test_features_from_config(self, logs):
        assert set(logs["feature_name"]) <= set(config.AI_FEATURES)

    def test_time_within_window(self, logs):
        assert logs["request_time"].min() >= pd.Timestamp(config.OBSERVATION_START)
        assert logs["request_time"].max() <= pd.Timestamp(config.OBSERVATION_END) + pd.Timedelta(days=1)

    def test_request_id_unique(self, logs):
        assert logs["request_id"].is_unique

    def test_error_rows_have_error_type(self, logs):
        errors = logs[logs["status"] == "error"]
        assert (errors["error_type"] != "").all()
        assert (logs[logs["status"] == "success"]["error_type"] == "").all()

    def test_positive_values(self, logs):
        assert (logs["latency_ms"] > 0).all()
        assert (logs["input_tokens"] > 0).all()
        assert (logs["output_tokens"] >= 0).all()

    def test_accounts_come_from_real_data(self, logs):
        """AI 日志的账户必须来自 T1 真实账户集合，不得自造。"""
        import pandas as pd_  # 局部导入避免与 fixture 名冲突

        real = set(pd_.read_csv(config.RAW_DIR / "ravenstack_accounts.csv")["account_id"])
        assert set(logs["account_id"]) <= real

    def test_parameter_recovery(self, logs):
        """功能占比应接近配置的 mix（容差 2 个百分点）。"""
        mix = logs["feature_name"].value_counts(normalize=True)
        for feature, spec in config.AI_FEATURES.items():
            assert abs(mix.get(feature, 0) - spec["mix"]) < 0.02


class TestAbTest:
    def test_deterministic(self, ab):
        assert simulation.simulate_ab_test(300).equals(ab)

    def test_balanced_allocation(self, ab):
        counts = ab["group"].value_counts()
        assert counts["control"] == counts["treatment"] == 300

    def test_ids_unique(self, ab):
        assert ab["account_id"].is_unique

    def test_ids_are_synthetic(self, ab):
        """实验账户为合成 ID：真实 500 个账户不足以支撑设计样本量。"""
        assert ab["account_id"].str.startswith("SIM-AB-").all()

    def test_binary_fields(self, ab):
        for col in ("first_effective_task_7d", "support_ticket_7d"):
            assert set(ab[col].unique()) <= {0, 1}

    def test_error_rate_in_unit_interval(self, ab):
        assert ab["post_7d_error_rate"].between(0, 1).all()

    def test_days_only_when_achieved(self, ab):
        achieved = ab[ab["first_effective_task_7d"] == 1]
        failed = ab[ab["first_effective_task_7d"] == 0]
        assert achieved["days_to_first_task"].notna().all()
        assert failed["days_to_first_task"].isna().all()
        assert achieved["days_to_first_task"].between(1, config.ACTIVATION_WINDOW_DAYS).all()
