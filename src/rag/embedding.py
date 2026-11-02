"""Embedding 模型加载与编码（本地推理，不调用任何在线 API）。

三个必须处理的现实问题，都是实测撞出来的：

1. **huggingface.co 在本机不可达**（CONNECT tunnel failed, 502）。必须走镜像。
2. **huggingface_hub 新版默认用 Xet 协议**（cas-server.xethub.hf.co），镜像不代理，
   报 401 Unauthorized。需要关掉 Xet 回落传统 HTTP。
3. **按 repo id 加载会逐模块向 hub 发 HEAD 请求**。模型已缓存时这些请求纯属浪费，
   而且会撞上镜像的 429 限流（实测等了 73 秒）。改为**先解析出本地快照路径再加载**，
   加载阶段完全不触网。

设计取舍：把"下载"和"加载"拆开。下载只在 `ensure_model()` 里发生一次，
之后 `get_model()` 永远读本地路径——`.encode()` 的调用方不必关心网络状态。
"""

from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

from src import config

# 必须在 transformers / huggingface_hub 被导入之前设置，否则不生效。
# 用 setdefault 而不是直接赋值：允许用户在 shell 里覆盖（例如临时切回官方源）。
os.environ.setdefault("HF_ENDPOINT", config.HF_ENDPOINT)
os.environ.setdefault("HF_HUB_DISABLE_XET", config.HF_HUB_DISABLE_XET)

# 只下必要文件，跳过 .bin（与 safetensors 重复，会多占 95MB）与各种示例脚本
MODEL_ALLOW_PATTERNS = ["*.json", "*.txt", "*.safetensors", "*.model", "1_Pooling/*"]


def resolve_model_path(repo_id: str | None = None, allow_download: bool = True) -> str:
    """返回本地模型目录。优先用缓存，缓存缺失且允许下载时才联网。"""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    repo_id = repo_id or config.EMBEDDING_MODEL
    try:
        return snapshot_download(repo_id, local_files_only=True, allow_patterns=MODEL_ALLOW_PATTERNS)
    except LocalEntryNotFoundError:
        if not allow_download:
            raise FileNotFoundError(
                f"本地无 {repo_id} 缓存，且调用方禁止下载。"
                f"请先运行 python scripts/check_rag_env.py --download"
            ) from None

    return snapshot_download(repo_id, allow_patterns=MODEL_ALLOW_PATTERNS, max_workers=4)


@lru_cache(maxsize=1)
def get_model():
    """加载模型（进程内只加载一次，8 秒左右，之后复用）。"""
    from sentence_transformers import SentenceTransformer

    path = resolve_model_path(allow_download=False)
    # device="cpu"：本机虽有 MPS，但 24M 参数的小模型在 CPU 上编码 344 条/秒已足够，
    # 走 MPS 反而有首次 kernel 编译开销。CPU 结果也更可复现。
    return SentenceTransformer(path, device="cpu")


def encode(texts: list[str], normalize: bool = True, batch_size: int = 32) -> np.ndarray:
    """编码为向量。

    normalize=True 时输出已 L2 归一化（范数 1.0），余弦相似度可直接用点积计算，
    不必再做除法——这是后续手写向量检索时减少一次逐元素运算的前提。
    """
    if not texts:
        return np.zeros((0, config.EMBEDDING_DIM), dtype="float32")
    return get_model().encode(
        texts, batch_size=batch_size, normalize_embeddings=normalize,
        show_progress_bar=False, convert_to_numpy=True,
    )


def count_tokens(text: str) -> int:
    """用模型自己的 tokenizer 数 token，**不靠字符数估算**。

    实测（见 docs/rag_design.md）：该 tokenizer 对中文约 1 字 = 1.005 token，
    但混入英文术语、数字、标点后比例会上浮。切块前必须实测，
    否则会重演任务书 800 字块被静默截断的问题。
    """
    model = get_model()
    # verbose=False 关掉 transformers 的「序列超过模型上限」告警：
    # 数 token 本来就会故意测超限样本，这条告警在自检里的作用是噪声。
    return len(
        model.tokenizer(text, add_special_tokens=True, truncation=False, verbose=False)["input_ids"]
    )


def model_info() -> dict:
    """模型元信息，用于环境自检与文档留痕。"""
    model = get_model()
    tok = model.tokenizer
    # sentence-transformers 6.x 把 get_sentence_embedding_dimension 改名为
    # get_embedding_dimension，旧名仍可用但会发 FutureWarning。取新名优先、旧名兜底。
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return {
        "repo_id": config.EMBEDDING_MODEL,
        "path": resolve_model_path(allow_download=False),
        "embedding_dim": getter(),
        "tokenizer_max_length": tok.model_max_length,
        "max_position_embeddings": model[0].auto_model.config.max_position_embeddings,
        "device": str(model.device),
    }
