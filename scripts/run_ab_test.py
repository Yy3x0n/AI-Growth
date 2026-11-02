#!/usr/bin/env python3
"""阶段 3：A/B 实验分析（模块 D）。

用法：
    python scripts/run_ab_test.py

结论框架（设计文档 §6.2，三个条件缺一即为"证据不足"）：
    统计显著  ∧  效应 ≥ 设计 MDE  ∧  护栏未恶化

本脚本只跑**一次**分析，不做多次尝试后挑好看的结果。
模拟数据由 scripts/simulate_data.py 用固定种子一次性生成，重跑结果必然相同。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import ab_test, config  # noqa: E402

AB_FILE = "ab_test_simulated.csv"


def main() -> int:
    path = config.SIMULATED_DIR / AB_FILE
    if not path.is_file():
        print(f"缺少 {path}，请先运行 scripts/simulate_data.py", file=sys.stderr)
        return 1

    df = pd.read_csv(path)
    if (df["data_source"] != "simulated").any():
        print("数据中出现非 simulated 来源的行，拒绝分析", file=sys.stderr)
        return 1

    exp = config.EXPERIMENT
    control = df[df["group"] == "control"]
    treatment = df[df["group"] == "treatment"]

    print("=" * 74)
    print("A/B 实验分析（模拟实验，仅演示分析方法）")
    print("=" * 74)
    print(f"  实验：{exp['name']}")
    print(f"  样本：对照 {len(control):,} / 实验 {len(treatment):,}")

    # ---- 1. SRM ----
    srm = ab_test.srm_check(len(control), len(treatment))
    print(f"\n① 样本比例（SRM）")
    print(f"   对照占比 {srm['observed_ratio']:.4f}　chi2={srm['chi2']:.4f}　"
          f"p={srm['p_value']:.4f}　判定：{'失衡 !!' if srm['is_srm'] else '正常'}")

    # ---- 2. 主指标 ----
    primary = ab_test.ProportionResult(
        name="first_effective_task_7d",
        control_success=int(control["first_effective_task_7d"].sum()),
        control_n=len(control),
        treatment_success=int(treatment["first_effective_task_7d"].sum()),
        treatment_n=len(treatment),
    )
    design_mde_abs = exp["treatment_rate"] - exp["baseline_rate"]

    print(f"\n② 主指标：7 日内完成首次有效任务")
    print(f"   对照 {primary.control_rate:.4f}　实验 {primary.treatment_rate:.4f}")
    print(f"   绝对提升 {primary.absolute_lift:+.4f}（相对 {primary.relative_lift:+.1%}）")
    print(f"   95% CI [{primary.ci_low:+.4f}, {primary.ci_high:+.4f}]（Newcombe 混合评分法）")
    print(f"   z={primary.z_stat:.3f}　p={primary.p_value:.4f}　"
          f"{'显著' if primary.is_significant else '不显著'}")

    # ---- 3. 护栏 ----
    print(f"\n③ 护栏指标")
    guardrail_rows: list[dict] = []
    for name, spec in exp["guardrails"].items():
        if df[name].dropna().isin([0, 1]).all() and df[name].nunique() <= 2:
            result = ab_test.ProportionResult(
                name=name,
                control_success=int(control[name].sum()),
                control_n=len(control),
                treatment_success=int(treatment[name].sum()),
                treatment_n=len(treatment),
            )
            diff = result.absolute_lift
            is_harm = result.is_significant and (
                diff > 0 if spec["direction"] == "lower_is_better" else diff < 0
            )
            row = result.as_row() | {"direction": spec["direction"], "is_harm": is_harm, "type": "binary"}
        else:
            stats = ab_test.welch_test(
                control[name].dropna().to_numpy(), treatment[name].dropna().to_numpy()
            )
            diff = stats["absolute_lift"]
            is_harm = stats["significant"] and (
                diff > 0 if spec["direction"] == "lower_is_better" else diff < 0
            )
            row = {
                "metric": name, "control_rate": round(stats["control_mean"], 6),
                "treatment_rate": round(stats["treatment_mean"], 6),
                "absolute_lift": round(diff, 6),
                "relative_lift": round(diff / stats["control_mean"], 4) if stats["control_mean"] else 0,
                "ci_low": round(stats["ci_low"], 6), "ci_high": round(stats["ci_high"], 6),
                "z_stat": round(stats["t_stat"], 4), "p_value": round(stats["p_value"], 6),
                "significant": stats["significant"], "direction": spec["direction"],
                "is_harm": is_harm, "type": "continuous",
            }
        guardrail_rows.append(row)
        print(f"   {name:24s} 对照 {row['control_rate']:.4f} → 实验 {row['treatment_rate']:.4f}"
              f"　p={row['p_value']:.4f}"
              f"　{'护栏恶化 !!' if is_harm else ('显著改善' if row['significant'] else '无显著变化')}")

    # ---- 4. 决策 ----
    decision = ab_test.decide(primary, design_mde_abs, guardrail_rows, srm)
    print(f"\n④ 决策：{decision['decision']}")
    for reason in decision["reasons"]:
        print(f"   · {reason}")

    # ---- 5. 落盘 ----
    config.TABLE_DIR.mkdir(parents=True, exist_ok=True)

    summary = pd.DataFrame([{
        "实验": exp["name"],
        "对照样本": len(control),
        "实验样本": len(treatment),
        "对照率": round(primary.control_rate, 6),
        "实验率": round(primary.treatment_rate, 6),
        "绝对提升": round(primary.absolute_lift, 6),
        "相对提升": round(primary.relative_lift, 4),
        "CI_下限": round(primary.ci_low, 6),
        "CI_上限": round(primary.ci_high, 6),
        "p值": round(primary.p_value, 6),
        "设计MDE_绝对": round(design_mde_abs, 6),
        "设计MDE_相对": exp["target_relative_lift"],
        "功效": exp["power"],
        "样本量每臂": exp.get("n_arm", len(control)),
    }])
    summary.to_csv(config.TABLE_DIR / "ab_test_primary.csv", index=False)

    pd.DataFrame(guardrail_rows).to_csv(config.TABLE_DIR / "ab_test_guardrails.csv", index=False)

    pd.DataFrame([{
        "决策": decision["decision"],
        "支持上线": decision["supported"],
        "失败原因": "；".join(decision["reasons"]) if decision["reasons"] else "",
        "SRM_p值": round(srm["p_value"], 6),
    }]).to_csv(config.TABLE_DIR / "ab_test_decision.csv", index=False)

    print(f"\n产物已写入 {config.TABLE_DIR.relative_to(PROJECT_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
