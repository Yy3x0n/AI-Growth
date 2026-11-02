"""A/B 实验统计工具。

按设计文档 §6.2 实现，每个方法的选择理由都写在函数文档里——面试被问到
"为什么不用 Wald 区间"时，答案应该在代码里而不在记忆里。

不依赖 statsmodels 的只有 Newcombe 区间（它没有现成实现），其余检验直接调用
statsmodels / scipy，避免手写公式引入静默错误。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import stats
from statsmodels.stats.proportion import proportions_ztest


# ---------------------------------------------------------------------------
# 设计阶段：样本量与最小可检测效应（互为逆运算，必须共用同一公式）
# ---------------------------------------------------------------------------


def _z_alpha(alpha: float) -> float:
    return float(stats.norm.ppf(1 - alpha / 2))


def _z_beta(power: float) -> float:
    return float(stats.norm.ppf(power))


def required_sample_size(p1: float, p2: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """两独立比例检验的每臂样本量。

    用的是带 pooled 方差的经典公式（Kohavi et al., *Trustworthy Online Controlled
    Experiments*, 式 3.1）。与直接用未 pooled 方差相比，它在 p1≈p2 时更保守。
    """
    if not 0 < p1 < 1 or not 0 < p2 < 1:
        raise ValueError("p1/p2 必须在 (0,1) 内")
    if p1 == p2:
        raise ValueError("两比例相等时无法计算样本量")

    p_bar = (p1 + p2) / 2
    numerator = (
        _z_alpha(alpha) * math.sqrt(2 * p_bar * (1 - p_bar))
        + _z_beta(power) * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    ) ** 2
    return math.ceil(numerator / (p2 - p1) ** 2)


def mde_for_n(n_arm: int, p1: float, alpha: float = 0.05, power: float = 0.80) -> float:
    """给定每臂样本量，反解可检测的**绝对**提升幅度。

    没有闭式解（n 同时出现在两个根号里），用二分法求解。
    这个方向才是真实项目里的顺序：样本量往往由流量决定，不是由愿望决定。
    """
    lo, hi = 1e-9, 1 - p1
    for _ in range(200):
        mid = (lo + hi) / 2
        if required_sample_size(p1, p1 + mid, alpha, power) > n_arm:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# 分析阶段
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score 区间。

    不用 Wald（p̂ ± z·√(p̂(1-p̂)/n)）：小样本或 p̂ 接近 0/1 时 Wald 会越界、
    且覆盖率明显低于名义水平。Wilson 在这两种情况下都稳定。
    """
    if n == 0:
        return (0.0, 1.0)
    z = _z_alpha(alpha)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (max(0.0, center - half), min(1.0, center + half))


def newcombe_difference_ci(
    s1: int, n1: int, s2: int, n2: int, alpha: float = 0.05
) -> tuple[float, float]:
    """两组比例差的 Newcombe 混合评分区间（method 10）。

    为什么不用 Wald 差值区间：它在两组都接近 0/1 或样本不均衡时会明显偏窄，
    造成"看起来显著、实际不显著"。Newcombe 由两个 Wilson 区间拼合，实测覆盖率
    更接近 95%。本项目的 CI 覆盖率在 scripts/validate_ab_harness.py 里被实测验证。
    """
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson_interval(s1, n1, alpha)
    l2, u2 = wilson_interval(s2, n2, alpha)
    diff = p1 - p2
    lower = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (lower, upper)


def srm_check(n_control: int, n_treatment: int, expected_ratio: float = 0.5) -> dict:
    """样本比例失衡检验（Sample Ratio Mismatch）。

    用卡方拟合优度。阈值取 α=0.001 而非 0.05：SRM 检验每天/每实验都在跑，
    0.05 会产生大量假警报；而一旦真的失衡，后果是整场实验作废，所以宁可阈值严。
    """
    total = n_control + n_treatment
    expected = [total * expected_ratio, total * (1 - expected_ratio)]
    observed = [n_control, n_treatment]
    chi2, p = stats.chisquare(observed, f_exp=expected)
    return {
        "control": n_control,
        "treatment": n_treatment,
        "observed_ratio": n_control / total if total else float("nan"),
        "chi2": float(chi2),
        "p_value": float(p),
        "is_srm": bool(p < 0.001),
    }


