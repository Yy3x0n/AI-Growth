"""向量库访问层：ChromaDB 持久化客户端的薄封装。

为什么单独一层：建索引脚本与检索器必须用**完全相同**的集合名、距离度量与
metadata schema。任何一处不一致都会表现为「检索不到」，而且不会报错。
把它收在一个文件里，改一处即可全局一致。
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

from src import config

COLLECTION_NAME = "knowledge_base"

# Chroma 的余弦距离 = 1 - cos_sim。写入时向量已 L2 归一化，
# 所以值域落在 [0, 2]，0 表示完全同向。
DISTANCE_SPACE = "cosine"


@lru_cache(maxsize=1)
def get_client():
    import chromadb

    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    # 关掉匿名遥测：会向外部发请求，且在有代理的环境里会拖慢启动
    from chromadb.config import Settings

    return chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def get_collection(create: bool = True):
    client = get_client()
    if create:
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": DISTANCE_SPACE},
        )
    return client.get_collection(COLLECTION_NAME)


def index_version() -> str:
    """索引版本标记：模型名 + 维度 + 构建日期。"""
    return f"{config.EMBEDDING_MODEL}@{config.EMBEDDING_DIM}d/{dt.date.today().isoformat()}"


# ---------------------------------------------------------------------------
# 索引元信息：写旁挂 JSON，不写 collection metadata
# ---------------------------------------------------------------------------
# 设计文档原本要求把 index_version 写进 collection metadata，实测做不到：
# Chroma 1.5 的 `collection.modify(metadata=...)` 有两条互相矛盾的约束——
#   ① 只传 {"index_version": ...} 会**整体替换** metadata，把 hnsw:space 丢掉；
#   ② 把 hnsw:space 一起传进去，直接抛
#      ValueError: Changing the distance function of a collection once it is created is not supported.
# 距离度量一旦被静默改回默认的 L2，检索语义会变但不会报错，属于最危险的一类问题。
# 因此改为旁挂文件记录版本，collection metadata 在创建后不再改动。

META_FILE = "index_meta.json"


def write_index_meta(extra: dict | None = None) -> dict:
    import json

    payload = {
        "collection": COLLECTION_NAME,
        "distance_space": DISTANCE_SPACE,
        "embedding_model": config.EMBEDDING_MODEL,
        "embedding_dim": config.EMBEDDING_DIM,
        "index_version": index_version(),
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        payload.update(extra)
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    (config.CHROMA_DIR / META_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def read_index_meta() -> dict:
    import json

    path = config.CHROMA_DIR / META_FILE
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def reset_collection() -> None:
    """删除并重建集合。仅在切换 embedding 模型或维度变化时使用。"""
    client = get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001 集合不存在时忽略
        pass
    client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": DISTANCE_SPACE}
    )
