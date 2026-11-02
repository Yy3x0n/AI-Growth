"""检索器测试。

前置条件：向量库已构建（scripts/build_knowledge_base.py）且模型已缓存。
缺任一条件则整体跳过——否则换机器跑测试会直接红一片，掩盖真正的问题。
"""

from __future__ import annotations

import pytest

pytest.importorskip("sentence_transformers", reason="未安装 sentence-transformers")

from src import config  # noqa: E402
from src.rag import embedding, retriever, store  # noqa: E402


def _ready() -> bool:
    try:
        embedding.resolve_model_path(allow_download=False)
        return store.get_collection(create=False).count() > 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="需要先运行 check_rag_env.py --download 与 build_knowledge_base.py"
)


class TestRetrieve:
    def test_returns_top_k(self):
        items = retriever.retrieve("流失口径怎么定义", top_k=4)
        assert len(items) == 4

    def test_distances_ascending(self):
        """Chroma 余弦距离越小越相近，返回结果必须按距离升序。"""
        items = retriever.retrieve("AI 成本为什么高", top_k=5)
        distances = [i.distance for i in items]
        assert distances == sorted(distances)

    def test_distance_in_valid_range(self):
        for item in retriever.retrieve("新手引导流程", top_k=3):
            assert 0.0 <= item.distance <= 2.0

    def test_empty_query_returns_empty(self):
        assert retriever.retrieve("") == []
        assert retriever.retrieve("   ") == []

    def test_metadata_complete(self):
        item = retriever.retrieve("分层阈值", top_k=1)[0]
        assert item.chunk_id and item.title and item.section and item.source_file
        assert item.text.strip()

    def test_citation_format(self):
        item = retriever.retrieve("分层阈值", top_k=1)[0]
        assert item.citation.startswith("《")
        assert "§" in item.citation


class TestDocQuota:
    def test_quota_limits_chunks_per_document(self):
        items = retriever.retrieve("数据口径与指标定义", top_k=4, max_per_doc=1)
        sources = [i.source_file for i in items]
        assert len(sources) == len(set(sources)), "每文档≤1 时不应出现同文档多条"

    def test_quota_improves_doc_diversity(self):
        """这是修过的缺陷：不限额时 Top-5 可能全来自同一份文档。"""
        unlimited = retriever.retrieve("数据口径与指标定义", top_k=5, max_per_doc=0)
        limited = retriever.retrieve("数据口径与指标定义", top_k=5, max_per_doc=1)
        assert len({i.source_file for i in limited}) >= len({i.source_file for i in unlimited})

    def test_quota_still_returns_top_k(self):
        """配额不能把结果条数压少——凑不满时要补齐。"""
        assert len(retriever.retrieve("AI 成本", top_k=5, max_per_doc=1)) == 5

    def test_zero_means_unlimited(self):
        items = retriever.retrieve("AI 成本", top_k=3, max_per_doc=0)
        assert len(items) == 3


class TestConfidenceHint:
    def test_low_confidence_flag(self):
        assert retriever.is_low_confidence(config.RAG_DISTANCE_WARN + 0.01)
        assert not retriever.is_low_confidence(config.RAG_DISTANCE_WARN - 0.01)

    def test_out_of_scope_query_far(self):
        """知识库外的问题，距离应明显大于库内问题——不能返回"看起来很像"的结果还当作命中。"""
        inside = retriever.retrieve("流失三套口径用哪个", top_k=1)[0].distance
        outside = retriever.retrieve("竞争对手的产品定价是多少", top_k=1)[0].distance
        assert outside > inside


class TestFormatContext:
    def test_includes_citation_and_distance(self):
        items = retriever.retrieve("分层阈值", top_k=2)
        text = retriever.format_context(items)
        assert "[1]" in text
        assert "出处：" in text
        assert "距离" in text

    def test_respects_char_budget(self):
        items = retriever.retrieve("AI 成本", top_k=5)
        text = retriever.format_context(items, max_chars=200)
        assert len(text) <= 400  # 单块可能略微超出预算，但不允许整段全塞

    def test_empty_items(self):
        assert retriever.format_context([]) == ""
