"""切块器测试。

切块是 RAG 里最容易"看起来对、其实错"的一环：切歪了不会报错，
只会表现为检索质量莫名下降。因此这里测的是结构约束，不是文字内容。
"""

from __future__ import annotations

from src import config
from src.rag import chunker


class TestFrontMatter:
    def test_parses_key_values(self):
        meta, body = chunker.parse_front_matter("---\ntitle: 测试\nversion: 2\n---\n正文")
        assert meta["title"] == "测试"
        assert meta["version"] == 2  # 数字要转成 int，否则 version 比较会变成字符串比较
        assert body.strip() == "正文"

    def test_missing_front_matter_returns_raw(self):
        meta, body = chunker.parse_front_matter("# 标题\n正文")
        assert meta == {}
        assert body.startswith("# 标题")


class TestSections:
    def test_splits_by_h2(self):
        body = "# 标题\n\n## 第一节\n内容甲\n\n## 第二节\n内容乙"
        sections = chunker.split_sections(body)
        titles = [s[0] for s in sections]
        assert "第一节" in titles and "第二节" in titles
        assert all("## " not in text for _, text in sections), "H2 标题行不应残留在正文里"

    def test_h1_not_kept_in_body(self):
        sections = chunker.split_sections("# 大标题\n\n## 小节\n正文")
        assert all("大标题" not in text for _, text in sections)

    def test_no_h2_returns_single_section(self):
        sections = chunker.split_sections("# 标题\n\n只有一段正文，没有任何二级标题。")
        assert len(sections) == 1


class TestChunking:
    def test_all_chunks_within_max(self):
        for chunk in chunker.chunk_all():
            assert len(chunk.text) <= config.CHUNK_MAX_CHARS, chunk.chunk_id

    def test_no_empty_chunks(self):
        assert all(chunk.text.strip() for chunk in chunker.chunk_all())

    def test_chunk_ids_unique(self):
        ids = [c.chunk_id for c in chunker.chunk_all()]
        assert len(ids) == len(set(ids))

    def test_chunk_id_stable_across_runs(self):
        """同一次输入必须产出同样的 ID，否则幂等写入会失效。"""
        first = [c.chunk_id for c in chunker.chunk_all()]
        second = [c.chunk_id for c in chunker.chunk_all()]
        assert first == second

    def test_chunk_id_contains_doc_stem(self):
        for chunk in chunker.chunk_all():
            assert chunk.chunk_id.startswith(chunk.doc_stem)
            assert chunk.source_file.endswith(".md")

    def test_content_hash_is_16_hex(self):
        chunk = chunker.chunk_all()[0]
        assert len(chunk.content_hash) == 16
        assert all(c in "0123456789abcdef" for c in chunk.content_hash)

    def test_hash_changes_when_text_changes(self):
        doc = chunker.Document(
            stem="t", title="T", version=1, path=chunker.Path("t.md"),
            body="# T\n\n## 节\n内容甲", front_matter={},
        )
        a = chunker.chunk_document(doc)[0]
        doc2 = chunker.Document(
            stem="t", title="T", version=2, path=chunker.Path("t.md"),
            body="# T\n\n## 节\n内容乙", front_matter={},
        )
        b = chunker.chunk_document(doc2)[0]
        assert a.content_hash != b.content_hash
        assert a.chunk_id == b.chunk_id  # ID 不变，只靠 hash 判断是否需要重算

    def test_short_section_kept_whole(self):
        """短于上限的节必须整节成块，不能被切开。"""
        text = "这是一段刚好不到上限的规则说明，应当整节保留。" * 5
        doc = chunker.Document(
            stem="t", title="T", version=1, path=chunker.Path("t.md"),
            body=f"# T\n\n## 规则\n{text}", front_matter={},
        )
        chunks = chunker.chunk_document(doc)
        assert len(chunks) == 1
        assert text in chunks[0].text

    def test_long_section_split(self):
        text = "这是一条需要被切开的很长规则说明。" * 60  # 约 1000 字
        doc = chunker.Document(
            stem="t", title="T", version=1, path=chunker.Path("t.md"),
            body=f"# T\n\n## 长节\n{text}", front_matter={},
        )
        chunks = chunker.chunk_document(doc)
        assert len(chunks) > 1
        assert all(len(c.text) <= config.CHUNK_MAX_CHARS for c in chunks)

    def test_preamble_merged_into_first_section(self):
        """前言过短时并入第一节，避免产生 50 字的弱检索单元。"""
        doc = chunker.Document(
            stem="t", title="T", version=1,
            path=chunker.Path("t.md"),
            body="# T\n\n本文为模拟资料。\n\n## 第一节\n第一节的正文内容。",
            front_matter={},
        )
        chunks = chunker.chunk_document(doc)
        assert len(chunks) == 1
        assert chunks[0].section == "第一节"
        assert "本文为模拟资料" in chunks[0].text

    def test_metadata_keys(self):
        meta = chunker.chunk_all()[0].metadata()
        expected = {
            "chunk_id", "source_file", "title", "section",
            "ordinal", "content_hash", "document_version",
        }
        assert set(meta) == expected
        assert isinstance(meta["ordinal"], int)
