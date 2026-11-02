"""Embedding 与 RAG 基础组件测试。

默认会被跳过：模型权重 95.8MB，不在仓库里。先运行
    python scripts/check_rag_env.py --download
下载后这些测试才会执行。

这样处理的原因：CI 或他人 clone 后 `pytest` 不应因为缺一个大文件就整体失败，
但真装了模型之后，这些断言必须真的跑起来。
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("sentence_transformers", reason="未安装 sentence-transformers")

from src import config  # noqa: E402
from src.rag import embedding  # noqa: E402


def _model_cached() -> bool:
    try:
        embedding.resolve_model_path(allow_download=False)
        return True
    except Exception:  # noqa: BLE001 任何异常都视为"不可用"
        return False


pytestmark = pytest.mark.skipif(
    not _model_cached(),
    reason="模型未缓存，先运行 python scripts/check_rag_env.py --download",
)


class TestModelInfo:
    def test_dimension_matches_config(self):
        assert embedding.model_info()["embedding_dim"] == config.EMBEDDING_DIM

    def test_token_limit_matches_config(self):
        info = embedding.model_info()
        assert info["tokenizer_max_length"] == config.EMBEDDING_MAX_TOKENS
        assert info["max_position_embeddings"] == config.EMBEDDING_MAX_TOKENS

    def test_loaded_from_local_cache(self):
        """必须从本地路径加载，不能是 repo id——否则会回源触发限流。"""
        assert "/" in embedding.model_info()["path"]
        assert "snapshots" in embedding.model_info()["path"]


class TestEncode:
    def test_shape_and_dtype(self):
        vectors = embedding.encode(["账户激活率下降", "文档总结成本偏高"])
        assert vectors.shape == (2, config.EMBEDDING_DIM)
        assert vectors.dtype == np.float32

    def test_normalized_by_default(self):
        """归一化后余弦相似度可直接用点积，省掉一次除法。"""
        vector = embedding.encode(["新手引导步骤过长"])[0]
        assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5

    def test_empty_input(self):
        assert embedding.encode([]).shape == (0, config.EMBEDDING_DIM)

    def test_similar_texts_closer_than_unrelated(self):
        """语义检索的基本前提：同义句相似度必须高于无关句。"""
        base = "新手引导流程太长导致激活率下降"
        similar = "引导步骤过多会拉低首次任务完成率"
        unrelated = "文档总结的输入 token 体量最大"
        vectors = embedding.encode([base, similar, unrelated])
        sim_close = float(vectors[0] @ vectors[1])
        sim_far = float(vectors[0] @ vectors[2])
        assert sim_close > sim_far

    def test_deterministic(self):
        a = embedding.encode(["同一段文本"])
        b = embedding.encode(["同一段文本"])
        assert np.array_equal(a, b)


class TestTokenBudget:
    def test_ratio_near_one_for_chinese(self):
        """实测约 1.005 token/字，允许一定浮动。"""
        text = "客户成功团队需要按季度复盘账户的激活漏斗与留存表现并识别流失风险信号。" * 5
        ratio = embedding.count_tokens(text) / len(text)
        assert 0.9 < ratio < 1.3

    def test_monotonic_in_length(self):
        short = "账户激活"
        long = short * 10
        assert embedding.count_tokens(long) > embedding.count_tokens(short)

    def test_chunk_max_fits_model_limit(self):
        """回归护栏：config 的 chunk 上限一旦被调大到截断区间，这条会红。"""
        text = ("客户成功团队按季度复盘账户的激活漏斗与留存表现并识别流失风险。" * 20)[
            : config.CHUNK_MAX_CHARS
        ]
        assert embedding.count_tokens(text) <= config.EMBEDDING_MAX_TOKENS

    def test_oversized_text_detected(self):
        """超过 512 字确实会超限——这正是 C-01 要防的情况。"""
        text = "客户成功团队按季度复盘账户的激活漏斗与留存表现并识别流失风险。" * 20
        assert embedding.count_tokens(text[:600]) > config.EMBEDDING_MAX_TOKENS
