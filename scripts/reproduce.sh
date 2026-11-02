#!/usr/bin/env bash
# =============================================================================
# reproduce.sh —— 一键复现全链路
#
# 设计原则
#   1. 幂等：每一步都可以重复执行，已有产物会跳过或覆盖，不会产生重复数据。
#   2. 快速失败：任一步退出码非 0 立即停止，并打印失败步骤，不掩盖错误。
#   3. 可跳过：用 STEP 环境变量只跑某几步，便于排障。
#   4. 不联网也能跑：没有 GLM_API_KEY 时诊断报告自动降级为 --mock。
#
# 用法
#   bash scripts/reproduce.sh                     # 全链路
#   SKIP_HARNESS=0 bash scripts/reproduce.sh      # 额外跑 A/B 框架自校验（较慢）
#   HARNESS_REPS=2000 bash scripts/reproduce.sh   # 指定自校验次数
#   bash scripts/reproduce.sh 3 5 7               # 只跑第 3、5、7 步
# =============================================================================

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "✗ 未找到 $PY"
  echo "  请先创建虚拟环境：uv venv --python 3.11.16 .venv && uv pip install -r requirements.txt"
  exit 1
fi

# 只跑指定步骤
ONLY=("$@")
should_run() {
  [ ${#ONLY[@]} -eq 0 ] && return 0
  local n="$1"
  for x in "${ONLY[@]}"; do [ "$x" = "$n" ] && return 0; done
  return 1
}

STEP_NO=0
FAILED_STEP=""
declare -a DONE=()

run_step() {
  local title="$1"; shift
  STEP_NO=$((STEP_NO + 1))
  should_run "$STEP_NO" || return 0

  echo ""
  echo "───────────────────────────────────────────────────────────────"
  echo "[$STEP_NO] $title"
  echo "───────────────────────────────────────────────────────────────"

  local start=$SECONDS
  if "$@"; then
    echo "  ✓ 完成（$((SECONDS - start))s）"
    DONE+=("$STEP_NO $title")
  else
    local code=$?
    FAILED_STEP="$STEP_NO $title（退出码 $code）"
    echo "  ✗ 失败：$FAILED_STEP" >&2
    exit "$code"
  fi
}

# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------

step_env() {
  "$PY" scripts/check_environment.py
}

step_download() {
  # 已下载则跳过，避免每次复跑都重新拉取
  local need=0
  for f in accounts subscriptions feature_usage support_tickets churn_events; do
    [ -f "data/raw/ravenstack_${f}.csv" ] || need=1
  done
  if [ "$need" -eq 0 ]; then
    echo "  五张原始 CSV 已存在，跳过下载"
    return 0
  fi
  "$PY" scripts/download_data.py
}

step_profile() {
  "$PY" scripts/profile_data.py
}

step_load() {
  "$PY" scripts/load_to_mysql.py
}

step_kpi_views() {
  "$PY" scripts/run_sql.py sql/02_kpi_analysis.sql
}

step_metrics() {
  "$PY" scripts/run_analysis.py
}

step_simulate() {
  # --force 让随机种子重算，保证与文档记录的数字一致
  "$PY" scripts/simulate_data.py --force
}

step_load_sim() {
  "$PY" scripts/load_simulated_to_mysql.py
}

step_ai() {
  "$PY" scripts/run_ai_analysis.py
}

step_ab() {
  "$PY" scripts/run_ab_test.py
}

step_harness() {
  if [ "${SKIP_HARNESS:-1}" = "1" ]; then
    echo "  默认跳过（Monte Carlo 较慢）。需要校验时：SKIP_HARNESS=0 bash scripts/reproduce.sh"
    return 0
  fi
  "$PY" scripts/validate_ab_harness.py --reps "${HARNESS_REPS:-500}"
}

step_segments() {
  "$PY" scripts/run_sql.py sql/03_customer_segments.sql
  "$PY" scripts/segment_accounts.py
}

step_kb() {
  "$PY" scripts/build_knowledge_base.py
}

step_retrieval() {
  "$PY" scripts/evaluate_retrieval.py
}

step_reports() {
  # 无 key 时用 Mock，保证离线也能验证链路；有 key 则真实调用
  local flag=""
  if ! grep -qE '^GLM_API_KEY=.{20,}' .env 2>/dev/null; then
    echo "  未检测到 GLM_API_KEY，使用 --mock 离线模式"
    flag="--mock"
  fi
  "$PY" scripts/ask_ops_assistant.py --scenario activation_diagnosis \
      --start-date 2024-07-01 --end-date 2024-09-30 $flag
  "$PY" scripts/ask_ops_assistant.py --scenario feature_adoption_diagnosis \
      --start-date 2024-10-01 --end-date 2024-12-31 $flag
  "$PY" scripts/ask_ops_assistant.py --scenario low_activity_customer \
      --as-of 2024-12-31 $flag
}

step_dashboard() {
  "$PY" scripts/build_dashboard.py        # 6 张 PNG 图 + 静态图版看板
  "$PY" scripts/build_bi_dataset.py       # BI 数据层（维度表 + 事实表 CSV）
  "$PY" scripts/build_html_dashboard.py   # 交互版看板（ECharts 单文件）
}

step_tests() {
  "$PY" -m pytest tests/ -q
}

step_secrets() {
  "$PY" scripts/check_no_secrets.py --all
}

# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

echo "==============================================================="
echo " 全链路复现　项目根目录：$ROOT"
echo " Python：$("$PY" -V 2>&1)"
echo "==============================================================="

run_step "环境自检"                 step_env
run_step "下载原始数据"             step_download
run_step "数据画像与质量检查"       step_profile
run_step "入仓（建表 + 装载）"      step_load
run_step "创建 KPI 视图"            step_kpi_views
run_step "指标计算 + SQL/Pandas 对账" step_metrics
run_step "生成模拟数据（T2/T3）"    step_simulate
run_step "装载模拟日志"             step_load_sim
run_step "AI 使用分析 + 对账"       step_ai
run_step "A/B 实验分析"             step_ab
run_step "A/B 框架自校验"           step_harness
run_step "客户分层 + 对账"          step_segments
run_step "构建知识库索引"           step_kb
run_step "检索评估"                 step_retrieval
run_step "生成诊断报告"             step_reports
run_step "生成图表与看板"           step_dashboard
run_step "单元测试"                 step_tests
run_step "密钥扫描"                 step_secrets

echo ""
echo "==============================================================="
echo " 全部完成（共 ${#DONE[@]} 步）"
echo "==============================================================="
for item in "${DONE[@]}"; do echo "  ✓ $item"; done
echo ""
echo "产物位置："
echo "  指标表    outputs/tables/"
echo "  图表      outputs/figures/"
echo "  看板      outputs/dashboard.html"
echo "  诊断报告  outputs/reports/"
