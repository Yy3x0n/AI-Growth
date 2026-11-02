#!/usr/bin/env python3
"""阶段 4：构建知识库向量索引（幂等）。

用法：
    python scripts/build_knowledge_base.py            # 增量构建（只重算变化的块）
    python scripts/build_knowledge_base.py --rebuild  # 删除集合后全量重建
    python scripts/build_knowledge_base.py --dry-run  # 只报告将做什么，不写入

幂等设计（对应设计文档 §5.3）：
    1. 按 chunk_id **upsert**，不 add；
    2. 已存在的块比对 content_hash，未变化的跳过 embedding（省时，也避免重复计算）；
    3. 集合里存在但当前文档已不存在的块，按 chunk_id 差集删除；
    4. 连续运行两次，集合计数必须不变——这是阶段 4 的验收命令。

为什么坚持记 content_hash 而不是只比文本：
    一段文本可能因为排版变化（多余空格、换行）而字面不同但语义相同，
    也可能因为改了一个数字而字面相近但语义相反。哈希比对是唯一可靠的"是否变化"判据。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402
from src.rag import chunker, embedding, store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="构建知识库向量索引")
    parser.add_argument("--rebuild", action="store_true", help="删除集合并全量重建")
    parser.add_argument("--dry-run", action="store_true", help="只报告差异，不写入")
    args = parser.parse_args()

    docs = chunker.load_documents()
    chunks = chunker.chunk_all()
    if not chunks:
        print(f"未在 {config.KNOWLEDGE_DIR} 找到任何知识文档", file=sys.stderr)
        return 1

    print(f"知识文档 {len(docs)} 份 → 切块 {len(chunks)} 个")

    # 维度自检：不一致就退出，不静默续跑（换模型后最容易出的错）
    dim = embedding.model_info()["embedding_dim"]
    if dim != config.EMBEDDING_DIM:
        print(f"向量维度 {dim} 与 config 期望的 {config.EMBEDDING_DIM} 不一致，终止", file=sys.stderr)
        return 1

    if args.rebuild:
        store.reset_collection()
        print("已清空集合，开始全量重建")

    collection = store.get_collection()
    existing = collection.get(include=["metadatas"])
    existing_ids = set(existing["ids"])
    existing_hash = {
        metadata["chunk_id"]: metadata.get("content_hash")
        for metadata in (existing["metadatas"] or [])
        if metadata
    }

    current_ids = {c.chunk_id for c in chunks}
    to_write = [c for c in chunks if existing_hash.get(c.chunk_id) != c.content_hash]
    to_delete = sorted(existing_ids - current_ids)
    unchanged = len(chunks) - len(to_write)

    print(f"  已存在 {len(existing_ids)} 块；本次需写入 {len(to_write)}（其中新增 "
          f"{len([c for c in to_write if c.chunk_id not in existing_ids])}、内容变更 "
          f"{len([c for c in to_write if c.chunk_id in existing_ids])}），未变化 {unchanged}，待删除 {len(to_delete)}")

    if args.dry_run:
        for c in to_write[:5]:
            print(f"    + {c.chunk_id}  {len(c.text)} 字  §{c.section}")
        if to_delete:
            print(f"    - 将删除 {len(to_delete)} 块，例如 {to_delete[:3]}")
        print("dry-run，未写入任何数据")
        return 0

    if to_write:
        vectors = embedding.encode([c.text for c in to_write])
        if vectors.shape[1] != config.EMBEDDING_DIM:
            print(f"编码输出维度 {vectors.shape[1]} 异常，终止", file=sys.stderr)
            return 1
        collection.upsert(
            ids=[c.chunk_id for c in to_write],
            documents=[c.text for c in to_write],
            embeddings=vectors.tolist(),
            metadatas=[c.metadata() for c in to_write],
        )
        print(f"  已写入 {len(to_write)} 块")

    if to_delete:
        collection.delete(ids=to_delete)
        print(f"  已删除 {len(to_delete)} 个过期块")

    # 索引版本写旁挂文件，不改 collection metadata。
    # 原因见 src/rag/store.py 的说明：Chroma 1.5 的 modify 会整体替换 metadata，
    # 从而丢掉 hnsw:space，把距离度量静默改回 L2。
    meta = store.write_index_meta({"chunks": len(chunks), "documents": len(docs)})

    final = collection.count()
    print(f"\n集合 {store.COLLECTION_NAME} 现有 {final} 块"
          f"（索引版本 {meta['index_version']}，距离度量 {meta['distance_space']}）")
    if final != len(chunks):
        print(f"计数与切块数不一致（{final} vs {len(chunks)}），请检查是否有残留块", file=sys.stderr)
        return 1

    # 幂等验收：再跑一次不应有任何变化
    print("验收：重复运行本脚本，集合计数应保持不变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
