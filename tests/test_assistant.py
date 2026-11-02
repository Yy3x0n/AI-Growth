"""报告生成链路测试：用"脚本化假模型"验证重试与降级行为。

为什么要假模型而不是真调 GLM：
- 真模型输出不确定，测不出"第一次违规、第二次改正"这种路径；
- 这里要验证的是**控制流**（重试是否发生、降级是否触发、审计是否会拦），
  不是模型的写作能力。模型效果另在 docs/llm_integration.md 里记录实测。
"""

from __future__ import annotations

import json

import pytest

from src import ops_metrics
from src.llm_client import LLMError, LLMResponse
from src.rag import assistant


class ScriptedClient:
    """按顺序返回预设响应；元素为异常时抛出。"""

    provider = "scripted"
    model = "scripted-v1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    @property
    def is_mock(self) -> bool:
        return False

    def chat(self, messages, json_mode=False, **kwargs):
        self.seen_messages.append(messages)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        item = self.responses[idx]
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return LLMResponse(text=text, provider=self.provider, model=self.model, latency_ms=1)


class _Chunk:
    def __init__(self, doc, section, text="片段内容"):
        self.chunk_id = f"{doc}#1"
        self.source_file = f"knowledge_base/{doc}"
        self.section = section
        self.text = text
        self.distance = 0.21


@pytest.fixture
def fixed_retrieval(monkeypatch):
    """固定检索结果，避免测试依赖向量库状态。"""

    def _retrieve(query, top_k=None, **kwargs):
        return [
            _Chunk("03_activation_funnel.md", "同期群分析的另一处限制"),
            _Chunk("06_data_quality_limits.md", "七项不可计算的指标"),
        ]

    monkeypatch.setattr(assistant.retriever, "retrieve", _retrieve)
    return _retrieve


PARAMS = {"start_date": "2024-07-01", "end_date": "2024-09-30", "window_days": 7}


def _real_metrics() -> dict:
    """取真实指标，用于构造"数字正确"的模型输出。"""
    return ops_metrics.run_scenario("activation_diagnosis", **PARAMS)["metrics"]


def _valid_payload() -> dict:
    m = _real_metrics()
    pct = m["conversion_rate"] * 100
    return {
        "question_overview": "分析 2024Q3 新客激活表现。",
        "data_facts": [
            f"激活率为 {pct:.2f}%，对应 {m['converted']} 个账户完成付费转化。",
            f"可判定样本 {m['eligible_size']} 个。",
        ],
        "knowledge_citations": [
            {"document": "03_activation_funnel.md", "section": "同期群分析的另一处限制", "point": "左截断导致队列不可比"}
        ],
        "hypotheses": [
            {"hypothesis": "引导流程过长", "basis": "激活率偏低", "confidence": "中"}
        ],
        "target_segments": ["新注册账户"],
        "recommended_actions": [
            {"action": "缩短引导步骤", "target": "新客", "expected_effect": "提升激活", "effort": "中"}
        ],
        "metrics": {"primary": "观察窗口内付费转化率", "guardrails": ["7 日内工单率"]},
        "limitations": ["观察期不足的账户已剔除"],
    }


def _fabricated_payload() -> dict:
    payload = _valid_payload()
    payload["data_facts"] = ["激活率为 99.9%，上季度仅 12.3%。"]
    return payload