@dataclass
class ProportionResult:
    """一次比例型指标的检验结果。"""

    name: str
    control_success: int
    control_n: int
    treatment_success: int
    treatment_n: int
    control_rate: float = field(init=False)
    treatment_rate: float = field(init=False)
    absolute_lift: float = field(init=False)
    relative_lift: float = field(init=False)
    z_stat: float = field(init=False)
    p_value: float = field(init=False)
    ci_low: float = field(init=False)
    ci_high: float = field(init=False)

    def __post_init__(self) -> None:
        self.control_rate = self.control_success / self.control_n
        self.treatment_rate = self.treatment_success / self.treatment_n
        self.absolute_lift = self.treatment_rate - self.control_rate
        self.relative_lift = (
            self.absolute_lift / self.control_rate if self.control_rate else float("nan")
        )
        # 双侧 z 检验；注意 proportions_ztest 的备择假设是 p1 - p2 ≠ 0
        stat, p = proportions_ztest(
            [self.treatment_success, self.control_success],
            [self.treatment_n, self.control_n],
            alternative="two-sided",
        )
        self.z_stat = float(stat)
        self.p_value = float(p)
        self.ci_low, self.ci_high = newcombe_difference_ci(
            self.treatment_success, self.treatment_n,
            self.control_success, self.control_n,
        )

    @property
    def is_significant(self) -> bool:
        return self.p_value < 0.05

    def as_row(self) -> dict:
        return {
            "metric": self.name,
            "control_rate": round(self.control_rate, 6),
            "treatment_rate": round(self.treatment_rate, 6),
            "absolute_lift": round(self.absolute_lift, 6),
            "relative_lift": round(self.relative_lift, 4),
            "ci_low": round(self.ci_low, 6),
            "ci_high": round(self.ci_high, 6),
            "z_stat": round(self.z_stat, 4),
            "p_value": round(self.p_value, 6),
            "significant": bool(self.is_significant),
        }


def welch_test(control: np.ndarray, treatment: np.ndarray) -> dict:
    """连续型指标的 Welch t 检验（不假设等方差）。

    不假设方差齐性：引导组可能同时改变均值和波动，等方差假设没有依据。
    """
    stat, p = stats.ttest_ind(treatment, control, equal_var=False)
    diff = float(np.mean(treatment) - np.mean(control))
    # Welch-Satterthwaite 自由度下的差值 CI
    se = math.sqrt(
        np.var(control, ddof=1) / len(control) + np.var(treatment, ddof=1) / len(treatment)
    )
    dof = se**4 / (
        (np.var(control, ddof=1) / len(control)) ** 2 / (len(control) - 1)
        + (np.var(treatment, ddof=1) / len(treatment)) ** 2 / (len(treatment) - 1)
    )
    t_crit = stats.t.ppf(0.975, dof)
    return {
        "control_mean": float(np.mean(control)),
        "treatment_mean": float(np.mean(treatment)),
        "absolute_lift": diff,
        "ci_low": diff - t_crit * se,
        "ci_high": diff + t_crit * se,
        "t_stat": float(stat),
        "p_value": float(p),
        "significant": bool(p < 0.05),
    }


# ---------------------------------------------------------------------------
# 决策框架
# ---------------------------------------------------------------------------


def decide(
    primary: ProportionResult,
    design_mde_absolute: float,
    guardrail_results: list[dict],
    srm: dict,
) -> dict:
    """按设计文档 §6.2 的三重条件给出上线判断。

    统计显著、效应达到设计 MDE、护栏未恶化——三者缺一即"证据不足"。
    这个顺序不能调换：只看 p 值会让"显著但效应微小"和"显著但有副作用"两类
    结果都通过，正是 A/B 实践里最常见的两类误判。
    """
    reasons: list[str] = []

    if srm["is_srm"]:
        reasons.append(f"SRM 检验失败（p={srm['p_value']:.4f}），分组不可信，结果作废")

    if not primary.is_significant:
        reasons.append(f"主指标不显著（p={primary.p_value:.4f}）")
    elif primary.absolute_lift < design_mde_absolute:
        reasons.append(
            f"主指标显著但效应未达设计 MDE（实测 {primary.absolute_lift:+.4f} "
            f"< 设计 {design_mde_absolute:+.4f}）"
        )

    worsened = [g for g in guardrail_results if g.get("is_harm")]
    for g in worsened:
        reasons.append(f"护栏指标恶化：{g['metric']}（p={g['p_value']:.4f}）")

    supported = not reasons
    return {
        "decision": "支持上线" if supported else "证据不足，不建议直接上线",
        "supported": supported,
        "reasons": reasons,
    }
