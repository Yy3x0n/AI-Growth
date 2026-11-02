"""检索器：向量召回 + 结果整形。

对外只暴露一个函数 `retrieve()`，返回带出处的片段。上层（诊断报告）不需要知道
向量库是 Chroma 还是别的实现。

两个刻意的设计：

1. **输出字段叫 distance，不叫 score/probability/confidence。**
   Chroma 返回的是余弦距离（0 最近，2 最远），它不是概率，也不是"相关度百分比"。
   一旦在报告里写成"置信度 0.87"，读者会以为是可信度，属于实质性误导。

2. **默认不加查询指令前缀。**
   BGE v1.5 中文版官方说明检索时无需指令。设计上保留开关，由 18 题评估集实测决定，
   而不是凭印象设定——这一点在 docs/rag_design.md 有对照数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from src import config
from src.rag import embedding, store


@dataclass
class Retrieved:
    chunk_id: str
    text: str
    distance: float
    title: str
    section: str
    source_file: str

    @property
    def citation(self) -> str:
        """引用标签，格式「文档名 §章节」，供报告正文标注出处。"""
        return f"《{self.title}》§{self.section}"


def is_low_confidence(distance: float) -> bool:
    """距离超过阈值时提示「知识库可能未覆盖」。

    只作为提示，不自动拒答：跨文档问题往往单块距离偏大但整体仍有答案，
    自动拒答会造成漏答。真正的拒答判断交给生成侧的提示词约束。
    """
    return distance > config.RAG_DISTANCE_WARN


def retrieve(
    query: str,
    top_k: int | None = None,
    instruction: str | None = None,
    max_per_doc: int | None = None,
) -> list[Retrieved]:
    """按语义召回 Top-K 片段。

    max_per_doc 限制**同一份文档**最多贡献几个片段。不加限制时，
    Top-3 很容易被同一份文档的三个相邻小节占满——实测在跨文档问题上出现过
    「期望 A+B 两份文档，实际前 3 全是 B」的情况，导致多文档证据凑不齐。

    参数语义：`None` = 用 config 默认值；`0` = 不限制。
    注意不能用 None 表示"不限制"——那样就无法表达"用配置默认值"，
    两种意图混在一个值上，会让对照实验悄悄变成同一组配置（本项目踩过一次）。
    """
    query = (query or "").strip()
    if not query:
        return []

    top_k = top_k or config.RAG_TOP_K
    if max_per_doc is None:
        max_per_doc = config.RAG_MAX_PER_DOC
    prefix = config.QUERY_INSTRUCTION if instruction is None else instruction
    vector = embedding.encode([f"{prefix}{query}"])[0].tolist()

    collection = store.get_collection(create=False)
    total = collection.count()
    # 多取一些候选，做完文档级去重后再截断到 top_k。
    # 若只取 top_k 条，去重后可能凑不满目标条数。
    fetch_n = min(top_k, total) if max_per_doc <= 0 else min(top_k * 4, total)
    result = collection.query(
        query_embeddings=[vector],
        n_results=fetch_n,
        include=["documents", "metadatas", "distances"],
    )

    candidates: list[Retrieved] = []
    for text, meta, distance in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        candidates.append(
            Retrieved(
                chunk_id=meta.get("chunk_id", ""),
                text=text,
                distance=float(distance),
                title=meta.get("title", ""),
                section=meta.get("section", ""),
                source_file=meta.get("source_file", ""),
            )
        )

    if max_per_doc <= 0:
        return candidates[:top_k]

    # 贪心：按距离升序保留，超过单文档配额就跳过
    per_doc: dict[str, int] = {}
    picked: list[Retrieved] = []
    for item in candidates:
        if per_doc.get(item.source_file, 0) >= max_per_doc:
            continue
        picked.append(item)
        per_doc[item.source_file] = per_doc.get(item.source_file, 0) + 1
        if len(picked) == top_k:
            break

    # 配额导致凑不满时，用剩余候选按原顺序补齐，保证条数不缩水
    if len(picked) < top_k:
        picked_ids = {item.chunk_id for item in picked}
        for item in candidates:
            if item.chunk_id in picked_ids:
                continue
            picked.append(item)
            if len(picked) == top_k:
                break

    return picked


def format_context(items: list[Retrieved], max_chars: int = 2400) -> str:
    """把检索结果拼成给 LLM 的上下文块，带编号与出处，便于模型逐条引用。"""
    lines: list[str] = []
    used = 0
    for index, item in enumerate(items, start=1):
        block = (
            f"[{index}] 出处：{item.citation}（距离 {item.distance:.4f}）\n"
            f"{item.text.strip()}\n"
        )
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)
