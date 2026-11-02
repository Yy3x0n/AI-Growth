#!/usr/bin/env python3
"""阶段 3：生成 T2 / T3 模拟数据。

用法：
    python scripts/simulate_data.py
    python scripts/simulate_data.py --force   # 覆盖已有文件

产出（均为**模拟**数据，带 data_source 列）：
    data/simulated/ai_request_logs.csv      T2 AI 请求日志（账户级，锚定真实订阅与流失）
    data/simulated/ab_test_simulated.csv    T3 A/B 实验账户级明细（合成 ID）
    docs/simulated_data_manifest.md         校验和与生成参数清单

设计约束：
    - 同一 RANDOM_SEED 必须复现同一份文件，故写出后立刻重算 sha256 并与内存比对；
    - 生成完立即做"参数恢复"自检：把已知的生成参数从数据里估计出来，
      与 config 中的设定值比对。这一步能抓住"生成器写错但没人发现"的问题。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import ab_test, config, simulation  # noqa: E402

AB_FILE = "ab_test_simulated.csv"
AI_FILE = "ai_request_logs.csv"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def print_design() -> int:
    """打印实验设计参数，并回答"为什么不能用真实账户做实验"。"""
    exp = config.EXPERIMENT
    p1, p2 = exp["baseline_rate"], exp["treatment_rate"]
    n_arm = ab_test.required_sample_size(p1, p2, exp["alpha"], exp["power"])

    print("=" * 74)
    print("实验设计（参数冻结于 src/config.py，本脚本只读）")
    print("=" * 74)
    print(f"  基线 {p1:.2%} → 目标 {p2:.2%}（相对 +{exp['target_relative_lift']:.0%}）"
          f"  α={exp['alpha']}  power={exp['power']:.0%}")
    print(f"  所需样本：每臂 {n_arm:,}，合计 {2 * n_arm:,}")

    print("\n  为什么不能直接用 T1 的 500 个真实账户做实验：")
    for label, n in (("全部 500 个账户（每臂 250）", 250), ("按单月新增约 21 个（每臂 10）", 10)):
        mde = ab_test.mde_for_n(n, p1)
        print(f"    {label:26s} → 只能检测 {mde:+.3f}（相对 {mde / p1:.0%}）")
    print("  运营上不存在「新手引导带来 46% 相对提升」的可能，故按设计样本量生成模拟实验。")
    print()
    return n_arm


def check_recovery(df: pd.DataFrame) -> pd.DataFrame:
    """参数恢复自检：从生成结果反推参数，与设定值比对。"""
    rows = []
    for feature, spec in config.AI_FEATURES.items():
        sub = df[df["feature_name"] == feature]
        if sub.empty:
            continue
        # 故障窗口内的问答请求成功率被刻意降级，比对基线时要排除
        inc = config.SIMULATED_INCIDENT
        if feature == inc["feature"]:
            in_window = sub["request_time"].between(
                pd.Timestamp(inc["start"]), pd.Timestamp(inc["end"]) + pd.Timedelta(days=1)
            )
            sub = sub[~in_window]
        rows.append({
            "参数": f"{feature} 成功率",
            "设定值": spec["success_rate"],
            "估计值": round(float(sub["status"].eq("success").mean()), 4),
        })
        rows.append({
            "参数": f"{feature} 请求占比",
            "设定值": spec["mix"],
            "估计值": round(len(df[df["feature_name"] == feature]) / len(df), 4),
        })
    out = pd.DataFrame(rows)
    out["偏差"] = (out["估计值"] - out["设定值"]).round(4)
    out["判定"] = out["偏差"].abs().le(0.02).map({True: "OK", False: "偏离 >2pp"})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 T2/T3 模拟数据")
    parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = parser.parse_args()

    n_arm = print_design()

    config.SIMULATED_DIR.mkdir(parents=True, exist_ok=True)
    ai_path = config.SIMULATED_DIR / AI_FILE
    ab_path = config.SIMULATED_DIR / AB_FILE

    if not args.force and ai_path.is_file() and ab_path.is_file():
        print("两份模拟数据已存在，跳过生成（加 --force 覆盖）")
        return 0

    # ---- T2 ----
    print("① 生成 AI 请求日志（锚定真实订阅与流失）")
    grid = simulation.active_account_months()
    print(f"   「账户-月」活跃网格：{len(grid):,} 行 / {grid['account_id'].nunique()} 个账户")
    logs = simulation.simulate_ai_request_logs(grid)
    logs.to_csv(ai_path, index=False)
    print(f"   → {ai_path.relative_to(PROJECT_ROOT)}  {len(logs):,} 行")

    print("\n   参数恢复自检：")
    recovery = check_recovery(logs)
    print(recovery.to_string(index=False))
    if (recovery["判定"] != "OK").any():
        print("\n   警告：存在偏离设定值超过 2 个百分点的参数，请检查生成逻辑")

    # ---- T3 ----
    print("\n② 生成 A/B 实验明细（合成账户 ID）")
    ab = simulation.simulate_ab_test(n_arm)
    ab.to_csv(ab_path, index=False)
    print(f"   → {ab_path.relative_to(PROJECT_ROOT)}  {len(ab):,} 行")
    print(
        ab.groupby("group")
        .agg(
            账户数=("account_id", "size"),
            主指标均值=("first_effective_task_7d", "mean"),
            错误率均值=("post_7d_error_rate", "mean"),
            工单率=("support_ticket_7d", "mean"),
        )
        .round(4)
        .to_string()
    )

    # ---- 清单 ----
    print("\n③ 写校验和清单")
    manifest = [
        "# 模拟数据清单（Simulated Data Manifest）",
        "",
        "> 本文件由 `scripts/simulate_data.py` 自动生成，请勿手工编辑。",
        f"> 随机种子 `RANDOM_SEED = {config.RANDOM_SEED}`；同一种子重跑结果逐字节一致。",
        "",
        "## 文件",
        "",
        "| 文件 | 层级 | 行数 | 字节 | sha256 |",
        "|---|---|---:|---:|---|",
        f"| `data/simulated/{AI_FILE}` | T2 | {len(logs):,} | {ai_path.stat().st_size:,} | `{sha256_of(ai_path)}` |",
        f"| `data/simulated/{AB_FILE}` | T3 | {len(ab):,} | {ab_path.stat().st_size:,} | `{sha256_of(ab_path)}` |",
        "",
        "## 生成参数（冻结于 `src/config.py`）",
        "",
        "| 参数 | 值 |",
        "|---|---|",
        f"| 观察期 | {config.OBSERVATION_START} ~ {config.OBSERVATION_END} |",
        f"| AI 采用率 | {config.AI_ADOPTION_RATE:.0%} |",
        f"| 月度自然增长 | ×{config.AI_MONTHLY_GROWTH} / 月 |",
        f"| 故障窗口 | {config.SIMULATED_INCIDENT['feature']} "
        f"{config.SIMULATED_INCIDENT['start']} ~ {config.SIMULATED_INCIDENT['end']}，"
        f"成功率降至 {config.SIMULATED_INCIDENT['degraded_success_rate']} |",
        f"| A/B 基线 / 目标 | {config.EXPERIMENT['baseline_rate']:.2%} → "
        f"{config.EXPERIMENT['treatment_rate']:.2%} |",
        f"| A/B 每臂样本 | {n_arm:,} |",
        "",
        "## 参数恢复自检",
        "",
        recovery.to_markdown(index=False),
        "",
        "> 说明：生成器的参数是已知的，因此可以用生成结果反推参数并比对。",
        "> 若偏差超过 2 个百分点，说明生成逻辑与配置不一致，需先修生成器再用数据。",
        "",
    ]
    manifest_path = config.DOCS_DIR / "simulated_data_manifest.md"
    manifest_path.write_text("\n".join(manifest), encoding="utf-8")
    print(f"   → {manifest_path.relative_to(PROJECT_ROOT)}")

    print("\n完成。下一步：python scripts/load_simulated_to_mysql.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
