#!/usr/bin/env python
"""生成图表与自包含 HTML 看板。

为什么不用 Tableau / Power BI
    任务书把看板列为"可选"。Tableau 需要桌面客户端与本机安装，别人克隆仓库跑不起来，
    而"可复现"是本项目的硬要求。因此改为：matplotlib 出静态图 → 内嵌 base64 进单个 HTML。
    产物双击即可看，不依赖网络、不依赖任何外部 JS。

中文乱码的处理
    matplotlib 默认字体不含 CJK，会显示成方框。这里显式指定 PingFang SC（macOS 自带），
    并在找不到时回退到其它常见中文字体；同时关闭 unicode_minus，否则负号也会变方块。
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无 GUI 环境必须用 Agg，否则 savefig 会报错

import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config  # noqa: E402

# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------

CJK_FONTS = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS", "Songti SC"]

COLORS = {
    "primary": "#2F6BFF",
    "accent": "#E8543F",
    "green": "#1F9D6B",
    "amber": "#D98E04",
    "muted": "#9AA4B2",
    "grid": "#E5E8EE",
    "text": "#1F2430",
}


def setup_style() -> str:
    """配置中文字体与统一风格，返回实际使用的字体名。"""
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = next((f for f in CJK_FONTS if f in available), None)

    plt.rcParams.update({
        "font.sans-serif": ([chosen] if chosen else []) + ["DejaVu Sans"],
        "font.family": "sans-serif",
        "axes.unicode_minus": False,  # 否则负号显示为方块
        "figure.dpi": 160,
        "savefig.dpi": 160,
        "savefig.bbox": "tight",
        "axes.edgecolor": COLORS["grid"],
        "axes.labelcolor": COLORS["text"],
        "text.color": COLORS["text"],
        "xtick.color": "#5A6472",
        "ytick.color": "#5A6472",
        "axes.grid": True,
        "grid.color": COLORS["grid"],
        "grid.linewidth": 0.8,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })
    return chosen or "DejaVu Sans（未找到中文字体，中文可能显示为方块）"


def load(name: str) -> pd.DataFrame:
    path = config.TABLE_DIR / f"{name}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {path}，请先跑完阶段 2/3 的分析脚本")
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# 各图
# ---------------------------------------------------------------------------


def fig_revenue_concentration() -> plt.Figure:
    """分层：账户占比 vs 收入占比。核心结论是"少数账户撑起多数收入"。"""
    d = load("segment_summary").sort_values("revenue_share", ascending=True)

    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    y = range(len(d))
    ax.barh([i + 0.2 for i in y], d["account_share"] * 100, height=0.38,
            color=COLORS["muted"], label="账户占比")
    ax.barh([i - 0.2 for i in y], d["revenue_share"] * 100, height=0.38,
            color=COLORS["primary"], label="收入占比")

    ax.set_yticks(list(y))
    ax.set_yticklabels(d["segment"])
    ax.set_xlabel("占比（%）")
    ax.set_title("客户分层：账户占比与收入占比并不一致", fontsize=13, pad=12, loc="left")
    ax.legend(frameon=False, loc="lower right")
    ax.set_xlim(0, 65)

    for i, (_, row) in enumerate(d.iterrows()):
        ax.text(row["revenue_share"] * 100 + 0.8, i - 0.2, f"{row['revenue_share']*100:.1f}%",
                va="center", fontsize=8.5, color=COLORS["primary"])

    high = d[d["segment"] == "高价值"]
    if len(high):
        ax.annotate(
            f"高价值层 {high['account_share'].iloc[0]*100:.1f}% 账户 → {high['revenue_share'].iloc[0]*100:.1f}% 收入",
            xy=(high["revenue_share"].iloc[0] * 100, list(d["segment"]).index("高价值") - 0.2),
            xytext=(34, 1.2), fontsize=9, color=COLORS["accent"],
            arrowprops=dict(arrowstyle="->", color=COLORS["accent"], lw=1.1),
        )
    return fig


def fig_mrr_trend() -> plt.Figure:
    """月度 MRR 与付费账户：双轴，避免两个量纲挤在一根轴上互相误导。"""
    d = load("core_kpis").copy()
    d["month"] = pd.to_datetime(d["month_start"])

    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(d["month"], d["mrr_total"] / 1000, color=COLORS["primary"], lw=2, label="MRR（千元）")
    ax.fill_between(d["month"], d["mrr_total"] / 1000, color=COLORS["primary"], alpha=0.12)
    ax.set_ylabel("MRR（千元）", color=COLORS["primary"])
    ax.tick_params(axis="y", labelcolor=COLORS["primary"])

    ax2 = ax.twinx()
    ax2.plot(d["month"], d["paying_accounts"], color=COLORS["amber"], lw=1.8,
             linestyle="--", label="付费账户数")
    ax2.set_ylabel("付费账户数", color=COLORS["amber"])
    ax2.tick_params(axis="y", labelcolor=COLORS["amber"])
    ax2.grid(False)

    ax.set_title("月度 MRR 与付费账户数（观察期 2023-01 ~ 2024-12）", fontsize=13, pad=12, loc="left")

    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [ln.get_label() for ln in lines], frameon=False, loc="upper left")
    return fig


def fig_ai_cost_mix() -> plt.Figure:
    """请求占比 vs 成本占比：说明"成本杠杆不在调用量最大的功能"。"""
    d = load("ai_feature_summary").copy()
    d["cost"] = d["estimated_cost_usd_at_assumed_price"]
    d["request_share"] = d["requests"] / d["requests"].sum() * 100
    d["cost_share"] = d["cost"] / d["cost"].sum() * 100
    d = d.sort_values("request_share", ascending=False)

    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    x = range(len(d))
    ax.bar([i - 0.2 for i in x], d["request_share"], width=0.38,
           color=COLORS["muted"], label="请求占比")
    ax.bar([i + 0.2 for i in x], d["cost_share"], width=0.38,
           color=COLORS["accent"], label="成本占比")

    ax.set_xticks(list(x))
    ax.set_xticklabels(d["feature_name"])
    ax.set_ylabel("占比（%）")
    ax.set_ylim(0, 62)
    ax.set_title("AI 功能：请求量与成本结构错位", fontsize=13, pad=12, loc="left")
    ax.legend(frameon=False)

    for i, (_, row) in enumerate(d.iterrows()):
        ax.text(i - 0.2, row["request_share"] + 1.2, f"{row['request_share']:.1f}%",
                ha="center", fontsize=8.5)
        ax.text(i + 0.2, row["cost_share"] + 1.2, f"{row['cost_share']:.1f}%",
                ha="center", fontsize=8.5, color=COLORS["accent"])
    return fig


def fig_ab_result() -> plt.Figure:
    """A/B：主指标与两个护栏的点估计 + 95% CI。跨 0 即不显著，一眼可读。"""
    primary = load("ab_test_primary").iloc[0]
    guards = load("ab_test_guardrails")

    labels = ["主指标\n7日首次有效任务率"]
    lows = [primary["CI_下限"]]
    highs = [primary["CI_上限"]]
    points = [primary["绝对提升"]]
    pvalues = [primary["p值"]]
    kinds = ["primary"]

    for _, row in guards.iterrows():
        name = {"post_7d_error_rate": "护栏\n7日后错误率", "support_ticket_7d": "护栏\n7日内工单率"}.get(
            row["metric"], row["metric"]
        )
        labels.append(name)
        lows.append(row["ci_low"])
        highs.append(row["ci_high"])
        points.append(row["absolute_lift"])
        pvalues.append(row["p_value"])
        kinds.append("guard")

    fig, ax = plt.subplots(figsize=(8.2, 3.9))
    y = list(range(len(labels)))[::-1]

    for yi, lo, hi, pt, p, kind in zip(y, lows, highs, points, pvalues, kinds):
        color = COLORS["primary"] if kind == "primary" else COLORS["accent"]
        ax.plot([lo * 100, hi * 100], [yi, yi], color=color, lw=2.2, solid_capstyle="round")
        ax.plot(pt * 100, yi, "o", color=color, ms=7)
        sig = "显著" if p < 0.05 else "不显著"
        ax.text(hi * 100 + 0.22, yi, f"{pt*100:+.2f}pp（p={p:.3f}，{sig}）",
                va="center", fontsize=8.8, color=color)

    ax.axvline(0, color="#5A6472", lw=1, linestyle=":")
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("实验组 − 对照组（百分点，95% CI）")
    ax.set_title("A/B 结果：主指标不显著，两个护栏显著恶化", fontsize=13, pad=12, loc="left")
    ax.set_xlim(min(lows) * 100 - 1.2, max(highs) * 100 + 9)
    return fig


def fig_usage_concentration() -> plt.Figure:
    """请求集中度：前 N% 账户贡献多少请求。"""
    d = load("ai_usage_concentration")
    shares = [float(s.strip("%")) for s in d["account_share"].astype(str)]
    requests = d["request_share"] * 100

    fig, ax = plt.subplots(figsize=(7.4, 3.6))
    ax.plot([0] + shares + [100], [0] + list(requests) + [100],
            color=COLORS["primary"], lw=2, marker="o", ms=5)
    ax.plot([0, 100], [0, 100], color=COLORS["muted"], lw=1, linestyle="--",
            label="完全均等分布")

    for s, r in zip(shares, requests):
        ax.annotate(f"{s:.0f}% 账户 → {r:.1f}% 请求", xy=(s, r),
                    xytext=(6, -12), textcoords="offset points", fontsize=8.5,
                    color=COLORS["primary"])

    ax.set_xlabel("账户累计占比（%，按请求量降序）")
    ax.set_ylabel("请求累计占比（%）")
    ax.set_title("AI 请求高度集中：前 10% 账户贡献过半请求", fontsize=13, pad=12, loc="left")
    ax.legend(frameon=False, loc="lower right")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    return fig


def fig_rag_eval() -> plt.Figure:
    """检索评估：三种配置对比，并标出"每文档配额"修掉的那一项。"""
    d = load("rag_eval_summary")
    metrics = ["Hit@3", "Recall@3", "MRR"]
    labels = list(d["口径"])

    fig, ax = plt.subplots(figsize=(8.4, 3.8))
    x = range(len(metrics))
    width = 0.26
    palette = [COLORS["primary"], COLORS["muted"], COLORS["amber"]]

    for i, (_, row) in enumerate(d.iterrows()):
        offset = (i - (len(d) - 1) / 2) * width
        values = [row[m] for m in metrics]
        bars = ax.bar([xi + offset for xi in x], values, width=width,
                      color=palette[i % len(palette)], label=labels[i])
        for b, v in zip(bars, values):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.012, f"{v:.3f}",
                    ha="center", fontsize=7.8)

    ax.set_xticks(list(x))
    ax.set_xticklabels(metrics)
    ax.set_ylim(0.8, 1.05)
    ax.set_ylabel("指标值")
    ax.set_title("检索评估：18 题评估集，三种配置对比", fontsize=13, pad=12, loc="left")
    ax.legend(frameon=False, loc="lower left", fontsize=8.5)
    return fig


# ---------------------------------------------------------------------------
# HTML 看板
# ---------------------------------------------------------------------------


def save_and_encode(fig: plt.Figure, path: Path) -> str:
    """同一张图既落盘 PNG，也编码成 base64 供 HTML 内嵌。

    只 build 一次、savefig 两次（一次到磁盘、一次到内存缓冲），
    不要把图表函数再跑一遍——那会让每张图的计算做两次，且两张图未必完全一致。
    """
    buffer = io.BytesIO()
    fig.savefig(path, facecolor="white")
    fig.savefig(buffer, format="png", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_html(images: dict[str, str], facts: dict[str, str], font_note: str) -> str:
    cards = "".join(
        f'<div class="card"><div class="card-value">{value}</div>'
        f'<div class="card-label">{label}</div></div>'
        for label, value in facts.items()
    )

    sections = [
        ("客户结构与收入集中度", "fig_segments",
         "六层互斥分层，阈值全部取自实际分布并落库为 <code>segment_threshold</code> 单表。"
         "按定义「高价值」上限为 25% 账户，因此它贡献超过一半收入说明收入高度集中，"
         "而不是分层规则人为制造了集中。"),
        ("收入与付费规模趋势", "fig_mrr",
         "MRR 与付费账户同步上行。注意 2023 年初的 MRR 基数极低（两个付费账户），"
         "因此早期的环比增速不具参考价值，宜看绝对增量。"),
        ("AI 功能的请求与成本错位", "fig_cost",
         "文档总结占约三成请求却吃掉过半成本，根因在输入 token 体量。"
         "按请求量做容量规划会低估成本。成本为「假设单价」估算，非真实账单。"),
        ("AI 使用集中度", "fig_conc",
         "请求高度集中意味着：容量优化与体验治理都应优先覆盖头部账户，"
         "但也要注意尾部账户一旦流失对总量的影响有限。"),
        ("A/B 实验：不上线的结论", "fig_ab",
         "主指标方向为正但不显著，两个护栏显著恶化，因此判定「证据不足，不建议上线」。"
         "该实验为模拟，效应参数由脚本显式设定，p 值不构成真实业务证据。"),
        ("RAG 检索评估", "fig_rag",
         "18 题评估集在建索引前冻结。Hit@3 为 1.0 但 Top1 准确率仅 0.8667，"
         "说明有两题第一名是错的、被 Hit@3 掩盖。该评估集规模小且为自建，指标偏乐观。"),
    ]

    body = ""
    for title, key, note in sections:
        body += (
            f'<section><h2>{title}</h2>'
            f'<img src="data:image/png;base64,{images[key]}" alt="{title}">'
            f'<p class="note">{note}</p></section>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 应用增长与数据运营看板</title>
<style>
  :root {{
    --bg: #F7F8FA; --panel: #FFFFFF; --text: #1F2430; --muted: #6B7480;
    --line: #E5E8EE; --primary: #2F6BFF; --accent: #E8543F;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
    line-height: 1.65;
  }}
  .wrap {{ max-width: 1040px; margin: 0 auto; padding: 36px 24px 64px; }}
  header h1 {{ font-size: 26px; margin: 0 0 6px; letter-spacing: -0.2px; }}
  header .sub {{ color: var(--muted); font-size: 14px; margin-bottom: 22px; }}
  .banner {{
    background: #FFF4F0; border: 1px solid #FFD9CE; border-left: 4px solid var(--accent);
    border-radius: 8px; padding: 14px 16px; font-size: 13px; color: #7A2E1E; margin-bottom: 26px;
  }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 30px; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }}
  .card-value {{ font-size: 21px; font-weight: 600; color: var(--primary); letter-spacing: -0.3px; }}
  .card-label {{ font-size: 12.5px; color: var(--muted); margin-top: 4px; }}
  section {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 22px 22px 16px; margin-bottom: 20px;
  }}
  section h2 {{ font-size: 16px; margin: 0 0 14px; }}
  section img {{ width: 100%; height: auto; display: block; border-radius: 6px; }}
  .note {{ font-size: 13px; color: var(--muted); margin: 14px 2px 0; }}
  code {{ background: #EEF2FB; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  footer {{ margin-top: 28px; font-size: 12.5px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI 应用增长与数据运营看板</h1>
    <div class="sub">数据区间 2023-01-01 ~ 2024-12-31　·　账户 500　·　生成于 {facts['生成时间']}</div>
  </header>

  <div class="banner">
    <strong>数据边界：</strong>本看板基于公开<strong>合成</strong> SaaS 数据集（T1）与固定种子生成的
    <strong>模拟</strong>日志（T2）、模拟实验（T3）。所有数字均为方法演示，<strong>不代表任何真实企业业绩</strong>。
    AI 成本为假设单价估算值，A/B 结论不构成真实业务证据。
  </div>

  <div class="cards">{cards}</div>

  {body}

  <footer>
    图表由 <code>scripts/build_dashboard.py</code> 生成，图片已内嵌，离线可看。
    中文字体：{font_note}。<br>
    完整口径与限制见 <code>docs/metric_dictionary.md</code> 与
    <code>docs/data_capability_assessment.md</code>。
  </footer>
</div>
</body>
</html>
"""


