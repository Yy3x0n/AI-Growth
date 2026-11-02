#!/usr/bin/env python
"""运营诊断助手 CLI。

用法示例

    # 新客激活诊断（真实数据）
    python scripts/ask_ops_assistant.py --scenario activation_diagnosis \\
        --start-date 2024-07-01 --end-date 2024-09-30

    # AI 功能采用诊断（模拟日志）
    python scripts/ask_ops_assistant.py --scenario feature_adoption_diagnosis \\
        --start-date 2024-10-01 --end-date 2024-12-31

    # 低活跃客户
    python scripts/ask_ops_assistant.py --scenario low_activity_customer --as-of 2024-12-31

    # 不联网，验证链路
    python scripts/ask_ops_assistant.py --scenario low_activity_customer --as-of 2024-12-31 --mock

退出码

    0  正常报告：模型输出通过数字审计与引用校验
    1  降级输出：报告已生成，但模型未通过校验，已用确定性模板替代
       （非零是为了让自动化流程能发现"这次结果不可直接采用"）
    2  参数或配置错误（日期格式、超出观察期、缺少 API Key）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, llm_client, ops_metrics  # noqa: E402
from src.rag import assistant  # noqa: E402

EXIT_OK = 0
EXIT_LLM_FAILED = 1
EXIT_BAD_PARAMS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI 增长运营助手：白名单取数 + RAG 检索 + LLM 诊断报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=list(ops_metrics.SCENARIOS),
        help="分析场景（白名单，不可自定义）",
    )
    parser.add_argument("--start-date", help="区间起点 YYYY-MM-DD（activation / adoption 场景必填）")
    parser.add_argument("--end-date", help="区间终点 YYYY-MM-DD（activation / adoption 场景必填）")
    parser.add_argument("--as-of", help="截止日期 YYYY-MM-DD（low_activity 场景必填）")
    parser.add_argument("--window-days", type=int, default=7, help="激活观察窗口天数，默认 7")
    parser.add_argument("--inactive-days", type=int, default=30, help="低活跃阈值天数，默认 30")
    parser.add_argument("--top-n", type=int, default=10, help="低活跃清单返回前 N 个账户，默认 10")
    parser.add_argument("--source", choices=["simulated", "real"], default="simulated",
                        help="adoption 场景的数据源，默认 simulated（有 AI 语义）")
    parser.add_argument("--question", help="自定义问题；不填则用场景默认问题")
    parser.add_argument("--provider", choices=["glm", "deepseek", "mock"], help="覆盖 .env 里的 LLM_PROVIDER")
    parser.add_argument("--mock", action="store_true", help="等价于 --provider mock")
    parser.add_argument("--top-k", type=int, default=None, help="检索片段数，默认取 config.RAG_TOP_K")
    parser.add_argument("--out-dir", default=str(config.REPORT_DIR), help="报告输出目录")
    return parser


def collect_params(args: argparse.Namespace) -> dict:
    """按场景收集参数。必填项缺失时给出明确提示，而不是让下层抛 KeyError。"""
    scenario = args.scenario

    if scenario == "low_activity_customer":
        if not args.as_of:
            raise ops_metrics.ParamError("low_activity_customer 场景必须提供 --as-of")
        params = {"as_of_date": args.as_of, "inactive_days": args.inactive_days, "top_n": args.top_n}
    else:
        if not (args.start_date and args.end_date):
            raise ops_metrics.ParamError(f"{scenario} 场景必须提供 --start-date 与 --end-date")
        params = {"start_date": args.start_date, "end_date": args.end_date}
        if scenario == "activation_diagnosis":
            params["window_days"] = args.window_days
        elif scenario == "feature_adoption_diagnosis":
            params["source"] = args.source

    return params


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ---- 参数校验：在调用模型之前就把非法输入挡掉 ----
    try:
        params = collect_params(args)
        # 先跑一次取数，既校验参数也早点拿到指标，避免模型调用后才发现参数错
        ops_metrics.run_scenario(args.scenario, **params)
    except ops_metrics.ParamError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return EXIT_BAD_PARAMS

    provider = "mock" if args.mock else args.provider
    try:
        client = llm_client.get_client(provider)
    except ValueError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_BAD_PARAMS

    if not client.is_configured():
        print(
            f"[配置错误] provider={client.provider} 未配置 API Key。\n"
            f"  请在 .env 填写 {client.provider.upper()}_API_KEY，或加 --mock 离线验证。",
            file=sys.stderr,
        )
        return EXIT_BAD_PARAMS

    print(f"场景：{ops_metrics.SCENARIOS[args.scenario]['label']}（{args.scenario}）")
    print(f"参数：{json.dumps(params, ensure_ascii=False)}")
    print(f"模型：{client.describe()}")
    print("生成中…")

    report = assistant.generate_report(
        args.scenario, params, question=args.question, client=client, top_k=args.top_k
    )

    # ---- 落盘：JSON 供程序消费，Markdown 供人阅读 ----
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.metrics_payload["generated_at"].replace(":", "").replace("-", "")
    stem = f"{args.scenario}_{stamp}"

    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(assistant.render_markdown(report), encoding="utf-8")

    # ---- 控制台摘要 ----
    print()
    print("=" * 68)
    print("成文状态：", "⚠️ 降级" if report.degraded else "正常")
    if report.degraded:
        print("降级原因：", report.degradation_reason)
    print("尝试次数：", report.attempts)
    print("数字审计：", report.audit_result.get("numbers_summary", "—"))
    print("引用校验：", report.audit_result.get("citations_summary", "—"))
    meta = report.llm_meta
    if meta.get("total_tokens"):
        print(
            f"Token：{meta.get('prompt_tokens')} 入 / {meta.get('completion_tokens')} 出"
            f"（合计 {meta.get('total_tokens')}），耗时 {meta.get('latency_ms')} ms"
        )
    if report.warnings:
        print("过程提示：")
        for w in report.warnings:
            print("  -", w)
    print("=" * 68)
    print(f"Markdown：{md_path}")
    print(f"JSON    ：{json_path}")

    return EXIT_OK if not report.degraded else EXIT_LLM_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
