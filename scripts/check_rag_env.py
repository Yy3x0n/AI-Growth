#!/usr/bin/env python3
"""阶段 4 前置：RAG 环境自检。

用法：
    python scripts/check_rag_env.py              # 只检查，不联网
    python scripts/check_rag_env.py --download   # 缺失时先下载模型（走镜像）
    python scripts/check_rag_env.py --json       # 输出 JSON

检查项与判定依据：
    1. 依赖可导入（torch / sentence-transformers / chromadb）
    2. 模型能**离线**加载——这是重点：按 repo id 加载会逐模块回源，
       实测撞 429 限流等 73 秒；必须验证「不触网也能加载」。
    3. 向量维度 = 512，且与 config.EMBEDDING_DIM 一致
    4. tokenizer 上限 = 512，并用实际文本测出「汉字 → token」比例，
       与 config.CHUNK_MAX_CHARS 交叉验证是否会截断
    5. ChromaDB 能建集合、写入、查询、重开后仍在
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402

RESULTS: list[dict] = []


def record(item: str, status: str, detail: str) -> None:
    RESULTS.append({"检查项": item, "结果": status, "详情": detail})


def check_imports() -> None:
    for name in ("torch", "sentence_transformers", "chromadb"):
        try:
            module = __import__(name)
            record(f"依赖 {name}", "PASS", getattr(module, "__version__", "?"))
        except ImportError as exc:
            record(f"依赖 {name}", "FAIL", f"未安装：{exc}")


def check_model(download: bool) -> None:
    from src.rag import embedding

    try:
        if download:
            path = embedding.resolve_model_path(allow_download=True)
        else:
            path = embedding.resolve_model_path(allow_download=False)
        record("模型缓存", "PASS", path)
    except FileNotFoundError as exc:
        record("模型缓存", "FAIL", str(exc))
        return

    # 关键：强制离线加载，验证"不触网也能用"
    started = time.time()
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        info = embedding.model_info()
    except Exception as exc:  # noqa: BLE001 自检脚本要兜住一切异常
        record("模型加载（离线）", "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")
        return
    finally:
        os.environ.pop("HF_HUB_OFFLINE", None)
    elapsed = time.time() - started

    record("模型加载（离线）", "PASS", f"{elapsed:.1f}s，device={info['device']}")
    record(
        "向量维度",
        "PASS" if info["embedding_dim"] == config.EMBEDDING_DIM else "FAIL",
        f"{info['embedding_dim']}（config 期望 {config.EMBEDDING_DIM}）",
    )
    record(
        "tokenizer 上限",
        "PASS" if info["tokenizer_max_length"] == config.EMBEDDING_MAX_TOKENS else "WARN",
        f"{info['tokenizer_max_length']}（config 期望 {config.EMBEDDING_MAX_TOKENS}）",
    )


def check_chunk_budget() -> None:
    """用真实 tokenizer 验证 chunk 上限是否会触发截断。"""
    from src.rag import embedding

    sample = "客户成功团队需要按季度复盘账户的激活漏斗与留存表现，并识别流失风险信号。"
    rows = []
    for chars in (300, 350, 400, config.CHUNK_MAX_CHARS, 500, 600):
        text = (sample * (chars // 38 + 2))[:chars]
        tokens = embedding.count_tokens(text)
        rows.append((chars, tokens, tokens > config.EMBEDDING_MAX_TOKENS))

    # 用最长的样本反推比例
    ratio = rows[2][1] / rows[2][0]
    safe = [c for c, t, over in rows if not over]

    record(
        "汉字→token 比例",
        "PASS",
        f"{ratio:.3f} token/字（实测；不同语料会浮动）",
    )
    over = [c for c, _, o in rows if o]
    if config.CHUNK_MAX_CHARS in over:
        record("chunk 上限", "FAIL", f"{config.CHUNK_MAX_CHARS} 字会超 {config.EMBEDDING_MAX_TOKENS} token")
    else:
        headroom = config.EMBEDDING_MAX_TOKENS - embedding.count_tokens((sample * 12)[: config.CHUNK_MAX_CHARS])
        record(
            "chunk 上限",
            "PASS",
            f"{config.CHUNK_MAX_CHARS} 字未超限，剩余 {headroom} token 余量（实测安全上限约 {max(safe)} 字）",
        )


def check_chroma() -> None:
    import chromadb

    try:
        from src.rag import embedding
    except Exception as exc:  # noqa: BLE001
        record("ChromaDB 落盘", "FAIL", f"模型不可用，跳过：{exc}")
        return

    tmp = Path(tempfile.mkdtemp(prefix="chroma_selfcheck_"))
    try:
        client = chromadb.PersistentClient(path=str(tmp))
        col = client.get_or_create_collection("selfcheck", metadata={"hnsw:space": "cosine"})
        docs = [
            "新手引导步骤过长会导致首次有效任务完成时间超过七天阈值。",
            "文档总结的单次成本是问答的三点七倍，来自输入 token 体量。",
            "P95 延迟采用最近秩法，与 SQL 排名窗口严格对应。",
        ]
        vectors = embedding.encode(docs).tolist()
        col.add(ids=[f"d{i}" for i in range(len(docs))], documents=docs, embeddings=vectors)
        query = embedding.encode(["为什么首次有效任务会延迟"])[0].tolist()
        result = col.query(query_embeddings=[query], n_results=1)
        top_distance = float(result["distances"][0][0])
        top_doc = result["documents"][0][0]

        reopened = chromadb.PersistentClient(path=str(tmp)).get_collection("selfcheck").count()
        record(
            "ChromaDB 落盘",
            "PASS" if reopened == len(docs) else "FAIL",
            f"写入 {len(docs)} 条，重开后 {reopened} 条，top1 距离 {top_distance:.4f}",
        )
        record(
            "检索可用性",
            "PASS" if top_doc == docs[0] else "WARN",
            f"top1 命中「{top_doc[:20]}…」" + ("（符合预期）" if top_doc == docs[0] else "（未命中预期文档）"),
        )
    except Exception as exc:  # noqa: BLE001
        record("ChromaDB 落盘", "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 环境自检")
    parser.add_argument("--download", action="store_true", help="模型缺失时下载（走 HF 镜像）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    print(f"HF_ENDPOINT={os.getenv('HF_ENDPOINT', config.HF_ENDPOINT)}  "
          f"HF_HUB_DISABLE_XET={os.getenv('HF_HUB_DISABLE_XET', config.HF_HUB_DISABLE_XET)}")

    check_imports()
    if any(r["结果"] == "FAIL" and r["检查项"].startswith("依赖") for r in RESULTS):
        pass
    else:
        check_model(args.download)
        if any(r["检查项"] == "模型缓存" and r["结果"] == "PASS" for r in RESULTS):
            check_chunk_budget()
            check_chroma()

    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    else:
        width = max(len(r["检查项"]) for r in RESULTS) if RESULTS else 10
        print()
        for r in RESULTS:
            mark = {"PASS": "[✓]", "WARN": "[!]", "FAIL": "[✗]"}.get(r["结果"], "[ ]")
            print(f"{mark} {r['检查项']:<{width}}  {r['详情']}")

    counts = {s: sum(1 for r in RESULTS if r["结果"] == s) for s in ("PASS", "WARN", "FAIL")}
    print(f"\n合计: PASS {counts['PASS']} / WARN {counts['WARN']} / FAIL {counts['FAIL']}")
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