def collect_facts() -> dict[str, str]:
    seg = load("segment_summary")
    high = seg[seg["segment"] == "高价值"]
    kpi = load("core_kpis")
    primary = load("ab_test_primary").iloc[0]
    rag = load("rag_eval_summary").iloc[0]
    ai = load("ai_monthly_summary")
    conc = load("ai_usage_concentration")

    return {
        "总 MRR（末月）": f"{kpi['mrr_total'].iloc[-1] / 1000:,.0f} 千元",
        "高价值层收入占比": f"{high['revenue_share'].iloc[0]*100:.1f}%" if len(high) else "—",
        "高价值层账户占比": f"{high['account_share'].iloc[0]*100:.1f}%" if len(high) else "—",
        "A/B 主指标": f"{primary['绝对提升']*100:+.2f}pp（p={primary['p值']:.3f}）",
        "RAG Hit@3": f"{rag['Hit@3']:.2f}",
        "AI 请求总量": f"{ai['requests'].sum():,}",
        "前 10% 账户请求占比": f"{conc['request_share'].iloc[0]*100:.1f}%",
        "生成时间": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    }


def main() -> int:
    font_note = setup_style()
    print(f"中文字体：{font_note}")

    config.FIGURE_DIR.mkdir(parents=True, exist_ok=True)

    builders = {
        "fig_segments": fig_revenue_concentration,
        "fig_mrr": fig_mrr_trend,
        "fig_cost": fig_ai_cost_mix,
        "fig_ab": fig_ab_result,
        "fig_conc": fig_usage_concentration,
        "fig_rag": fig_rag_eval,
    }

    images: dict[str, str] = {}
    for key, builder in builders.items():
        fig = builder()
        images[key] = save_and_encode(fig, config.FIGURE_DIR / f"{key}.png")
        print(f"  已生成 {key}.png")

    html = build_html(images, collect_facts(), font_note)
    out = config.OUTPUT_DIR / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    size_mb = out.stat().st_size / 1e6
    print(f"\n看板已生成：{out}（{size_mb:.2f} MB，图片内嵌，离线可看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
