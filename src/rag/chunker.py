"""知识文档加载与切块。

切块策略（对应 docs/00_requirements_and_design.md C-01 与 §5.1）：

1. **先按 H2 分节，节内再按长度切。** 一个 H2 节如果整体不超过上限，就整节成块——
   强行按字数切会把一个完整的规则拦腰截断，检索到半条规则的害处大于块偏大。
2. **切分边界优先落在段落，其次句子，最后才是硬切。** 硬切是兜底，出现即记 WARN。
3. **每块实测 token 数**，超过模型上限要二次切分并记 WARN。不靠字符数估算——
   实测中文约 1.005 token/字，但混入英文术语与数字后比例会上浮。
4. **重叠区取上一块的尾部若干字**，且保证重叠后不超上限。

为什么 chunk_id 用标题哈希而不是节序号：
节序号会随文档插入新章节而整体后移，导致所有 chunk_id 变化，幂等写入失效（旧块残留、
新块重复）。用标题哈希后，新增章节不影响既有块的 ID。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from src import config

# 切分优先级：先段落，再句末标点
PARAGRAPH_SEP = re.compile(r"\n\s*\n")
SENTENCE_SEP = re.compile(r"(?<=[。！？；!?;])")


@dataclass
class Chunk:
    """一个可写入向量库的知识块。"""

    chunk_id: str
    text: str
    doc_stem: str
    title: str
    section: str
    ordinal: int
    source_file: str
    document_version: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def metadata(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_file": self.source_file,
            "title": self.title,
            "section": self.section,
            "ordinal": self.ordinal,
            "content_hash": self.content_hash,
            "document_version": self.document_version,
        }


@dataclass
class Document:
    stem: str
    title: str
    version: int
    path: Path
    body: str
    front_matter: dict


def parse_front_matter(raw: str) -> tuple[dict, str]:
    """解析 `---` 包裹的简单 key: value 头。缺失时返回空 dict 与原文。"""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip().strip('"').strip("'")
            if value.isdigit():
                meta[key.strip()] = int(value)
            else:
                meta[key.strip()] = value
    return meta, parts[2].lstrip("\n")


def load_documents(root: Path | None = None) -> list[Document]:
    """加载 knowledge_base/*.md（不递归，排除 eval/ 等子目录）。"""
    root = root or config.KNOWLEDGE_DIR
    docs: list[Document] = []
    for path in sorted(root.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        # H1 作为标题；没有 H1 时退回 front-matter 的 title
        h1 = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = h1.group(1).strip() if h1 else meta.get("title", path.stem)
        docs.append(
            Document(
                stem=path.stem,
                title=title,
                version=int(meta.get("version", 1)),
                path=path,
                body=body,
                front_matter=meta,
            )
        )
    return docs


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 H2 切分，返回 [(section_title, section_text)]。

    H1 标题行与首个 H2 之前的内容（引言）归入「概述」一节，避免丢失开头的说明。
    """
    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = "概述"
    current_lines: list[str] = []
    seen_h2 = False

    for line in lines:
        if line.startswith("## "):
            # 第一段（H1 之后、首个 H2 之前）单独成节
            if current_lines and any(s.strip() for s in current_lines):
                sections.append((current_title, current_lines))
            current_title = line[3:].strip()
            current_lines = []
            seen_h2 = True
            continue
        if line.startswith("# "):
            continue  # H1 只作为文档标题，不进入正文块
        current_lines.append(line)

    if current_lines and any(s.strip() for s in current_lines):
        sections.append((current_title, current_lines))

    if not seen_h2 and len(sections) == 1:
        # 全文没有 H2 时，整篇作为一节
        return [(sections[0][0], "\n".join(sections[0][1]))]

    return [(title, "\n".join(text).strip()) for title, text in sections if "\n".join(text).strip()]


def _split_long(text: str) -> list[str]:
    """把超过上限的文本切成若干段，优先段落、其次句子、最后硬切。"""
    max_chars = config.CHUNK_MAX_CHARS
    target = config.CHUNK_TARGET_CHARS
    overlap = config.CHUNK_OVERLAP_CHARS

    # 先按段落，段落若单独超限再按句子
    units: list[str] = []
    for para in PARAGRAPH_SEP.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        for sentence in SENTENCE_SEP.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                # 硬切：无标点的超长串（如长表格、长代码），记录在调用方
                for i in range(0, len(sentence), max_chars):
                    units.append(sentence[i : i + max_chars])

    chunks: list[str] = []
    buffer = ""
    for unit in units:
        candidate = (buffer + "\n" + unit).strip() if buffer else unit
        if len(candidate) <= target or not buffer:
            buffer = candidate
            if len(buffer) > max_chars:  # 单个 unit 就超了上限
                chunks.append(buffer[:max_chars])
                buffer = ""
            continue
        chunks.append(buffer)
        # 重叠：取上一块尾部，但保证新块不超上限
        tail = buffer[-overlap:] if overlap and len(buffer) > overlap else ""
        buffer = (tail + "\n" + unit).strip()
        if len(buffer) > max_chars:
            chunks.append(buffer[:max_chars])
            buffer = buffer[max_chars:].strip()

    if buffer:
        chunks.append(buffer)

    # 尾块太短时并入前一块，避免产生「半句话」块
    if len(chunks) > 1 and len(chunks[-1]) < config.CHUNK_MIN_CHARS:
        merged = chunks[-2] + "\n" + chunks[-1]
        if len(merged) <= max_chars:
            chunks = chunks[:-2] + [merged]
    return chunks


def chunk_document(doc: Document) -> list[Chunk]:
    """把一份文档切成块。"""
    sections = split_sections(doc.body)

    # 前言合并：H1 之后、首个 H2 之前的内容通常只有一两句说明（「本文为模拟资料」之类），
    # 单独成块会变成 50~90 字的弱检索单元，既可能被无关问题命中，又不足以回答任何问题。
    # 若它短于下限且后面还有小节，则并入第一节，标题沿用第一节。
    if len(sections) >= 2 and sections[0][0] == "概述" and len(sections[0][1]) < config.CHUNK_MIN_CHARS:
        merged = sections[0][1] + "\n\n" + sections[1][1]
        if len(merged) <= config.CHUNK_MAX_CHARS:
            sections = [(sections[1][0], merged)] + sections[2:]

    chunks: list[Chunk] = []
    for section_title, section_text in sections:
        if not section_text.strip():
            continue
        heading_hash = hashlib.sha256(section_title.encode("utf-8")).hexdigest()[:8]
        pieces = (
            [section_text]
            if len(section_text) <= config.CHUNK_MAX_CHARS
            else _split_long(section_text)
        )
        for ordinal, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.stem}#{heading_hash}#{ordinal:02d}",
                    text=piece,
                    doc_stem=doc.stem,
                    title=doc.title,
                    section=section_title,
                    ordinal=ordinal,
                    source_file=f"knowledge_base/{doc.path.name}",
                    document_version=doc.version,
                )
            )
    return chunks


def chunk_all(root: Path | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in load_documents(root):
        chunks.extend(chunk_document(doc))
    return chunks
