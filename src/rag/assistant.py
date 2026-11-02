"""运营诊断报告生成：固定取数 + RAG 检索 + LLM 成文 + 事后审计。

与"让模型自由发挥"的区别

1. **取数不由模型决定**。场景与参数经 `ops_metrics` 白名单校验，模型拿到的是
   一份已经算好的指标 JSON，它没有数据库连接、也不能拼 SQL。
2. **成文后必须过审计**。数字要能追溯到指标 JSON，引用要能对上本次检索结果。
   不通过就先带错误反馈重试一次，仍不通过则降级为模板输出。
3. **降级不是失败**。宁可给一份"只有数据事实、没有润色"的报告，也不给一份
   读起来很顺但数字靠编的报告。降级事实会写进报告的元信息里。

三段式提示词

- system：角色 + 硬约束（只用给定数据、区分事实与假设、中文作答、只输出 JSON）
- user：问题 + 指标 JSON + 检索片段（带编号与来源）+ 输出 schema
- 重试时追加：上一版违反了哪几条，要求逐条修正
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .. import config, llm_client, ops_metrics
from ..llm_client import LLMClient, LLMError
from . import audit, retriever

# 固定 8 段。顺序与任务书 §模块 F 一致，不允许增删。
SECTION_TITLES = [
    "问题概述",
    "数据事实",
    "检索到的知识依据",
    "可能原因（假设）",
    "建议关注的目标客群",
    "推荐运营动作",
    "主指标与护栏指标",
    "数据限制与待验证事项",
]

SYSTEM_PROMPT = """你是 SaaS 产品的运营数据分析助手，为运营团队撰写诊断报告。

必须遵守的硬性规则：
1. 只能依据用户消息中提供的「指标数据」和「知识片段」作答，不得引入任何外部知识或常识补充。
2. 数字只能原样引用「指标数据」中出现过的值。**禁止做加、减、乘、除等派生计算**，
   包括差值、增长率、平均值。唯一允许的换算是：指标中的比例字段是 0~1 的小数
   （例如 0.8555），若要以百分比呈现可写成 85.55%，但不得改变数值本身。
   需要表达两期变化时，只能使用指标里已给出的 `rate_delta_pp`、`change_pp` 等差值字段；
   若指标未给出差值，就直接陈述两期原值，不要自己相减。
3. 「数据事实」只写指标数据直接支撑的内容；任何推测必须放进「可能原因」，并标注为假设。
4. 每条知识依据必须写明确切的文档名与章节名，只能引用「知识片段」中实际出现的文档。
5. 若证据不足以回答，必须在「数据限制与待验证事项」中明确指出缺什么，不得用常识填补。
6. 不得声称已经执行了任何触达、配置修改、人群圈选等动作；你只输出建议。
7. 全部内容使用简体中文（专有名词与文件名除外）。
8. 只输出一个 JSON 对象，不要输出 markdown 代码块、不要输出解释性文字。"""

OUTPUT_SCHEMA = """{
  "question_overview": "用 2~3 句话说明本次分析的问题与范围",
  "data_facts": ["每条一个事实，必须包含数字与口径说明"],
  "knowledge_citations": [{"document": "文件名.md", "section": "章节标题", "point": "该章节支撑了什么判断"}],
  "hypotheses": [{"hypothesis": "可能原因", "basis": "基于哪条数据事实或知识依据", "confidence": "高/中/低"}],
  "target_segments": ["建议关注的具体客群，尽量用指标中的规模描述"],
  "recommended_actions": [{"action": "具体动作", "target": "对象", "expected_effect": "预期影响", "effort": "低/中/高"}],
  "metrics": {"primary": "用于验证效果的主指标及口径", "guardrails": ["护栏指标"]},
  "limitations": ["数据限制、口径风险、需要进一步验证的事项"]
}"""


@dataclass
class Report:
    """一份完整报告，含成文、审计与元信息。"""

    scenario: str
    question: str
    content: dict[str, Any]
    metrics_payload: dict[str, Any]
    retrieved: list[Any]
    audit_result: dict[str, Any]
    llm_meta: dict[str, Any]
    degraded: bool = False
    degradation_reason: str = ""
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "question": self.question,
            "content": self.content,
            "metrics": self.metrics_payload,
            "retrieved": [
                {
                    "chunk_id": c.chunk_id,
                    "document": c.source_file,
                    "section": c.section,
                    "distance": round(c.distance, 4),
                }
                for c in self.retrieved
            ],
            "audit": self.audit_result,
            "llm": self.llm_meta,
            "degraded": self.degraded,
            "degradation_reason": self.degradation_reason,
            "attempts": self.attempts,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 提示词拼装
# ---------------------------------------------------------------------------


def _format_chunks(chunks: list[Any]) -> str:
    if not chunks:
        return "（本次未检索到相关知识片段）"
    lines: list[str] = []
    for idx, item in enumerate(chunks, start=1):
        doc = item.source_file.rsplit("/", 1)[-1]
        lines.append(
            f"[片段 {idx}] 文档：{doc}｜章节：{item.section}｜相关度距离：{item.distance:.4f}\n"
            f"{item.text}"
        )
    return "\n\n".join(lines)


def _build_user_prompt(
    question: str,
    metrics_payload: dict[str, Any],
    chunks: list[Any],
) -> str:
    # 指标 JSON 用 indent 保留结构，模型更容易对齐字段，
    # 同时便于人工在日志里核对"它到底看到了什么"
    metrics_json = json.dumps(metrics_payload, ensure_ascii=False, indent=2)
    return f"""## 用户问题
{question}

