"""数字审计与引用校验 —— 报告可信度的实际执行者。

为什么需要这一层

让 LLM 写运营报告，最大的风险不是文笔差，而是**它会把数字编得很像真的**。
一次 25.17% → 26.86% 的对比，模型完全可能写成"提升 1.69 个百分点"（正确），
也可能写成"提升了 6.7%"（把绝对差当成相对差），还可能凭空补一个"上季度 23.4%"。
这三种错误在中文里读起来毫无违和感，人工复核也极易漏掉。

所以本模块不依赖模型"自觉"，而是做事后核验：
把指标 JSON 里所有出现过的数字抽成一个白名单，再抽取正文里的每个数字，
凡不在白名单内的一律判定为编造。

已知局限（必须写清楚，否则会高估这层护栏）

- 只能查"数字是否来自数据"，**不能查"数字有没有用错地方"**。
  把 A 指标的 75 写成 B 指标的 75 依然能通过。
- 无法校验语义层面的因果误读（例如把相关说成因果，这靠提示词约束 + 人工复核）。
- 中文数词（"三成"、"两倍"）不在抽取范围内，会被漏检。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 结构性数字：章节序号、Top-N、天数单位等。这些不是"数据事实"，
# 不参与审计，否则模型每写一句"第 2 段"都要被判违规。
STRUCTURAL_NUMBERS = {float(i) for i in range(0, 13)}

# 数值比较容差：相对 5e-4。
# 为什么不用字符串精确比对：0.09176 换算成 "9.176%" 是完全正确的，
# 但字符串白名单必须穷举所有小数位数才能匹配上，实测直接导致误报。
# 容差取 5e-4 的含义是：43.06 与 43.1 相差 0.04 > 0.0215，仍会被判违规（拒绝四舍五入失真）；
# 而 9.176 与 9.18 相差 0.004 < 0.0046，视为同一数字（接受正常进位）。
REL_TOLERANCE = 5e-4
ABS_TOLERANCE = 1e-6

DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBER_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # 日期整体
    r"|-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # 千分位
    r"|-?\d+(?:\.\d+)?"  # 普通数字
)


def extract_numbers(text: str) -> set[str]:
    """抽取文本中的全部数字字面量（保留原始写法，便于报错时展示）。"""
    return set(NUMBER_TOKEN.findall(text or ""))


def _to_float(raw: str) -> float | None:
    """日期返回 None；其余转 float。"""
    if DATE_PATTERN.fullmatch(raw):
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def collect_allowed_values(payload: Any) -> tuple[set[float], set[str]]:
    """递归收集指标 JSON 中的数值与日期。

    返回 (数值集合, 日期字符串集合)。

    派生规则：
    - 0~1 之间的小数同时派生出 ×100 的百分比形态（0.4306 → 43.06）。
    - 日期串同时派生出年/月/日三个分量（"2024-12-31" → 2024, 12, 31），
      因为中文里常写成"截至2024年12月31日"，会被拆成三个数字。
    - 负数的绝对值也加入，正文说"下降 12.29"与指标里 -12.29 等价。
    """
    values: set[float] = set()
    dates: set[str] = set()

    def _add_number(value: float) -> None:
        values.add(value)
        values.add(abs(value))
        if 0 <= value <= 1:
            values.add(value * 100)

    def _walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            _add_number(float(node))
            return
        if isinstance(node, str):
            for raw in extract_numbers(node):
                if DATE_PATTERN.fullmatch(raw):
                    dates.add(raw)
                    # 中文常写成"2024年12月31日"，会被拆成三个数字，故把分量一并放行
                    year, month, day = raw.split("-")
                    values.update({float(year), float(month), float(day)})
                else:
                    number = _to_float(raw)
                    if number is not None:
                        _add_number(number)
            return
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(payload)
    return values, dates


def _matches(value: float, allowed: set[float]) -> bool:
    """容差匹配。allowed 规模在数百级，线性扫描足够快。"""
    for candidate in allowed:
        gap = abs(candidate - value)
        if gap <= ABS_TOLERANCE or gap <= REL_TOLERANCE * max(abs(value), abs(candidate), 1e-9):
            return True
    return False


@dataclass
class AuditResult:
    """一次审计的结果。violations 保留原文上下文，便于定位。"""

    ok: bool
    checked: int = 0
    violations: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"数字审计通过：核验 {self.checked} 处数字，全部可追溯到指标数据"
        items = "；".join(f"{v['number']}（{v['context']}）" for v in self.violations[:5])
        return f"数字审计失败：{len(self.violations)} 处数字在指标数据中找不到来源 —— {items}"


def audit_numbers(
    text: str,
    allowed: set[float] | tuple[set[float], set[str]],
    dates: set[str] | None = None,
    *,
    allow_structural: bool = True,
) -> AuditResult:
    """核验正文中的数字是否都能追溯到指标数据。

    allow_structural=True 时放行 0~12 这类小整数，用于容纳章节序号与"前 3 条"
    这类表述。代价是"3 个功能"这种错误用法可能漏检——这是精度与误报的取舍，
    选择偏向"少误报"，因为频繁误报会让人直接关掉这条护栏。
    """
    if isinstance(allowed, tuple):
        allowed_values, allowed_dates = allowed
    else:
        allowed_values, allowed_dates = allowed, (dates or set())

    violations: list[dict[str, str]] = []
    checked = 0

    for raw in sorted(extract_numbers(text)):
        checked += 1
        if DATE_PATTERN.fullmatch(raw):
            if raw in allowed_dates:
                continue
        else:
            value = _to_float(raw)
            if value is None:
                continue
            if _matches(value, allowed_values):
                continue
            if allow_structural and value in STRUCTURAL_NUMBERS:
                continue
            if allow_structural and abs(value) in STRUCTURAL_NUMBERS:
                continue

        idx = text.find(raw)
        context = text[max(0, idx - 12) : idx + len(raw) + 12].replace("\n", " ") if idx >= 0 else ""
        violation = {"number": raw, "context": context.strip()}

        # 给出最接近的合法值，让重试时的反馈可操作。
        # 实测小模型最常见的错误不是"凭空编"，而是"漏了一个数量级"
        # （0.8555 写成 8.555%），只告诉它"这个数字不合法"它改不对。
        value = _to_float(raw)
        if value is not None and allowed_values:
            nearest = min(allowed_values, key=lambda c: abs(c - value))
            violation["nearest_allowed"] = f"{nearest:g}"
        violations.append(violation)

    return AuditResult(ok=not violations, checked=checked, violations=violations)


@dataclass
class CitationAudit:
    ok: bool
    cited: int = 0
    invalid: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"引用校验通过：{self.cited} 条引用全部来自本次检索结果"
        items = "；".join(f"{i['document']}#{i['section']}" for i in self.invalid[:5])
        return f"引用校验失败：{len(self.invalid)} 条引用不在本次检索结果中 —— {items}"


def audit_citations(citations: list[dict[str, str]], retrieved: list[Any]) -> CitationAudit:
    """核验每条引用是否真的来自本次检索到的片段。

    这一步防的是"模型凭训练记忆编一个文档名"。它可能写出
    `09_service_escalation_sop.md` 这种看起来完全合理的文件名，
    但只要本次没检索到，就必须判违规。
    """
    if not citations:
        return CitationAudit(ok=False, cited=0, invalid=[{"document": "(空)", "section": "未给出任何引用"}])

    # 建立本次检索结果的索引：文档名（含/不含 .md）→ 该文档出现过的章节集合
    index: dict[str, set[str]] = {}
    for item in retrieved:
        source = getattr(item, "source_file", "") or ""
        stem = source.rsplit("/", 1)[-1]
        stem_no_ext = stem[:-3] if stem.endswith(".md") else stem
        section = getattr(item, "section", "") or ""
        for key in (stem, stem_no_ext):
            if key:
                index.setdefault(key, set()).add(section)

    invalid: list[dict[str, str]] = []
    for cite in citations:
        doc = str(cite.get("document", "")).strip()
        section = str(cite.get("section", "")).strip()
        matched = None
        for key in (doc, doc.rsplit("/", 1)[-1]):
            if key in index:
                matched = key
                break
        if matched is None:
            invalid.append({"document": doc, "section": section})
            continue
        # 章节名允许模型做轻微改写（加/去冒号、截断），做包含匹配即可；
        # 但若该文档有多个章节，要求至少与其中一个存在字符重叠，防止完全瞎编。
        sections = {s for s in index[matched] if s}
        if sections and section:
            if not any(_loose_match(section, s) for s in sections):
                invalid.append({"document": doc, "section": section})

    return CitationAudit(ok=not invalid, cited=len(citations), invalid=invalid)


def _loose_match(a: str, b: str) -> bool:
    """章节名宽松匹配：去空白与标点后取交集长度，避免因标点差异误判。"""
    strip = lambda s: re.sub(r"[\s:：、，,。.（）()【】\[\]|/\\-]", "", s)  # noqa: E731
    x, y = strip(a), strip(b)
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    # 字符级 Jaccard，阈值取 0.6：足够容纳"P95 延迟"↔"P95 延迟口径"这类改写
    inter = len(set(x) & set(y))
    union = len(set(x) | set(y))
    return union > 0 and inter / union >= 0.6


def audit_report(report: dict[str, Any], metrics_payload: dict[str, Any], retrieved: list[Any]) -> dict[str, Any]:
    """对整份报告做审计，返回可直接内嵌到输出的结论。"""
    allowed_values, allowed_dates = collect_allowed_values(metrics_payload)

    # 只审计"数据事实"与"建议"两类会引用数字的段落；
    # 问题概述通常复述用户提问，可能带用户自己给的数字，放行会导致漏检，
    # 但它也最可能编数字，所以一并审计，宁可多报。
    narrative_keys = [
        "question_overview",
        "data_facts",
        "hypotheses",
        "target_segments",
        "recommended_actions",
        "limitations",
    ]
    chunks: list[str] = []
    for key in narrative_keys:
        value = report.get(key)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    chunks.extend(str(v) for v in item.values() if isinstance(v, (str, int, float)))

    number_audit = audit_numbers("\n".join(chunks), (allowed_values, allowed_dates))
    citation_audit = audit_citations(report.get("knowledge_citations") or [], retrieved)

    return {
        "numbers_ok": number_audit.ok,
        "numbers_summary": number_audit.summary(),
        "numbers_checked": number_audit.checked,
        "number_violations": number_audit.violations,
        "citations_ok": citation_audit.ok,
        "citations_summary": citation_audit.summary(),
        "citations_checked": citation_audit.cited,
        "citation_violations": citation_audit.invalid,
    }
