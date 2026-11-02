"""审计层测试：这组测试的价值在于证明护栏"真的会响"。

如果只测"合法输入能通过"，那么一个永远返回 ok=True 的空实现也能过关。
因此每个正向用例都配一个反向用例：同样的句式，只改数字，必须被判违规。
"""

from __future__ import annotations

import pytest

from src.rag import audit

PAYLOAD = {
    "scenario": "activation_diagnosis",
    "period": {"start": "2024-07-01", "end": "2024-09-30", "as_of": "2024-12-31"},
    "metrics": {
        "conversion_rate": 0.4306,
        "converted": 31,
        "eligible_size": 72,
        "previous_period": {"conversion_rate": 0.3077, "rate_delta_pp": 12.29},
        "mrr_at_risk": 174342,
    },
    "caveats": ["观察期 2023-01-01 ~ 2024-12-31"],
}


@pytest.fixture(scope="module")
def allowed():
    return audit.collect_allowed_values(PAYLOAD)


class TestCollectAllowed:
    def test_includes_raw_and_derived_percentage(self, allowed):
        values, _ = allowed
        assert 0.4306 in values
        # 0.4306 必须派生出 43.06，否则"写成百分比"会被误判为编造
        assert any(abs(v - 43.06) < 1e-6 for v in values)

    def test_includes_absolute_value_for_negative(self, allowed):
        values, _ = audit.collect_allowed_values({"delta": -12.29})
        assert any(abs(v - 12.29) < 1e-6 for v in values)

    def test_includes_date_components(self, allowed):
        """中文日期"2024年12月31日"会被拆成三个数字，分量必须放行。"""
        values, dates = allowed
        assert "2024-12-31" in dates
        assert 2024.0 in values and 12.0 in values and 31.0 in values


class TestNumberAudit:
    def test_legitimate_percentage_passes(self, allowed):
        result = audit.audit_numbers("激活率为 43.06%，高于上期的 30.77%。", allowed)
        assert result.ok, result.summary()

    def test_legitimate_delta_passes(self, allowed):
        result = audit.audit_numbers("环比提升 12.29 个百分点。", allowed)
        assert result.ok, result.summary()

    def test_three_decimal_conversion_passes(self):
        """0.09176 写成 9.176% 是正确的，不能因小数位数不同而误报。"""
        values, dates = audit.collect_allowed_values({"rate": 0.09176})
        result = audit.audit_numbers("采用率为 9.176%。", (values, dates))
        assert result.ok, result.summary()

    def test_fabricated_percentage_is_caught(self, allowed):
        """核心反向用例：编一个像模像样的数字必须被拦下。"""
        result = audit.audit_numbers("上季度激活率仅 23.4%，本季度明显提升。", allowed)
        assert not result.ok
        assert any(v["number"] == "23.4" for v in result.violations)

    def test_fabricated_forecast_is_caught(self, allowed):
        """运营建议里最容易夹带的"预期提升 X 个百分点"。"""
        result = audit.audit_numbers("预计可提升 18.7 个百分点。", allowed)
        assert not result.ok

    def test_rounding_distortion_is_caught(self, allowed):
        """43.06 写成 43.1 属于失真，不放过。

        容差存在的目的是容纳"0.09176 → 9.176%"这种位数换算，
        不是允许四舍五入改写原值。
        """
        result = audit.audit_numbers("激活率约 43.1%。", allowed)
        assert not result.ok

    def test_thousands_separator_fabrication_is_caught(self, allowed):
        result = audit.audit_numbers("MRR 为 1,286,108 元。", allowed)
        assert not result.ok

    def test_thousands_separator_legitimate_passes(self):
        """指标里存 174342，正文写 174,342 是同一个数，必须放行。"""
        values, dates = audit.collect_allowed_values({"mrr": 174342})
        result = audit.audit_numbers("MRR 合计 174,342 元。", (values, dates))
        assert result.ok, result.summary()

    def test_structural_numbers_allowed(self, allowed):
        """章节序号这类结构性数字不应触发误报。"""
        result = audit.audit_numbers("见第 7 段与第 8 段。", allowed)
        assert result.ok

    def test_violation_includes_nearest_allowed(self, allowed):
        """违规项要给出最接近的合法值，重试反馈才可操作。"""
        result = audit.audit_numbers("采用率 8.555%。", allowed)
        assert not result.ok
        assert result.violations[0].get("nearest_allowed")


class TestCitationAudit:
    class _Chunk:
        def __init__(self, source_file: str, section: str):
            self.source_file = source_file
            self.section = section

    @pytest.fixture
    def chunks(self):
        return [
            self._Chunk("knowledge_base/03_activation_funnel.md", "同期群分析的另一处限制"),
            self._Chunk("knowledge_base/06_data_quality_limits.md", "七项不可计算的指标"),
        ]

    def test_valid_citation_passes(self, chunks):
        result = audit.audit_citations(
            [{"document": "03_activation_funnel.md", "section": "同期群分析的另一处限制"}], chunks
        )
        assert result.ok, result.summary()

    def test_citation_with_path_prefix_passes(self, chunks):
        """模型有时会带上目录前缀，应视为同一文档。"""
        result = audit.audit_citations(
            [{"document": "knowledge_base/03_activation_funnel.md", "section": "同期群分析的另一处限制"}],
            chunks,
        )
        assert result.ok

    def test_invented_document_is_caught(self, chunks):
        """编一个看起来完全合理的文件名，也必须判违规。"""
        result = audit.audit_citations(
            [{"document": "13_growth_hacking_playbook.md", "section": "北极星指标"}], chunks
        )
        assert not result.ok

    def test_invented_section_is_caught(self, chunks):
        """文档对但章节瞎编，同样违规。"""
        result = audit.audit_citations(
            [{"document": "03_activation_funnel.md", "section": "完全无关的章节名称"}], chunks
        )
        assert not result.ok

    def test_empty_citations_is_not_ok(self, chunks):
        """一条引用都不给，不能算"通过"——报告的价值就在于可追溯。"""
        result = audit.audit_citations([], chunks)
        assert not result.ok


class TestAuditReport:
    def test_detects_fabrication_in_nested_fields(self):
        chunks = [TestCitationAudit._Chunk("knowledge_base/01_metric_definitions.md", "激活率")]
        report = {
            "question_overview": "分析激活情况",
            "data_facts": ["激活率 43.06%"],
            "hypotheses": [{"hypothesis": "引导太长", "basis": "转化 43.06%", "confidence": "中"}],
            "target_segments": ["新注册账户"],
            "recommended_actions": [{"action": "缩短步骤", "target": "新客", "expected_effect": "提升 25%"}],
            "limitations": [],
            "knowledge_citations": [{"document": "01_metric_definitions.md", "section": "激活率"}],
        }
        result = audit.audit_report(report, PAYLOAD, chunks)
        # 25% 不在指标里，必须被抓到
        assert not result["numbers_ok"]
        assert any(v["number"] == "25" for v in result["number_violations"])

    def test_clean_report_passes_both(self):
        chunks = [TestCitationAudit._Chunk("knowledge_base/01_metric_definitions.md", "激活率")]
        report = {
            "question_overview": "分析激活情况",
            "data_facts": ["激活率 43.06%"],
            "hypotheses": [],
            "target_segments": ["新注册账户"],
            "recommended_actions": [],
            "limitations": [],
            "knowledge_citations": [{"document": "01_metric_definitions.md", "section": "激活率"}],
        }
        result = audit.audit_report(report, PAYLOAD, chunks)
        assert result["numbers_ok"]
        assert result["citations_ok"]
