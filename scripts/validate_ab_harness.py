#!/usr/bin/env python3
"""阶段 3：A/B 检验框架的自校验（Monte Carlo）。

用法：
    python scripts/validate_ab_harness.py
    python scripts/validate_ab_harness.py --reps 5000

为什么必须做这一步：
    在一次模拟实验里得到 p=0.20，既可能是"确实没效应"，也可能是"框架有偏"。
    不先把框架本身验证清楚，报告里的任何 p 值都不值得信。

验证五件事：
    ① 第一类错误率：H0 为真时，多少次会误判显著（名义 5%）
    ② 功效：真实效应等于设计值时，多少次能检出（名义 80%）
    ③ 置信区间覆盖率：Newcombe 区间是否真的覆盖真值 95%
    ④ SRM 检验能否抓出 52/48 的分组偏移，且不会误报 50/50
    ⑤ 决策框架：主指标有效但护栏恶化时，会不会错误地放行

全部结果落盘到 outputs/tables/ab_harness_validation.csv。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import ab_test, config  # noqa: E402


def run_scenario(
    rng: np.random.Generator, n_arm: int, p_control: float, p_treatment: float, reps: int
) -> dict:
    """重复抽样，统计显著率与 CI 覆盖率。"""
    true_diff = p_treatment - p_control
    significant = 0
    covered = 0
    p_values = np.empty(reps)

    for i in range(reps):
        s_c = int(rng.binomial(n_arm, p_control))
        s_t = int(rng.binomial(n_arm, p_treatment))
        result = ab_test.ProportionResult("sim", s_c, n_arm, s_t, n_arm)
        p_values[i] = result.p_value
        if result.is_significant:
            significant += 1
        if result.ci_low <= true_diff <= result.ci_high:
            covered += 1

    return {
        "显著率": significant / reps,
        "CI覆盖率": covered / reps,
        "p值中位数": float(np.median(p_values)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B 检验框架自校验")
    parser.add_argument("--reps", type=int, default=2000, help="每个场景的重复次数")
    args = parser.parse_args()

    exp = config.EXPERIMENT
    p1, p2 = exp["baseline_rate"], exp["treatment_rate"]
    n_arm = ab_test.required_sample_size(p1, p2, exp["alpha"], exp["power"])
    rng = np.random.default_rng(config.RANDOM_SEED + 7)

    print("=" * 74)
    print("A/B 检验框架自校验（Monte Carlo）")
    print("=" * 74)
    print(f"  每臂样本 {n_arm:,}　重复 {args.reps:,} 次　α={exp['alpha']}　"
          f"目标功效 {exp['power']:.0%}")

    rows: list[dict] = []

    # ① 第一类错误
    print(f"\n① 第一类错误率（真实无差异：{p1:.4f} vs {p1:.4f}）")
    h0 = run_scenario(rng, n_arm, p1, p1, args.reps)
    print(f"   误判显著 {h0['显著率']:.3%}　（名义 {exp['alpha']:.1%}）")
    rows.append({"检验": "第一类错误率", "理论值": exp["alpha"], "实测值": round(h0["显著率"], 5),
                 "判定": "通过" if abs(h0["显著率"] - exp["alpha"]) < 0.015 else "偏离"})

    # ② 功效
    print(f"\n② 功效（真实效应 {p1:.4f} → {p2:.4f}）")
    h1 = run_scenario(rng, n_arm, p1, p2, args.reps)
    print(f"   检出率 {h1['显著率']:.2%}　（设计功效 {exp['power']:.0%}）")
    rows.append({"检验": "功效（设计效应）", "理论值": exp["power"], "实测值": round(h1["显著率"], 5),
                 "判定": "通过" if abs(h1["显著率"] - exp["power"]) < 0.05 else "偏离"})

    # ③ CI 覆盖率
    print(f"\n③ Newcombe 区间覆盖率（真值 {p2 - p1:+.4f}）")
    print(f"   覆盖 {h1['CI覆盖率']:.2%}　（名义 95%）")
    rows.append({"检验": "CI 覆盖率", "理论值": 0.95, "实测值": round(h1["CI覆盖率"], 5),
                 "判定": "通过" if abs(h1["CI覆盖率"] - 0.95) < 0.02 else "偏离"})

    # ④ SRM
    print(f"\n④ SRM 检验")
    false_alarm = sum(
        1 for _ in range(500)
        if ab_test.srm_check(*rng.multinomial(2 * n_arm, [0.5, 0.5]), expected_ratio=0.5)["is_srm"]
    ) / 500
    detect_52 = sum(
        1 for _ in range(500)
        if ab_test.srm_check(*rng.multinomial(2 * n_arm, [0.52, 0.48]), expected_ratio=0.5)["is_srm"]
    ) / 500
    detect_55 = sum(
        1 for _ in range(500)
        if ab_test.srm_check(*rng.multinomial(2 * n_arm, [0.55, 0.45]), expected_ratio=0.5)["is_srm"]
    ) / 500
    print(f"   50/50 误报率 {false_alarm:.3%}（阈值 α=0.001，应接近 0）")
    print(f"   52/48 检出率 {detect_52:.1%}　55/45 检出率 {detect_55:.1%}")
    rows.append({"检验": "SRM 误报率(50/50)", "理论值": 0.001, "实测值": round(false_alarm, 5),
                 "判定": "通过" if false_alarm < 0.01 else "偏离"})
    rows.append({"检验": "SRM 检出率(52/48)", "理论值": float("nan"), "实测值": round(detect_52, 5),
                 "判定": "已量化"})
    rows.append({"检验": "SRM 检出率(55/45)", "理论值": float("nan"), "实测值": round(detect_55, 5),
                 "判定": "已量化"})

    # ⑤ 决策框架：主指标有效 + 护栏恶化
    print(f"\n⑤ 决策框架（主指标真实有效，但工单率从 11% 恶化到 14%）")
    blocked = 0
    guardrail_reps = 500
    for _ in range(guardrail_reps):
        s_c = int(rng.binomial(n_arm, p1))
        s_t = int(rng.binomial(n_arm, p2))
        primary = ab_test.ProportionResult("primary", s_c, n_arm, s_t, n_arm)
        t_c = int(rng.binomial(n_arm, 0.11))
        t_t = int(rng.binomial(n_arm, 0.14))
        gr = ab_test.ProportionResult("ticket", t_c, n_arm, t_t, n_arm)
        rows_gr = [{
            "metric": "ticket", "is_harm": gr.is_significant and gr.absolute_lift > 0,
            "p_value": gr.p_value,
        }]
        decision = ab_test.decide(primary, p2 - p1, rows_gr, ab_test.srm_check(n_arm, n_arm))
        if not decision["supported"]:
            blocked += 1
    print(f"   拦住 {blocked / guardrail_reps:.2%} 的重复（越高越好：说明护栏机制不是摆设）")
    rows.append({"检验": "决策框架拦住护栏恶化", "理论值": float("nan"),
                 "实测值": round(blocked / guardrail_reps, 5), "判定": "已量化"})

    # ⑥ 基线敏感性：固定样本量，不同基线下的功效
    print(f"\n⑥ 基线敏感性（每臂仍是 {n_arm:,}，只改基线）")
    sensitivity = []
    for base in (0.15, 0.20, 0.25, 0.30, 0.35):
        target = base * (1 + exp["target_relative_lift"])
        res = run_scenario(rng, n_arm, base, target, 500)
        sensitivity.append({"基线": base, f"目标(+{exp['target_relative_lift']:.0%})": round(target, 4),
                            "实测功效": round(res["显著率"], 3)})
        print(f"   基线 {base:.2f} → 功效 {res['显著率']:.1%}")
    config.TABLE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sensitivity).to_csv(config.TABLE_DIR / "ab_power_sensitivity.csv", index=False)

    out = pd.DataFrame(rows)
    out.to_csv(config.TABLE_DIR / "ab_harness_validation.csv", index=False)

    print("\n" + "=" * 74)
    print(out.to_string(index=False))
    print(f"\n产物已写入 {config.TABLE_DIR.relative_to(PROJECT_ROOT)}/")

    failed = out[out["判定"] == "偏离"]
    return 0 if failed.empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
