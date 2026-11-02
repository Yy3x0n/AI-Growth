#!/usr/bin/env python
"""生成单文件交互式 HTML 看板（outputs/dashboard.html）。

设计取舍
    1. 单文件 + 内联 ECharts：双击就能看，不依赖网络、不依赖本地服务。
       代价是文件约 1 MB，但换来"发给面试官也能直接打开"这个确定性，
       而 ECharts 本身是固定资源，不随数据变化，所以只保留一份在 src/assets。
    2. 数据在构建期内联成 JSON，不做前端取数——看板是"快照"而非"应用"，
       面试场景下没人会去起后端。
    3. 每节只放 2–3 张图并给一句结论，避免堆图。图的顺序对应分析链条：
       增长发生了什么 → 谁在贡献 → AI 用得好不好 → 改动该不该上线。

产物：outputs/dashboard.html
对应文档：docs/dashboard_design.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config  # noqa: E402
from src.segmentation import SEGMENT_ORDER  # noqa: E402

SEGMENT_COLORS = {
    "零 MRR": "#B0B7C3",
    "风险关注": "#E8543F",
    "高价值": "#2F6BFF",
    "深度采用": "#1F9D6B",
    "浅层采用": "#D98E04",
    "稳健基础": "#7C5CFF",
}

PALETTE = ["#2F6BFF", "#E8543F", "#1F9D6B", "#D98E04", "#7C5CFF", "#00A6A6"]


def _read(name: str) -> pd.DataFrame:
    path = config.BI_DIR / f"{name}.csv"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，请先跑 scripts/build_bi_dataset.py")
    return pd.read_csv(path, encoding="utf-8-sig")


def _read_table(name: str) -> pd.DataFrame:
    """分层汇总是分析产物，不用再拷一份到 BI 层。"""
    path = config.TABLE_DIR / f"{name}.csv"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，请先跑 scripts/run_analysis.py")
    return pd.read_csv(path, encoding="utf-8-sig")


def build_payload() -> dict:
    kpi = _read("fact_kpi_monthly")
    seg = _read_table("segment_summary")
    dim = _read("dim_account")
    ai = _read("fact_ai_monthly")
    feat = _read("fact_ai_feature")
    ab = _read("fact_ab_arm")
    rag = _read("fact_rag_variant")

    seg = seg.copy()
    seg["_order"] = seg["segment"].map({s: i for i, s in enumerate(SEGMENT_ORDER)})
    seg = seg.sort_values("_order")

    # 散点按分层分组，前端一次遍历就能画
    scatter: dict[str, list[list[float]]] = {}
    for name in SEGMENT_ORDER:
        sub = dim[dim["segment"] == name]
        if sub.empty:
            continue
        scatter[name] = [
            [round(float(u), 1), round(float(m), 1)]
            for u, m in zip(sub["usage_records"], sub["mrr_latest"])
            if pd.notna(u) and pd.notna(m)
        ]

    last = kpi.iloc[-1]
    first = kpi.iloc[0]

    return {
        "meta": {
            "range": f"{kpi['month_label'].iloc[0]} ~ {kpi['month_label'].iloc[-1]}",
            "months": len(kpi),
            "accounts": int(len(dim)),
            "segments": len(seg),
            "rows": 33100,
        },
        "headline": {
            "mrr_total": float(kpi["mrr_total"].sum()),
            "mrr_last": float(last["mrr_total"]),
            "mrr_growth": float(last["mrr_total"] / first["mrr_total"] - 1) if first["mrr_total"] else 0,
            "active_last": int(last["active_accounts"]),
            "paying_last": int(last["paying_accounts"]),
            "ai_requests": int(ai["requests"].sum()),
            "ai_cost": float(ai["cost_usd"].sum()),
            "ai_success": float(ai["success_requests"].sum() / ai["requests"].sum()),
        },
        "kpi": {
            "months": kpi["month_label"].tolist(),
            "mrr": [round(float(x), 2) for x in kpi["mrr_total"]],
            "active": kpi["active_accounts"].astype(int).tolist(),
            "paying": kpi["paying_accounts"].astype(int).tolist(),
            "arpa": [round(float(x), 2) for x in kpi["arpa"]],
            "upgrades": kpi["upgrades"].astype(int).tolist(),
            "downgrades": kpi["downgrades"].astype(int).tolist(),
        },
        "segments": [
            {
                "name": r["segment"],
                "accounts": int(r["accounts"]),
                "mrr": float(r["mrr_total"]),
                "account_share": round(float(r["account_share"]) * 100, 1),
                "revenue_share": round(float(r["revenue_share"]) * 100, 1),
                "churn_rate": round(float(r["churn_event_rate"]) * 100, 1),
                "avg_csat": round(float(r["avg_csat"]), 2),
                "avg_error": round(float(r["avg_error_rate"]) * 100, 1),
                "color": SEGMENT_COLORS.get(r["segment"], "#9AA4B2"),
            }
            for _, r in seg.iterrows()
        ],
        "scatter": scatter,
        "ai": {
            "months": ai["month_label"].tolist(),
            "requests": ai["requests"].astype(int).tolist(),
            "cost": [round(float(x), 4) for x in ai["cost_usd"]],
            "success": [round(float(x) * 100, 2) for x in ai["success_rate"]],
            "p95": [round(float(x), 0) for x in ai["p95_latency_ms"]],
        },
        "features": [
            {
                "name": r["feature_name"],
                "requests": int(r["requests"]),
                "cost": round(float(r["cost_usd"]), 2),
                "cost_per_1k": round(float(r["cost_per_1k_requests"]), 2),
                "success": round(float(r["success_rate"]) * 100, 1),
                "request_share": round(float(r["request_share"]) * 100, 1),
                "cost_share": round(float(r["cost_share"]) * 100, 1),
            }
            for _, r in feat.sort_values("requests", ascending=False).iterrows()
        ],
        "ab": [
            {
                "metric": r["metric_label"],
                "type": r["metric_type"],
                "arm": r["arm"],
                "rate": round(float(r["rate"]) * 100, 2),
                "n": int(r["n"]),
                "ci_low": None if pd.isna(r["ci_low"]) else round(float(r["ci_low"]) * 100, 2),
                "ci_high": None if pd.isna(r["ci_high"]) else round(float(r["ci_high"]) * 100, 2),
                "p": None if pd.isna(r["p_value"]) else round(float(r["p_value"]), 4),
                "harm": bool(r["is_harm"]),
            }
            for _, r in ab.iterrows()
        ],
        "rag": [
            {
                "variant": r["variant"],
                "metric": r["metric_label"],
                "score": round(float(r["score"]) * 100, 1),
            }
            for _, r in rag.iterrows()
        ],
    }


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI 增长数据运营看板</title>
<style>
  :root {
    --bg: #F4F6FA;
    --panel: #FFFFFF;
    --ink: #161B26;
    --ink-2: #5A6478;
    --ink-3: #8B95A7;
    --line: #E6EAF2;
    --brand: #2F6BFF;
    --danger: #E8543F;
    --ok: #1F9D6B;
    --warn: #D98E04;
    --radius: 14px;
    --shadow: 0 1px 2px rgba(22, 27, 38, .04), 0 8px 24px -12px rgba(22, 27, 38, .12);
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1320px; margin: 0 auto; padding: 0 28px 72px; }

  /* ---------- 顶部 ---------- */
  header.top {
    position: sticky; top: 0; z-index: 50;
    background: rgba(244, 246, 250, .86);
    backdrop-filter: saturate(180%) blur(14px);
    border-bottom: 1px solid var(--line);
  }
  .top-inner {
    max-width: 1320px; margin: 0 auto; padding: 14px 28px;
    display: flex; align-items: center; justify-content: space-between; gap: 24px;
  }
  .brand { display: flex; align-items: center; gap: 11px; font-weight: 700; letter-spacing: -.01em; }
  .logo {
    width: 30px; height: 30px; border-radius: 9px; flex: none;
    background: linear-gradient(135deg, #2F6BFF, #7C5CFF);
    display: grid; place-items: center; color: #fff; font-size: 14px; font-weight: 800;
  }
  nav.links { display: flex; gap: 4px; }
  nav.links a {
    color: var(--ink-2); text-decoration: none; font-size: 13px; font-weight: 500;
    padding: 6px 12px; border-radius: 8px; transition: .15s;
  }
  nav.links a:hover { background: #EAEEF7; color: var(--ink); }
  .meta-pill {
    font-size: 12px; color: var(--ink-3); white-space: nowrap;
    border: 1px solid var(--line); background: #fff; padding: 5px 11px; border-radius: 999px;
  }

  /* ---------- 页头 ---------- */
  .hero { padding: 40px 0 8px; }
  .hero h1 { font-size: 30px; line-height: 1.25; margin: 0 0 10px; letter-spacing: -.025em; font-weight: 750; }
  .hero p { margin: 0; color: var(--ink-2); max-width: 760px; font-size: 14.5px; }

  /* ---------- KPI ---------- */
  .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 28px 0 8px; }
  .kpi {
    background: var(--panel); border-radius: var(--radius); padding: 18px 20px 16px;
    box-shadow: var(--shadow); border: 1px solid rgba(230, 234, 242, .8);
  }
  .kpi .label { font-size: 12.5px; color: var(--ink-3); letter-spacing: .02em; }
  .kpi .value { font-size: 27px; font-weight: 700; letter-spacing: -.03em; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
  .kpi .sub { font-size: 12px; color: var(--ink-3); }
  .kpi .sub b { font-weight: 600; }
  .up { color: var(--ok); } .down { color: var(--danger); }

  /* ---------- 分节 ---------- */
  section { margin-top: 44px; scroll-margin-top: 76px; }
  .sec-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
  .sec-head h2 { font-size: 19px; margin: 0; letter-spacing: -.015em; font-weight: 700; }
  .sec-head .idx {
    font-size: 11px; font-weight: 700; color: var(--brand); background: #E9EFFF;
    padding: 2px 8px; border-radius: 6px; letter-spacing: .04em;
  }
  .sec-note { color: var(--ink-2); margin: 0 0 18px; font-size: 13.5px; max-width: 900px; }

  .cards { display: grid; gap: 16px; }
  .g2 { grid-template-columns: 1fr 1fr; }
  .g3 { grid-template-columns: 1fr 1fr 1fr; }
  .card {
    background: var(--panel); border-radius: var(--radius); padding: 18px 20px 8px;
    box-shadow: var(--shadow); border: 1px solid rgba(230, 234, 242, .8);
  }
  .card h3 { font-size: 13.5px; margin: 0 0 2px; font-weight: 650; letter-spacing: -.005em; }
  .card .hint { font-size: 12px; color: var(--ink-3); margin: 0 0 6px; }
  .chart { width: 100%; height: 260px; }
  .chart.tall { height: 320px; }
  .takeaway {
    margin: 2px 0 12px; padding-top: 10px; border-top: 1px dashed var(--line);
    font-size: 12.5px; color: var(--ink-2); display: flex; gap: 7px; align-items: flex-start;
  }
  .takeaway i { font-style: normal; color: var(--brand); font-weight: 700; flex: none; }

  /* ---------- 表格 ---------- */
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: right; color: var(--ink-3); font-weight: 600; padding: 8px 10px;
    border-bottom: 1px solid var(--line); white-space: nowrap; font-size: 12px;
  }
  th:first-child, td:first-child { text-align: left; }
  td { text-align: right; padding: 9px 10px; border-bottom: 1px solid #F1F4F9; font-variant-numeric: tabular-nums; }
  tbody tr:last-child td { border-bottom: none; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: 1px; }
  .tag { font-size: 11px; padding: 1px 7px; border-radius: 5px; font-weight: 600; }
  .tag.bad { background: #FDECEA; color: var(--danger); }
  .tag.good { background: #E8F6F0; color: var(--ok); }
  .tag.mid { background: #FFF4E0; color: var(--warn); }

  footer {
    margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--line);
    font-size: 12px; color: var(--ink-3); display: flex; justify-content: space-between; gap: 20px; flex-wrap: wrap;
  }
  footer b { color: var(--ink-2); font-weight: 600; }

  @media (max-width: 1080px) {
    .kpis { grid-template-columns: repeat(2, 1fr); }
    .g3 { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 760px) {
    .wrap { padding: 0 16px 48px; }
    .top-inner { padding: 12px 16px; }
    nav.links { display: none; }
    .kpis, .g2, .g3 { grid-template-columns: 1fr; }
    .hero h1 { font-size: 23px; }
  }
</style>
</head>
<body>

<header class="top">
  <div class="top-inner">
    <div class="brand"><span class="logo">AG</span> AI 增长数据运营看板</div>
    <nav class="links">
      <a href="#growth">增长</a>
      <a href="#segment">分层</a>
      <a href="#ai">AI 运营</a>
      <a href="#exp">实验</a>
    </nav>
    <span class="meta-pill" id="metaPill"></span>
  </div>
</header>

<div class="wrap">
  <div class="hero">
    <h1>增长从哪来，AI 有没有真的被用起来</h1>
    <p>本期看板回答四个问题：MRR 是怎么涨上去的、钱集中在哪类客户身上、AI 功能的使用与成本结构是否健康、以及这次引导改版该不该上线。数据来自 RavenStack 公开数据集（5 张表 / 33,100 行），AI 相关结论基于按真实用量分布生成的模拟请求日志。</p>
  </div>

  <div class="kpis" id="kpis"></div>

  <section id="growth">
    <div class="sec-head"><span class="idx">01</span><h2>增长</h2></div>
    <p class="sec-note">MRR 在 24 个月里翻了 200 多倍，但拆开看是"少量大客户拉动 + 大量免费账户陪跑"的结构，这决定了后面的运营重点。</p>
    <div class="cards g2">
      <div class="card">
        <h3>MRR 月度趋势</h3><p class="hint">单位：元 / 月</p>
        <div class="chart tall" id="c_mrr"></div>
        <div class="takeaway"><i>›</i><span id="t_mrr"></span></div>
      </div>
      <div class="card">
        <h3>活跃账户与付费账户</h3><p class="hint">账户数 / 月</p>
        <div class="chart tall" id="c_acct"></div>
        <div class="takeaway"><i>›</i><span id="t_acct"></span></div>
      </div>
    </div>
    <div class="cards g2" style="margin-top:16px">
      <div class="card">
        <h3>ARPA（每账户平均收入）</h3><p class="hint">元 / 账户</p>
        <div class="chart" id="c_arpa"></div>
        <div class="takeaway"><i>›</i><span id="t_arpa"></span></div>
      </div>
      <div class="card">
        <h3>升降级事件</h3><p class="hint">笔 / 月</p>
        <div class="chart" id="c_grade"></div>
        <div class="takeaway"><i>›</i><span id="t_grade"></span></div>
      </div>
    </div>
  </section>

  <section id="segment">
    <div class="sec-head"><span class="idx">02</span><h2>客户分层</h2></div>
    <p class="sec-note">按 MRR、功能采用深度、风险信号把 500 个账户切成 6 层。分层不是目的，目的是让"该救谁、该投谁"有依据。</p>
    <div class="cards g2">
      <div class="card">
        <h3>账户数与收入贡献</h3><p class="hint">柱状为账户数，折线为 MRR 占全部收入比重</p>
        <div class="chart tall" id="c_seg"></div>
        <div class="takeaway"><i>›</i><span id="t_seg"></span></div>
      </div>
      <div class="card">
        <h3>使用深度 × 付费金额</h3><p class="hint">每个点是一个账户，颜色代表分层</p>
        <div class="chart tall" id="c_scatter"></div>
        <div class="takeaway"><i>›</i><span id="t_scatter"></span></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px;padding-bottom:18px">
      <h3>分层画像明细</h3><p class="hint">流失率 = 该层中发生过流失事件的账户占比</p>
      <table>
        <thead><tr>
          <th>分层</th><th>账户数</th><th>账户占比</th><th>MRR 合计</th><th>收入占比</th>
          <th>流失率</th><th>平均满意度</th><th>平均错误率</th>
        </tr></thead>
        <tbody id="segTable"></tbody>
      </table>
    </div>
  </section>

  <section id="ai">
    <div class="sec-head"><span class="idx">03</span><h2>AI 运营</h2></div>
    <p class="sec-note">请求量在 6 个月内涨了 40 倍，但成本增速更陡——真正的杠杆不在"少用"，而在"用得更短"。成本按假设单价折算，口径见数据字典。</p>
    <div class="cards g2">
      <div class="card">
        <h3>请求量与成本</h3><p class="hint">柱：请求数（左轴）／线：成本 USD（右轴）</p>
        <div class="chart tall" id="c_ai"></div>
        <div class="takeaway"><i>›</i><span id="t_ai"></span></div>
      </div>
      <div class="card">
        <h3>成功率与 P95 延迟</h3><p class="hint">成功率（左轴 %）／P95 延迟（右轴 ms）</p>
        <div class="chart tall" id="c_health"></div>
        <div class="takeaway"><i>›</i><span id="t_health"></span></div>
      </div>
    </div>
    <div class="cards g2" style="margin-top:16px">
      <div class="card">
        <h3>功能请求量对比</h3><p class="hint">次</p>
        <div class="chart" id="c_feat"></div>
        <div class="takeaway"><i>›</i><span id="t_feat"></span></div>
      </div>
      <div class="card">
        <h3>成本结构：谁在吃掉预算</h3><p class="hint">千次请求成本，USD</p>
        <div class="chart" id="c_featcost"></div>
        <div class="takeaway"><i>›</i><span id="t_featcost"></span></div>
      </div>
    </div>
  </section>

  <section id="exp">
    <div class="sec-head"><span class="idx">04</span><h2>实验与评估</h2></div>
    <p class="sec-note">A/B 实验用两比例检验 + 功效分析，样本量按 MDE 反解；RAG 检索策略做了三组对照，用于确定线上默认配置。</p>
    <div class="cards g2">
      <div class="card">
        <h3>引导改版：主指标与护栏</h3><p class="hint">误差线为实验组 95% 置信区间</p>
        <div class="chart tall" id="c_ab"></div>
        <div class="takeaway"><i>›</i><span id="t_ab"></span></div>
      </div>
      <div class="card">
        <h3>RAG 检索策略对比</h3><p class="hint">得分（%）越高越好</p>
        <div class="chart tall" id="c_rag"></div>
        <div class="takeaway"><i>›</i><span id="t_rag"></span></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px;padding-bottom:18px">
      <h3>实验结论</h3><p class="hint">主指标 p 值 0.203，未达显著；两项护栏指标反而显著恶化</p>
      <table>
        <thead><tr><th>指标</th><th>对照组</th><th>实验组</th><th>绝对差异</th><th>p 值</th><th>判定</th></tr></thead>
        <tbody id="abTable"></tbody>
      </table>
    </div>
  </section>

  <footer>
    <div><b>数据来源</b>　RavenStack 公开数据集（2023-01 至 2024-12）＋ 按真实用量分布生成的 AI 请求日志</div>
    <div><b>口径</b>　AI 成本为按假设单价的折算值，非真实账单</div>
  </footer>
</div>

<script>/*__ECHARTS__*/</script>
<script>
const D = /*__PAYLOAD__*/;

/* ---------- 通用配置 ---------- */
const FONT = '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", sans-serif';
const INK = '#161B26', INK2 = '#5A6478', INK3 = '#8B95A7', LINE = '#EDF0F6';
const money = v => v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿'
                : v >= 1e4 ? (v / 1e4).toFixed(v >= 1e6 ? 0 : 1) + ' 万'
                : v.toLocaleString();
const num = v => v.toLocaleString('zh-CN');

const baseGrid = { left: 52, right: 20, top: 24, bottom: 30, containLabel: true };
const axisCommon = {
  axisLine: { lineStyle: { color: LINE } },
  axisTick: { show: false },
  axisLabel: { color: INK3, fontSize: 11, fontFamily: FONT },
  splitLine: { lineStyle: { color: LINE, type: 'dashed' } },
};
const tip = {
  trigger: 'axis',
  backgroundColor: 'rgba(22,27,38,.92)',
  borderWidth: 0,
  padding: [9, 12],
  textStyle: { color: '#fff', fontSize: 12, fontFamily: FONT },
  axisPointer: { type: 'line', lineStyle: { color: '#C9D2E3' } },
};

function lineSeries(name, data, color, opts = {}) {
  return {
    name, type: 'line', data, smooth: .25, symbol: 'circle', symbolSize: 5,
    showSymbol: false, lineStyle: { width: 2.2, color },
    itemStyle: { color },
    areaStyle: opts.area ? {
      color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: color + '38' }, { offset: 1, color: color + '02' },
      ]),
    } : undefined,
    yAxisIndex: opts.yAxisIndex || 0,
    emphasis: { focus: 'series' },
  };
}

function barSeries(name, data, color, opts = {}) {
  return {
    name, type: 'bar', data,
    barMaxWidth: opts.barMaxWidth || 26,
    itemStyle: { color, borderRadius: opts.radius === undefined ? [4, 4, 0, 0] : opts.radius },
    yAxisIndex: opts.yAxisIndex || 0,
    emphasis: { focus: 'series' },
  };
}

/* ---------- KPI ---------- */
(function () {
  const h = D.headline;
  const items = [
    { label: '累计 MRR（24 个月）', value: '¥' + money(h.mrr_total), sub: '月末 MRR 求和' },
    { label: '期末 MRR', value: '¥' + money(h.mrr_last),
      sub: `<b class="up">↑ ${h.mrr_growth.toFixed(0)}×</b> 相比首月` },
    { label: '活跃账户 / 付费账户', value: num(h.active_last) + ' / ' + num(h.paying_last),
      sub: '付费转化仍是最大短板' },
    { label: 'AI 请求总量', value: num(h.ai_requests),
      sub: `成功率 <b>${(h.ai_success * 100).toFixed(1)}%</b> · 成本 $${h.ai_cost.toFixed(2)}` },
  ];
  document.getElementById('kpis').innerHTML = items.map(i => `
    <div class="kpi">
      <div class="label">${i.label}</div>
      <div class="value">${i.value}</div>
      <div class="sub">${i.sub}</div>
    </div>`).join('');
  document.getElementById('metaPill').textContent =
    `${D.meta.range} · ${D.meta.accounts} 账户 · ${D.meta.rows.toLocaleString()} 行`;
})();

const charts = [];
function make(id, option) {
  const el = document.getElementById(id);
  const c = echarts.init(el, null, { renderer: 'canvas' });
  c.setOption(option);
  charts.push(c);
  return c;
}
window.addEventListener('resize', () => charts.forEach(c => c.resize()));

/* ---------- 01 增长 ---------- */
make('c_mrr', {
  grid: baseGrid, tooltip: { ...tip, valueFormatter: v => '¥' + num(v) },
  xAxis: { type: 'category', data: D.kpi.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 3 } },
  yAxis: { type: 'value', ...axisCommon, axisLabel: { ...axisCommon.axisLabel, formatter: v => money(v) } },
  series: [lineSeries('MRR', D.kpi.mrr, '#2F6BFF', { area: true })],
});

const mrrFirst = D.kpi.mrr[0], mrrLast = D.kpi.mrr[D.kpi.mrr.length - 1];
const peakIdx = D.kpi.mrr.indexOf(Math.max(...D.kpi.mrr));
document.getElementById('t_mrr').textContent =
  `MRR 从 ¥${num(mrrFirst)} 增至 ¥${num(mrrLast)}，复合月增速约 ${(Math.pow(mrrLast / mrrFirst, 1 / (D.kpi.mrr.length - 1)) * 100 - 100).toFixed(1)}%，峰值出现在 ${D.kpi.months[peakIdx]}。`;

make('c_acct', {
  grid: baseGrid, tooltip: tip,
  legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
            textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
  xAxis: { type: 'category', data: D.kpi.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 3 } },
  yAxis: { type: 'value', ...axisCommon },
  series: [
    lineSeries('活跃账户', D.kpi.active, '#2F6BFF'),
    lineSeries('付费账户', D.kpi.paying, '#E8543F'),
  ],
});

const payRatio = D.headline.paying_last / D.headline.active_last * 100;
document.getElementById('t_acct').textContent =
  `活跃账户稳定在 ${D.headline.active_last} 上下，但付费账户只有 ${D.headline.paying_last} 个（付费率 ${payRatio.toFixed(1)}%）。增长瓶颈不在"拉新"，在"把活跃转成付费"。`;

make('c_arpa', {
  grid: baseGrid, tooltip: { ...tip, valueFormatter: v => '¥' + num(v) },
  xAxis: { type: 'category', data: D.kpi.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 3 } },
  yAxis: { type: 'value', ...axisCommon, axisLabel: { ...axisCommon.axisLabel, formatter: v => money(v) } },
  series: [lineSeries('ARPA', D.kpi.arpa, '#7C5CFF', { area: true })],
});
document.getElementById('t_arpa').textContent =
  'ARPA 从 2,256 元回落到 600 元区间又回升——分母（付费账户）增长快于收入，说明新增付费客户以中小单为主。';

const up = D.kpi.upgrades.reduce((a, b) => a + b, 0);
const down = D.kpi.downgrades.reduce((a, b) => a + b, 0);
make('c_grade', {
  grid: baseGrid, tooltip: tip,
  legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
            textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
  xAxis: { type: 'category', data: D.kpi.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 3 } },
  yAxis: { type: 'value', ...axisCommon },
  series: [
    barSeries('升级', D.kpi.upgrades, '#1F9D6B'),
    barSeries('降级', D.kpi.downgrades, '#E8543F'),
  ],
});
document.getElementById('t_grade').textContent =
  `24 个月累计升级 ${up} 笔、降级 ${down} 笔。升级笔数远多于降级，但绝对量都很小——说明扩张式增购还没形成机制。`;

/* ---------- 02 分层 ---------- */
make('c_seg', {
  grid: { ...baseGrid, right: 52 },
  tooltip: { ...tip, formatter: p => {
    const s = D.segments[p[0].dataIndex];
    return `<b>${s.name}</b><br/>账户数 ${s.accounts}（${s.account_share}%）<br/>收入占比 ${s.revenue_share}%`;
  } },
  legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
            textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
  xAxis: { type: 'category', data: D.segments.map(s => s.name),
           ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 0, fontSize: 11 } },
  yAxis: [
    { type: 'value', name: '账户数', nameTextStyle: { color: INK3, fontSize: 11 },
      ...axisCommon },
    { type: 'value', name: '收入占比 %', nameTextStyle: { color: INK3, fontSize: 11 },
      ...axisCommon, splitLine: { show: false }, axisLine: { show: false },
      axisLabel: { ...axisCommon.axisLabel, formatter: '{value}%' } },
  ],
  series: [
    { ...barSeries('账户数', D.segments.map(s => s.accounts), '#B7C6E8'),
      itemStyle: {
        borderRadius: [4, 4, 0, 0],
        color: p => D.segments[p.dataIndex].color + '99',
      } },
    { ...lineSeries('收入占比', D.segments.map(s => s.revenue_share), '#2F6BFF', { yAxisIndex: 1 }),
      symbolSize: 8, showSymbol: true },
  ],
});

const high = D.segments.find(s => s.name === '高价值');
document.getElementById('t_seg').textContent =
  `${high.name}客户只占 ${high.account_share}% 的账户数，却贡献 ${high.revenue_share}% 的收入；而「零 MRR」层有 ${D.segments[0].accounts} 个账户，收入贡献为 0——这两层是资源投放的两端。`;

/* 散点 */
const scatterSeries = Object.entries(D.scatter).map(([name, pts]) => ({
  name, type: 'scatter', data: pts, symbolSize: 6,
  itemStyle: { color: (D.segments.find(s => s.name === name) || {}).color || '#9AA4B2', opacity: .72 },
  emphasis: { focus: 'series', itemStyle: { borderColor: '#fff', borderWidth: 1 } },
}));
make('c_scatter', {
  grid: { ...baseGrid, right: 24 },
  tooltip: { trigger: 'item', backgroundColor: 'rgba(22,27,38,.92)', borderWidth: 0,
    padding: [9, 12], textStyle: { color: '#fff', fontSize: 12, fontFamily: FONT },
    formatter: p => `<b>${p.seriesName}</b><br/>使用记录 ${p.data[0]}<br/>MRR ¥${num(p.data[1])}` },
  legend: { top: 0, left: 0, itemWidth: 9, itemHeight: 9, icon: 'circle', itemGap: 12,
            textStyle: { color: INK2, fontSize: 11, fontFamily: FONT } },
  xAxis: { type: 'value', name: '使用记录数', nameLocation: 'middle', nameGap: 26,
           nameTextStyle: { color: INK3, fontSize: 11 }, ...axisCommon },
  yAxis: { type: 'value', name: 'MRR', nameTextStyle: { color: INK3, fontSize: 11 },
           ...axisCommon, axisLabel: { ...axisCommon.axisLabel, formatter: v => money(v) } },
  series: scatterSeries,
});
document.getElementById('t_scatter').textContent =
  '高价值客户集中在右上（用得深、付得多），但左上有一批"付费高、用得浅"的账户——这类客户续费风险最高，值得单独盯。';

/* 分层表 */
document.getElementById('segTable').innerHTML = D.segments.map(s => {
  const rate = s.churn_rate >= 70 ? 'bad' : s.churn_rate >= 60 ? 'mid' : 'good';
  return `<tr>
    <td><span class="dot" style="background:${s.color}"></span>${s.name}</td>
    <td>${s.accounts}</td>
    <td>${s.account_share}%</td>
    <td>${s.mrr ? '¥' + num(s.mrr) : '—'}</td>
    <td>${s.revenue_share}%</td>
    <td><span class="tag ${rate}">${s.churn_rate}%</span></td>
    <td>${s.avg_csat.toFixed(2)}</td>
    <td>${s.avg_error}%</td>
  </tr>`;
}).join('');

/* ---------- 03 AI 运营 ---------- */
make('c_ai', {
  grid: { ...baseGrid, right: 56 }, tooltip: tip,
  legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
            textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
  xAxis: { type: 'category', data: D.ai.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 2 } },
  yAxis: [
    { type: 'value', name: '请求数', nameTextStyle: { color: INK3, fontSize: 11 }, ...axisCommon },
    { type: 'value', name: '成本 USD', nameTextStyle: { color: INK3, fontSize: 11 },
      ...axisCommon, splitLine: { show: false }, axisLine: { show: false },
      axisLabel: { ...axisCommon.axisLabel, formatter: v => '$' + v } },
  ],
  series: [
    { ...barSeries('请求数', D.ai.requests, '#C7D4F5', { barMaxWidth: 18, radius: [3, 3, 0, 0] }) },
    lineSeries('成本', D.ai.cost, '#E8543F', { yAxisIndex: 1 }),
  ],
});

const aiLast = D.ai.requests.length - 1;
document.getElementById('t_ai').textContent =
  `请求量从 ${D.ai.requests[0]} 涨到 ${D.ai.requests[aiLast]}（${(D.ai.requests[aiLast] / Math.max(D.ai.requests[0], 1)).toFixed(0)}×），成本同步涨到 $${D.ai.cost[aiLast].toFixed(2)}。成本曲线比请求量更陡，说明单次请求在变"重"。`;

make('c_health', {
  grid: { ...baseGrid, right: 56 }, tooltip: tip,
  legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
            textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
  xAxis: { type: 'category', data: D.ai.months, ...axisCommon, splitLine: { show: false },
           axisLabel: { ...axisCommon.axisLabel, interval: 2 } },
  yAxis: [
    { type: 'value', name: '成功率 %', min: 80, max: 100,
      nameTextStyle: { color: INK3, fontSize: 11 }, ...axisCommon },
    { type: 'value', name: 'P95 ms', nameTextStyle: { color: INK3, fontSize: 11 },
      ...axisCommon, splitLine: { show: false }, axisLine: { show: false } },
  ],
  series: [
    lineSeries('成功率', D.ai.success, '#1F9D6B'),
    lineSeries('P95 延迟', D.ai.p95, '#D98E04', { yAxisIndex: 1 }),
  ],
});
document.getElementById('t_health').textContent =
  '成功率在 88%–95% 之间波动，没有随流量上升而恶化；P95 延迟稳定在 3.5–4 秒，说明限流与重试策略是有效的。';

make('c_feat', {
  grid: { ...baseGrid, right: 60 },
  tooltip: { ...tip, formatter: p => {
    const f = D.features[p[0].dataIndex];
    return `<b>${f.name}</b><br/>请求数 ${num(f.requests)}（占 ${f.request_share}%）<br/>成本 $${f.cost}（占 ${f.cost_share}%）<br/>成功率 ${f.success}%`;
  } },
  xAxis: { type: 'value', ...axisCommon, axisLabel: { ...axisCommon.axisLabel, formatter: v => money(v) } },
  yAxis: { type: 'category', data: D.features.map(f => f.name).reverse(),
           ...axisCommon, splitLine: { show: false } },
  series: [{
    type: 'bar', data: D.features.map(f => f.requests).reverse(),
    barMaxWidth: 22, itemStyle: { color: '#2F6BFF', borderRadius: [0, 5, 5, 0] },
    label: { show: true, position: 'right', color: INK2, fontSize: 11.5, fontFamily: FONT,
             formatter: p => num(p.value) },
  }],
});
const topFeat = D.features[0];
document.getElementById('t_feat').textContent =
  `${topFeat.name}贡献了 ${topFeat.request_share}% 的请求量，是绝对主力；三个功能的使用量差了一个数量级，说明功能热度极不均衡。`;

make('c_featcost', {
  grid: { ...baseGrid, right: 60 },
  tooltip: { ...tip, valueFormatter: v => '$' + v.toFixed(2) },
  xAxis: { type: 'value', ...axisCommon },
  yAxis: { type: 'category', data: D.features.map(f => f.name).reverse(),
           ...axisCommon, splitLine: { show: false } },
  series: [{
    type: 'bar', data: D.features.map(f => f.cost_per_1k).reverse(),
    barMaxWidth: 22,
    itemStyle: { color: p => p.dataIndex === 0 ? '#E8543F' : '#F0B4A9',
                 borderRadius: [0, 5, 5, 0] },
    label: { show: true, position: 'right', color: INK2, fontSize: 11.5, fontFamily: FONT,
             formatter: p => '$' + p.value.toFixed(2) },
  }],
});
const costly = D.features.slice().sort((a, b) => b.cost_per_1k - a.cost_per_1k)[0];
document.getElementById('t_featcost').textContent =
  `${costly.name}的千次请求成本是 ${costly.cost_per_1k.toFixed(2)} 美元，约为最低者的 3.7 倍——成本杠杆在输入 token 长度，不在调用次数。`;

/* ---------- 04 实验 ---------- */
(function () {
  const metrics = [...new Set(D.ab.map(r => r.metric))];
  const arms = ['对照组', '实验组'];
  const series = arms.map((arm, i) => ({
    name: arm, type: 'bar', barMaxWidth: 22, barGap: '20%',
    itemStyle: { color: i === 0 ? '#B7C6E8' : '#2F6BFF', borderRadius: [4, 4, 0, 0] },
    data: metrics.map(m => {
      const row = D.ab.find(r => r.metric === m && r.arm === arm);
      return row ? row.rate : 0;
    }),
    label: i === 0
      ? { show: true, position: 'insideTop', color: '#3D4A66', fontSize: 10.5, fontFamily: FONT,
          formatter: p => p.value.toFixed(2) + '%' }
      : { show: true, position: 'top', color: INK2, fontSize: 11, fontFamily: FONT,
          formatter: p => p.value.toFixed(2) + '%' },
  }));

  // 实验组的 95% 置信区间，用 custom 系列画误差线
  const errData = metrics.map((m, idx) => {
    const row = D.ab.find(r => r.metric === m && r.arm === '实验组');
    if (!row || row.ci_low === null) return null;
    return [idx, row.ci_low, row.ci_high];
  });

  make('c_ab', {
    grid: { ...baseGrid, top: 44 },
    tooltip: { ...tip, valueFormatter: v => v.toFixed(2) + '%' },
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, icon: 'circle',
              textStyle: { color: INK2, fontSize: 11.5, fontFamily: FONT } },
    xAxis: { type: 'category', data: metrics, ...axisCommon, splitLine: { show: false },
             axisLabel: { ...axisCommon.axisLabel, interval: 0 } },
    yAxis: { type: 'value', ...axisCommon, axisLabel: { ...axisCommon.axisLabel, formatter: '{value}%' } },
    series: [
      ...series,
      {
        name: '95% CI', type: 'custom', z: 5, tooltip: { show: false }, legendHoverLink: false,
        renderItem: (params, api) => {
          const idx = api.value(0), lo = api.value(1), hi = api.value(2);
          const x = api.coord([idx, 0])[0];
          const yLo = api.coord([0, lo])[1], yHi = api.coord([0, hi])[1];
          const w = 7;
          return {
            type: 'group', children: [
              { type: 'line', shape: { x1: x, y1: yHi, x2: x, y2: yLo },
                style: { stroke: '#161B26', lineWidth: 1.2 } },
              { type: 'line', shape: { x1: x - w / 2, y1: yHi, x2: x + w / 2, y2: yHi },
                style: { stroke: '#161B26', lineWidth: 1.2 } },
              { type: 'line', shape: { x1: x - w / 2, y1: yLo, x2: x + w / 2, y2: yLo },
                style: { stroke: '#161B26', lineWidth: 1.2 } },
            ],
          };
        },
        data: errData.filter(Boolean),
      },
    ],
  });
})();
document.getElementById('t_ab').textContent =
  '主指标 25.17% → 26.86%（+1.69pp），但 95% CI 跨 0、p=0.203 不显著；同时错误率与工单率两项护栏指标显著恶化。结论：证据不足，不上线。';

document.getElementById('abTable').innerHTML = (function () {
  const metrics = [...new Set(D.ab.map(r => r.metric))];
  return metrics.map(m => {
    const c = D.ab.find(r => r.metric === m && r.arm === '对照组');
    const t = D.ab.find(r => r.metric === m && r.arm === '实验组');
    const diff = t.rate - c.rate;
    const better = m.includes('激活') ? diff > 0 : diff < 0;
    const sig = t.p !== null && t.p < 0.05;
    const verdict = !sig ? '<span class="tag mid">不显著</span>'
      : (better ? '<span class="tag good">改善</span>' : '<span class="tag bad">恶化</span>');
    return `<tr>
      <td>${m}</td>
      <td>${c.rate.toFixed(2)}%</td>
      <td>${t.rate.toFixed(2)}%</td>
      <td style="color:${better ? 'var(--ok)' : 'var(--danger)'}">${diff > 0 ? '+' : ''}${diff.toFixed(2)}pp</td>
      <td>${t.p === null ? '—' : t.p.toFixed(4)}</td>
      <td>${verdict}</td>
    </tr>`;
  }).join('');
})();

(function () {
  const variants = [...new Set(D.rag.map(r => r.variant))];
  const metric = 'Hit@3';
  const data = variants.map(v => {
    const row = D.rag.find(r => r.variant === v && r.metric === metric);
    return row ? row.score : 0;
  });
  make('c_rag', {
    grid: { ...baseGrid, bottom: 56 },
    tooltip: { ...tip, valueFormatter: v => v.toFixed(1) + '%' },
    xAxis: { type: 'category', data: variants, ...axisCommon, splitLine: { show: false },
             axisLabel: { ...axisCommon.axisLabel, interval: 0, fontSize: 10.5, width: 120, overflow: 'break' } },
    yAxis: { type: 'value', min: 90, max: 100, ...axisCommon,
             axisLabel: { ...axisCommon.axisLabel, formatter: '{value}%' } },
    series: [{
      type: 'bar', data, barMaxWidth: 46,
      itemStyle: { color: p => p.value >= 99.9 ? '#2F6BFF' : '#B7C6E8', borderRadius: [5, 5, 0, 0] },
      label: { show: true, position: 'top', color: INK2, fontSize: 11.5,
               fontFamily: FONT, formatter: p => p.value.toFixed(1) + '%' },
    }],
  });
})();
document.getElementById('t_rag').textContent =
  '三组配置的 Hit@3 都是满分（100%），说明检索召回不是瓶颈；差异体现在 Top1 准确率与 MRR 上——加检索指令让命中更靠前，代价是需要多一次模型调用。';
</script>
</body>
</html>
"""


def main() -> None:
    payload = build_payload()

    echarts_js = (config.ASSET_DIR / "echarts.min.js").read_text(encoding="utf-8")
    html = TEMPLATE.replace("/*__ECHARTS__*/", echarts_js)
    html = html.replace(
        "/*__PAYLOAD__*/",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )

    out = config.OUTPUT_DIR / "dashboard.html"
    out.write_text(html, encoding="utf-8")

    print(f"已生成 {out}")
    print(f"  月份 {payload['meta']['months']} / 账户 {payload['meta']['accounts']} / 分层 {payload['meta']['segments']}")
    print(f"  文件大小 {out.stat().st_size / 1024 / 1024:.2f} MB（含内联 ECharts，可离线打开）")


if __name__ == "__main__":
    main()