## 指标数据（唯一允许引用的数字来源）
```json
{metrics_json}
```

## 知识片段（唯一允许引用的知识来源）
{_format_chunks(chunks)}

## 输出要求
严格按以下 JSON 结构输出，键名不得改动：
{OUTPUT_SCHEMA}

注意：
- data_facts 中的每个数字都必须能在「指标数据」里找到原值。
- knowledge_citations 的 document 必须与「知识片段」中出现的文件名完全一致。
- 若「知识片段」为空或不相关，knowledge_citations 返回空数组，并在 limitations 中说明。"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """去掉 ```json 围栏。小模型即使被要求"只输出 JSON"也常带围栏。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_response(text: str) -> dict[str, Any]:
    """稳健解析：围栏 → 直接解析 → 截取首个 { 到末个 } 再解析。"""
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型输出不是合法 JSON：{exc}") from exc
    raise ValueError(f"模型输出中找不到 JSON 对象；前 200 字：{text[:200]}")


def _normalize_content(data: dict[str, Any]) -> dict[str, Any]:
    """把模型输出规整成固定 8 段，缺失的补空并在 warnings 里记一笔。

    不做"缺就报错"的原因：小模型偶尔漏字段，但漏一个字段不该让整份报告作废，
    应该降级展示并明确告知缺了什么。
    """
    def _as_list(value: Any) -> list[Any]:
        if value is None or value == "":
            return []
        return value if isinstance(value, list) else [value]

    metrics = data.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {"primary": str(metrics), "guardrails": []}

    return {
        "question_overview": str(data.get("question_overview") or "").strip(),
        "data_facts": _as_list(data.get("data_facts")),
        "knowledge_citations": [
            c for c in _as_list(data.get("knowledge_citations")) if isinstance(c, dict)
        ],
        "hypotheses": [h for h in _as_list(data.get("hypotheses")) if isinstance(h, dict)],
        "target_segments": _as_list(data.get("target_segments")),
        "recommended_actions": [
            a for a in _as_list(data.get("recommended_actions")) if isinstance(a, dict)
        ],
        "metrics": {
            "primary": str(metrics.get("primary") or "").strip(),
            "guardrails": _as_list(metrics.get("guardrails")),
        },
        "limitations": _as_list(data.get("limitations")),
    }


# ---------------------------------------------------------------------------
# 降级模板
# ---------------------------------------------------------------------------


def _fallback_content(metrics_payload: dict[str, Any], chunks: list[Any], reason: str) -> dict[str, Any]:
    """不调用模型的确定性模板。

    只输出指标里已有的数字与检索到的文档名，绝不补充任何解释性推断——
    因为任何推断在缺少审计的情况下都可能是错的。
    """
    m = metrics_payload.get("metrics", {})
    facts: list[str] = []
    for key, value in m.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        facts.append(f"{key} = {value}")

    citations = [
        {
            "document": c.source_file.rsplit("/", 1)[-1],
            "section": c.section,
            "point": "（降级模式：仅列出本次检索到的片段，未生成解读）",
        }
        for c in chunks
    ]

    return {
        "question_overview": f"已按白名单函数取数并完成检索，但成文环节降级：{reason}",
        "data_facts": facts,
        "knowledge_citations": citations,
        "hypotheses": [],
        "target_segments": [],
        "recommended_actions": [],
        "metrics": {"primary": "", "guardrails": []},
        "limitations": [
            "本报告为降级输出：模型未通过输出校验，因此不提供原因假设与运营建议。",
            "指标口径见 metrics 字段与 definitions，数字未经模型改写。",
        ],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


DEFAULT_QUESTIONS = {
    "activation_diagnosis": "最近一个季度的新客激活率表现如何？与上一季度相比有什么变化，可能的原因是什么？",
    "feature_adoption_diagnosis": "最近一个季度各 AI 功能的采用率有什么变化？哪些功能在下降？",
    "low_activity_customer": "当前哪些客户处于低活跃状态？他们的收入风险有多大，应该怎么运营？",
}


def generate_report(
    scenario: str,
    params: dict[str, Any],
    *,
    question: str | None = None,
    client: LLMClient | None = None,
    top_k: int | None = None,
) -> Report:
    """端到端生成一份诊断报告。"""
    if scenario not in ops_metrics.SCENARIOS:
        raise ops_metrics.ParamError(f"未知场景 {scenario!r}")

    # ① 白名单取数（参数校验在这里，非法参数会直接抛错，不会进入 LLM）
    metrics_payload = ops_metrics.run_scenario(scenario, **params)

    # ② 检索：用场景默认问题 + 指标里的关键实体，避免只靠一句泛化问句召回
    question = (question or DEFAULT_QUESTIONS.get(scenario, "")).strip()
    retrieval_query = f"{question} {ops_metrics.SCENARIOS[scenario]['label']}"
    chunks = retriever.retrieve(retrieval_query, top_k=top_k or config.RAG_TOP_K)

    warnings: list[str] = []
    # 知识覆盖不足必须显式暴露：距离超过阈值说明"知识库里可能没有真正对口的资料"，
    # 此时报告里的原因假设可信度天然更低。不提示的话，读者会以为引用是充分的。
    if not chunks:
        warnings.append("本次未检索到任何知识片段，第 3 段为空，原因假设缺乏知识依据。")
    elif chunks[0].distance > config.RAG_DISTANCE_WARN:
        warnings.append(
            f"检索最相关片段的距离为 {chunks[0].distance:.4f}，高于提示阈值 "
            f"{config.RAG_DISTANCE_WARN:.2f}，说明知识库对该问题的覆盖可能不足，"
            "引用仅作参考。"
        )

    # ③ 成文 + 审计 + 最多重试一次
    llm = client or llm_client.get_client()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(question, metrics_payload, chunks)},
    ]

    last_audit: dict[str, Any] = {}
    last_meta: dict[str, Any] = {}
    attempts = 0

    for attempt in range(1, 3):  # 最多两轮：首轮 + 一次纠错
        attempts = attempt
        try:
            response = llm.chat(messages, json_mode=True)
        except LLMError as exc:
            # 网络/密钥问题不重试第二遍，直接降级——重试只会让用户多等一轮
            content = _fallback_content(metrics_payload, chunks, f"模型调用失败：{exc}")
            return Report(
                scenario=scenario,
                question=question,
                content=content,
                metrics_payload=metrics_payload,
                retrieved=chunks,
                audit_result={"numbers_ok": False, "citations_ok": False,
                              "numbers_summary": "未执行（模型未返回）", "citations_summary": "未执行"},
                llm_meta={"provider": llm.provider, "model": llm.model, "error": str(exc)},
                degraded=True,
                degradation_reason=f"模型调用失败：{exc}",
                attempts=attempt,
                warnings=warnings,
            )

        last_meta = response.as_dict()

        try:
            parsed = parse_json_response(response.text)
        except ValueError as exc:
            warnings.append(f"第 {attempt} 轮输出无法解析为 JSON：{exc}")
            if attempt == 1:
                messages = messages + [
                    {"role": "assistant", "content": response.text[:2000]},
                    {"role": "user", "content": "上一次输出不是合法 JSON。请只输出一个 JSON 对象，不要任何其他文字。"},
                ]
                continue
            content = _fallback_content(metrics_payload, chunks, "模型连续两次输出非 JSON")
            return Report(
                scenario=scenario, question=question, content=content,
                metrics_payload=metrics_payload, retrieved=chunks,
                audit_result={"numbers_ok": False, "citations_ok": False,
                              "numbers_summary": "未执行（输出非 JSON）", "citations_summary": "未执行"},
                llm_meta=last_meta, degraded=True,
                degradation_reason="模型连续两次输出非 JSON", attempts=attempts, warnings=warnings,
            )

        content = _normalize_content(parsed)
        last_audit = audit.audit_report(content, metrics_payload, chunks)

        if last_audit["numbers_ok"] and last_audit["citations_ok"]:
            return Report(
                scenario=scenario, question=question, content=content,
                metrics_payload=metrics_payload, retrieved=chunks,
                audit_result=last_audit, llm_meta=last_meta,
                degraded=False, attempts=attempts, warnings=warnings,
            )

        # 审计不通过：把具体违规项反馈给模型，让它针对性改，而不是重跑一遍碰运气
        if attempt == 1:
            problems: list[str] = []
            if last_audit["number_violations"]:
                items = "；".join(
                    f"「{v['number']}」出现在「{v['context']}」"
                    + (f"，最接近的合法值是 {v['nearest_allowed']}" if v.get("nearest_allowed") else "")
                    for v in last_audit["number_violations"][:8]
                )
                problems.append(f"以下数字在指标数据中不存在，必须删除或替换为正确值：{items}")
                problems.append(
                    "常见原因是漏了数量级：指标里的比例是 0~1 的小数，"
                    "0.8555 应写作 85.55%，不是 8.555%。"
                )
            if last_audit["citation_violations"]:
                items = "；".join(
                    f"{v['document']}#{v['section']}" for v in last_audit["citation_violations"][:5]
                )
                problems.append(f"以下引用不在本次检索结果中，必须删除：{items}")

            messages = messages + [
                {"role": "assistant", "content": response.text[:3000]},
                {
                    "role": "user",
                    "content": "上一版未通过校验，请修正后重新输出完整 JSON：\n"
                    + "\n".join(f"- {p}" for p in problems),
                },
            ]
            warnings.append("首轮未通过审计，已带错误反馈重试一次")

    # 两轮都没过：降级，但保留已解析的结构供人工参考
    content = _fallback_content(metrics_payload, chunks, "模型两次输出均未通过数字/引用校验")
    return Report(
        scenario=scenario, question=question, content=content,
        metrics_payload=metrics_payload, retrieved=chunks,
        audit_result=last_audit, llm_meta=last_meta,
        degraded=True, degradation_reason="模型两次输出均未通过数字/引用校验",
        attempts=attempts, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


DATA_SOURCE_LABEL = {
    "real": "真实数据（T1）",
    "simulated": "模拟数据（T2）",
}


def _format_period(period: dict[str, Any]) -> str:
    """截点类场景（as_of）没有起始日，直接拼会渲染出「 ~ 2024-12-31」这种空串。"""
    start, end = period.get("start"), period.get("end")
    if start and end:
        return f"{start} ~ {end}"
    if period.get("as_of"):
        return f"截至 {period['as_of']}"
    return "—"


def render_markdown(report: Report) -> str:
    """把报告渲染成固定 8 段的 Markdown。"""
    c = report.content
    m = report.metrics_payload
    period = m.get("period", {})
    period_text = _format_period(period)
    source_label = DATA_SOURCE_LABEL.get(str(m.get("data_source")), str(m.get("data_source")))
    lines: list[str] = []

    lines.append(f"# 运营诊断报告｜{ops_metrics.SCENARIOS[report.scenario]['label']}")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 场景 | `{report.scenario}` |")
    lines.append(f"| 数据来源 | {source_label} |")
    lines.append(f"| 统计区间 | {period_text} |")
    lines.append(f"| 生成模型 | {report.llm_meta.get('provider')} / {report.llm_meta.get('model')} |")
    lines.append(f"| 成文状态 | {'⚠️ 降级' if report.degraded else '正常'} |")
    lines.append(f"| 数字审计 | {report.audit_result.get('numbers_summary', '—')} |")
    lines.append(f"| 引用校验 | {report.audit_result.get('citations_summary', '—')} |")
    lines.append("")

    if report.degraded:
        lines.append(f"> ⚠️ 本报告为降级输出：{report.degradation_reason}")
        lines.append("> 降级意味着模型输出未通过校验，已用确定性模板替代，只保留可追溯的数字与文档名。")
        lines.append("")

    # 1
    lines.append(f"## 1. {SECTION_TITLES[0]}")
    lines.append("")
    lines.append(c.get("question_overview") or "（模型未给出）")
    lines.append("")
    lines.append(f"**用户问题**：{report.question}")
    lines.append("")
    if report.warnings:
        for w in report.warnings:
            lines.append(f"> ⚠️ {w}")
        lines.append("")

    # 2
    lines.append(f"## 2. {SECTION_TITLES[1]}")
    lines.append("")
    lines.append(f"口径来源：{source_label}，统计区间 {period_text}")
    lines.append("")
    for item in c.get("data_facts") or []:
        lines.append(f"- {item}")
    if not c.get("data_facts"):
        lines.append("- （无）")
    lines.append("")

    # 3
    lines.append(f"## 3. {SECTION_TITLES[2]}")
    lines.append("")
    citations = c.get("knowledge_citations") or []
    if citations:
        for cite in citations:
            doc = cite.get("document", "")
            section = cite.get("section", "")
            point = cite.get("point", "")
            lines.append(f"- `{doc}` › {section}：{point}")
    else:
        lines.append("- （本次未检索到可引用的知识片段）")
    lines.append("")

    # 4
    lines.append(f"## 4. {SECTION_TITLES[3]}")
    lines.append("")
    hypotheses = c.get("hypotheses") or []
    if hypotheses:
        lines.append("> 以下为假设，尚未被数据验证，不得当作结论使用。")
        lines.append("")
        for h in hypotheses:
            lines.append(
                f"- **{h.get('hypothesis', '')}**"
                f"（置信度：{h.get('confidence', '未标注')}）"
            )
            if h.get("basis"):
                lines.append(f"  - 依据：{h['basis']}")
    else:
        lines.append("- （证据不足，未给出原因假设）")
    lines.append("")

    # 5
    lines.append(f"## 5. {SECTION_TITLES[4]}")
    lines.append("")
    segments = c.get("target_segments") or []
    for seg in segments:
        lines.append(f"- {seg}")
    if not segments:
        lines.append("- （未能从指标中确定具体客群）")
    lines.append("")

    # 6
    lines.append(f"## 6. {SECTION_TITLES[5]}")
    lines.append("")
    actions = c.get("recommended_actions") or []
    if actions:
        lines.append("| 动作 | 对象 | 预期影响 | 成本 |")
        lines.append("|---|---|---|---|")
        for a in actions:
            lines.append(
                f"| {a.get('action', '')} | {a.get('target', '')} | "
                f"{a.get('expected_effect', '')} | {a.get('effort', '')} |"
            )
        lines.append("")
        lines.append("*以上为建议，本次分析未执行任何实际触达或配置变更。*")
    else:
        lines.append("- （未给出建议）")
    lines.append("")

    # 7
    lines.append(f"## 7. {SECTION_TITLES[6]}")
    lines.append("")
    metrics_block = c.get("metrics") or {}
    lines.append(f"- **主指标**：{metrics_block.get('primary') or '（未指定）'}")
    guardrails = metrics_block.get("guardrails") or []
    if guardrails:
        for g in guardrails:
            lines.append(f"- **护栏**：{g}")
    else:
        lines.append("- **护栏**：（未指定）")
    lines.append("")

    # 8
    lines.append(f"## 8. {SECTION_TITLES[7]}")
    lines.append("")
    for item in c.get("limitations") or []:
        lines.append(f"- {item}")
    # 指标自带的口径限制必须出现在报告里，不能只留在 JSON
    for item in (m.get("caveats") or []):
        lines.append(f"- 【口径限制】{item}")
    lines.append("")

    # 附录：检索明细，便于复核引用
    lines.append("---")
    lines.append("")
    lines.append("### 附：本次检索片段")
    lines.append("")
    lines.append("| # | 文档 | 章节 | 距离 |")
    lines.append("|---|---|---|---|")
    for idx, item in enumerate(report.retrieved, start=1):
        lines.append(
            f"| {idx} | `{item.source_file.rsplit('/', 1)[-1]}` | {item.section} | {item.distance:.4f} |"
        )
    lines.append("")

    return "\n".join(lines)
