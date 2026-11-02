#!/usr/bin/env python3
"""阶段 4：检索质量评估。

用法：
    python scripts/evaluate_retrieval.py
    python scripts/evaluate_retrieval.py --top-k 5

评估集为 knowledge_base/eval/questions.yaml（v1，已冻结）。18 题构成：
    12 题单文档 + 3 题跨文档 + 3 题应拒答

指标：
    Hit@3      前 3 条里是否出现任一预期文档（是/否）
    Recall@3   预期文档中被前 3 条覆盖的比例（跨文档题才有区分度）
    MRR        第一条相关结果的排名倒数，衡量"排得有多靠前"
    拒答可分离性 应拒答题的 top1 距离是否明显大于应命中题——决定能否用距离阈值拒答

评估纪律：题目在索引构建前冻结，本脚本只读不改。
禁止先看结果再调题；阈值若按结果调整必须显式标注为"形成性评估"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402
from src.rag import retriever  # noqa: E402

QUESTIONS_FILE = config.KNOWLEDGE_DIR / "eval" / "questions.yaml"

# 对照组：BGE 中文版官方推荐的查询指令。留作实测比较，不预设哪边更好。
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def load_questions() -> dict:
    payload = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))
    return payload


def doc_stem(source_file: str) -> str:
    return Path(source_file).stem


def evaluate_one(question: dict, top_k: int, instruction: str, max_per_doc: int | None) -> dict:
    items = retriever.retrieve(
        question["question"], top_k=top_k, instruction=instruction, max_per_doc=max_per_doc
    )
    docs = [doc_stem(item.source_file) for item in items]
    expected = question.get("expected_docs") or []

    if expected:
        top3 = docs[:3]
        hits = [doc for doc in expected if doc in top3]
        hit_at_3 = bool(hits)
        recall_at_3 = len(hits) / len(expected)
        # top1 命中率单独统计：Hit@3 会把"第一名错、第三名对"算作成功，
        # 而生成侧主要读前几条，第一名错了实际体验会明显变差。
        top1_ok = bool(docs) and docs[0] in expected
        mrr = 0.0
        for rank, doc in enumerate(docs, start=1):
            if doc in expected:
                mrr = 1.0 / rank
                break
    else:
        hit_at_3, recall_at_3, mrr, top1_ok = None, None, None, None

    return {
        "id": question["id"],
        "type": question["type"],
        "question": question["question"],
        "expected": ",".join(expected),
        "top1_doc": docs[0] if docs else "",
        "top1_distance": round(items[0].distance, 4) if items else None,
        "top3_docs": ",".join(docs[:3]),
        "hit@3": hit_at_3,
        "recall@3": round(recall_at_3, 4) if recall_at_3 is not None else None,
        "top1_ok": top1_ok,
        "mrr": round(mrr, 4) if mrr is not None else None,
    }


def summarize(rows: pd.DataFrame) -> dict:
    answerable = rows[rows["type"].isin(["single", "cross"])]
    refused = rows[rows["type"] == "refuse"]
    return {
        "题目数": len(answerable),
        "Hit@3": round(float(answerable["hit@3"].mean()), 4),
        "Top1 准确率": round(float(answerable["top1_ok"].mean()), 4),
        "Recall@3": round(float(answerable["recall@3"].mean()), 4),
        "MRR": round(float(answerable["mrr"].mean()), 4),
        "单文档 Hit@3": round(float(answerable[answerable["type"] == "single"]["hit@3"].mean()), 4),
        "跨文档 Hit@3": round(float(answerable[answerable["type"] == "cross"]["hit@3"].mean()), 4),
        "跨文档 Recall@3": round(float(answerable[answerable["type"] == "cross"]["recall@3"].mean()), 4),
        "拒答题 top1 距离(均值)": round(float(refused["top1_distance"].mean()), 4),
        "应答题 top1 距离(均值)": round(float(answerable["top1_distance"].mean()), 4),
    }


# 对照配置。主口径取第一项；其余用于验证"文档配额"与"查询指令"是否真的有用。
# max_per_doc=0 表示不限制（注意：None 表示"用 config 默认值"，不能拿来当"不限制"）
CONFIGS = [
    {"label": "无指令·每文档≤2", "instruction": "", "max_per_doc": 2},      # 采用口径
    {"label": "无指令·不限额", "instruction": "", "max_per_doc": 0},        # 对照组
    {"label": "BGE指令·每文档≤2", "instruction": BGE_QUERY_INSTRUCTION, "max_per_doc": 2},
]


def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量评估")
    parser.add_argument("--top-k", type=int, default=5, help="召回条数（默认 5，Hit@3 只看前 3）")
    args = parser.parse_args()

    payload = load_questions()
    questions = payload["questions"]
    meta = payload["meta"]
    print(f"评估集 {meta['name']}（冻结于 {meta['frozen_at']}，共 {meta['total']} 题）")
    print(f"向量库：{config.CHROMA_DIR}　Top-K={args.top_k}\n")

    all_rows: list[dict] = []
    summaries: list[dict] = []
    for conf in CONFIGS:
        for question in questions:
            row = evaluate_one(question, args.top_k, conf["instruction"], conf["max_per_doc"])
            row["group"] = conf["label"]
            all_rows.append(row)
        subset = pd.DataFrame([r for r in all_rows if r["group"] == conf["label"]])
        summaries.append({"口径": conf["label"], **summarize(subset)})
        print(f"已完成「{conf['label']}」组 {len(questions)} 题")

    rows = pd.DataFrame(all_rows)
    summary_table = pd.DataFrame(summaries)

    # 主口径落盘（逐题一行）
    main_rows = rows[rows["group"] == CONFIGS[0]["label"]].drop(columns=["group"])
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.OUTPUT_DIR / "rag_eval_results.csv"
    main_rows.to_csv(out_path, index=False, encoding="utf-8-sig")
    summary_table.to_csv(config.TABLE_DIR / "rag_eval_summary.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print("检索指标（主口径：无查询指令、每文档最多 2 条 —— 即 config 默认值）")
    print("=" * 78)
    for key, value in summaries[0].items():
        if key != "口径":
            print(f"  {key:24s} {value}")

    print("\n" + "=" * 78)
    print("配置对照（用于验证「文档配额」与「查询指令」是否真的有用）")
    print("=" * 78)
    cols = ["口径", "Hit@3", "Top1 准确率", "Recall@3", "跨文档 Recall@3", "MRR"]
    print(summary_table[cols].to_string(index=False))

    # 未命中明细：失败案例必须逐条可追查
    missed = main_rows[(main_rows["type"] != "refuse") & (main_rows["hit@3"] == False)]  # noqa: E712
    print(f"\nHit@3 未命中 {len(missed)} 题")
    for _, row in missed.iterrows():
        print(f"  [{row['id']}] 期望 {row['expected']}　实际前3 {row['top3_docs']}")

    top1_missed = main_rows[(main_rows["type"] != "refuse") & (main_rows["top1_ok"] == False)]  # noqa: E712
    print(f"Top1 未命中 {len(top1_missed)} 题（Hit@3 已掩盖这些失败）：")
    for _, row in top1_missed.iterrows():
        print(f"  [{row['id']}] 期望 {row['expected']}　top1 是 {row['top1_doc']}（距离 {row['top1_distance']:.4f}）")

    print("\n拒答题的 top1 距离（越高说明越像「知识库里没有」）：")
    for _, row in main_rows[main_rows["type"] == "refuse"].iterrows():
        print(f"  [{row['id']}] 距离 {row['top1_distance']:.4f}　误命中 {row['top1_doc']}")

    print(f"\n逐题结果 → {out_path.relative_to(PROJECT_ROOT)}")
    print(f"配置对照 → {(config.TABLE_DIR / 'rag_eval_summary.csv').relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