class TestHappyPath:
    def test_valid_output_passes_without_retry(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert not report.degraded
        assert report.attempts == 1
        assert client.calls == 1
        assert report.audit_result["numbers_ok"]
        assert report.audit_result["citations_ok"]

    def test_report_carries_metrics_and_retrieved(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        data = report.to_dict()

        assert data["metrics"]["scenario"] == "activation_diagnosis"
        assert len(data["retrieved"]) == 2
        assert data["llm"]["provider"] == "scripted"


class TestAuditDrivenRetry:
    def test_fabrication_triggers_retry_then_succeeds(self, fixed_retrieval):
        """第一次编数字 → 带反馈重试 → 第二次改正 → 不降级。"""
        client = ScriptedClient([_fabricated_payload(), _valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert client.calls == 2, "应重试一次"
        assert report.attempts == 2
        assert not report.degraded
        assert report.warnings

    def test_retry_message_contains_specific_violation(self, fixed_retrieval):
        """重试时必须告诉模型具体哪个数字错了，否则重跑等于碰运气。"""
        client = ScriptedClient([_fabricated_payload(), _valid_payload()])
        assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        retry_messages = client.seen_messages[1]
        feedback = retry_messages[-1]["content"]
        assert "99.9" in feedback or "12.3" in feedback

    def test_persistent_fabrication_degrades(self, fixed_retrieval):
        """两次都编 → 降级为模板，且明确标注原因。"""
        client = ScriptedClient([_fabricated_payload(), _fabricated_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert client.calls == 2
        assert report.degraded
        assert "未通过" in report.degradation_reason
        assert not report.audit_result["numbers_ok"]

    def test_degraded_report_still_has_data_facts(self, fixed_retrieval):
        """降级不是空白页：仍要给出可追溯的指标与文档名。"""
        client = ScriptedClient([_fabricated_payload(), _fabricated_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert report.content["data_facts"], "降级后仍应保留指标事实"
        assert report.content["knowledge_citations"], "降级后仍应保留引用列表"
        assert report.content["hypotheses"] == [], "降级后不得生成假设"


class TestCitationGuard:
    def test_invented_document_triggers_retry(self, fixed_retrieval):
        payload = _valid_payload()
        payload["knowledge_citations"] = [
            {"document": "13_growth_hacking.md", "section": "北极星指标", "point": "编的"}
        ]
        client = ScriptedClient([payload, _valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert client.calls == 2
        assert not report.degraded


class TestFailurePaths:
    def test_llm_error_degrades_immediately(self, fixed_retrieval):
        """网络/密钥错误重试无意义，应一次失败就降级，不让用户白等。"""
        client = ScriptedClient([LLMError("HTTP 401：密钥无效")])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert report.degraded
        assert report.attempts == 1
        assert client.calls == 1
        assert "密钥无效" in report.degradation_reason

    def test_invalid_json_twice_degrades(self, fixed_retrieval):
        client = ScriptedClient(["这不是 JSON", "这也不是"])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert report.degraded
        assert "JSON" in report.degradation_reason

    def test_unknown_scenario_rejected_before_llm(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        with pytest.raises(ops_metrics.ParamError):
            assistant.generate_report("free_form_query", {}, client=client)
        assert client.calls == 0, "非法场景不得触发模型调用"


class TestRendering:
    def test_markdown_has_eight_sections_in_order(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        md = assistant.render_markdown(report)

        positions = [md.index(f"## {i+1}. {title}") for i, title in enumerate(assistant.SECTION_TITLES)]
        assert positions == sorted(positions), "8 段必须按固定顺序出现"
        assert len(assistant.SECTION_TITLES) == 8

    def test_markdown_marks_hypotheses_as_unverified(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        md = assistant.render_markdown(
            assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        )
        assert "以下为假设" in md

    def test_markdown_includes_caveats(self, fixed_retrieval):
        """指标自带的口径限制必须出现在报告正文，不能只留在 JSON 里。"""
        client = ScriptedClient([_valid_payload()])
        md = assistant.render_markdown(
            assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        )
        assert "【口径限制】" in md

    def test_degraded_banner_present(self, fixed_retrieval):
        client = ScriptedClient([_fabricated_payload(), _fabricated_payload()])
        md = assistant.render_markdown(
            assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        )
        assert "本报告为降级输出" in md


class TestRetrievalConfidence:
    def test_far_retrieval_raises_warning(self, monkeypatch):
        """知识覆盖不足必须显式提示，否则读者会以为引用很充分。"""

        def _retrieve(query, top_k=None, **kwargs):
            chunk = _Chunk("03_activation_funnel.md", "同期群分析的另一处限制")
            chunk.distance = 0.91  # 远超阈值
            return [chunk]

        monkeypatch.setattr(assistant.retriever, "retrieve", _retrieve)
        client = ScriptedClient([_valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)

        assert any("覆盖可能不足" in w for w in report.warnings)

    def test_empty_retrieval_raises_warning(self, monkeypatch):
        monkeypatch.setattr(assistant.retriever, "retrieve", lambda *a, **k: [])
        client = ScriptedClient([_valid_payload()])
        report = assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        assert any("未检索到任何知识片段" in w for w in report.warnings)


class TestPeriodFormatting:
    def test_range_with_both_ends(self):
        assert assistant._format_period({"start": "2024-07-01", "end": "2024-09-30"}) == "2024-07-01 ~ 2024-09-30"

    def test_as_of_without_start_avoids_dangling_tilde(self):
        """截点场景没有起始日，不能渲染成「 ~ 2024-12-31」。"""
        assert assistant._format_period({"as_of": "2024-12-31"}) == "截至 2024-12-31"

    def test_empty_period(self):
        assert assistant._format_period({}) == "—"


class TestPromptConstraints:
    def test_system_prompt_forbids_arithmetic(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        system = client.seen_messages[0][0]["content"]
        assert "禁止做加、减、乘、除" in system
        assert "简体中文" in system

    def test_prompt_contains_metrics_and_chunks(self, fixed_retrieval):
        client = ScriptedClient([_valid_payload()])
        assistant.generate_report("activation_diagnosis", PARAMS, client=client)
        user_msg = client.seen_messages[0][1]["content"]
        assert "指标数据" in user_msg
        assert "03_activation_funnel.md" in user_msg
        assert "```json" in user_msg
