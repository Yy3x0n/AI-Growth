"""项目全局配置：单一来源。

设计原则：
1. 所有路径、随机种子、检索参数、计价假设集中在此，禁止在 Notebook 或脚本里硬编码。
2. 密钥与连接串只从 .env 读取，本文件不存放任何凭据。
3. 任何"假设值"（如 Token 单价）必须带快照日期，便于在报告中标注来源。

对应设计文档：docs/00_requirements_and_design.md
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

# 项目根目录 = 本文件的上上级（src/config.py -> src -> 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
SIMULATED_DIR = DATA_DIR / "simulated"
CHROMA_DIR = DATA_DIR / "chroma"

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge_base"
DOCS_DIR = PROJECT_ROOT / "docs"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
TABLE_DIR = OUTPUT_DIR / "tables"
FIGURE_DIR = OUTPUT_DIR / "figures"
REPORT_DIR = OUTPUT_DIR / "reports"

SQL_DIR = PROJECT_ROOT / "sql"

# 原始数据文件名（阶段 1 以下载后的实际文件名为准，若有出入在此处改一处即可）
RAW_FILES = {
    "accounts": "ravenstack_accounts.csv",
    "subscriptions": "ravenstack_subscriptions.csv",
    "feature_usage": "ravenstack_feature_usage.csv",
    "support_tickets": "ravenstack_support_tickets.csv",
    "churn_events": "ravenstack_churn_events.csv",
}

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv

    # 只加载项目根目录下的 .env；文件不存在时 load_dotenv 静默跳过
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # 依赖尚未安装时不应导致导入失败
    pass


def env(key: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白，避免 .env 里手写空格导致连接失败。"""
    return os.getenv(key, default).strip()


# ---- LLM ----
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "deepseek-flash")

# ---- 数据库 ----
MYSQL_HOST = env("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(env("MYSQL_PORT", "3306"))
MYSQL_DATABASE = env("MYSQL_DATABASE", "ai_growth_ops")
MYSQL_USER = env("MYSQL_USER", "ai_ops_user")
MYSQL_PASSWORD = env("MYSQL_PASSWORD")


def mysql_url(driver: str = "pymysql") -> str:
    """拼装 SQLAlchemy 连接串。密码含特殊字符时由调用方负责 URL 编码。"""
    return (
        f"mysql+{driver}://{MYSQL_USER}:{MYSQL_PASSWORD}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
    )


# ---------------------------------------------------------------------------
# 随机性（模拟数据与实验分组共用同一个种子，保证可复现）
# ---------------------------------------------------------------------------

RANDOM_SEED = int(env("RANDOM_SEED", "20261102"))

# ---------------------------------------------------------------------------
# Embedding 与检索
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = env("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DEVICE = env("EMBEDDING_DEVICE", "auto")

# 模型下载镜像。huggingface.co 直连在本机返回 502（代理层拒绝），必须走镜像。
HF_ENDPOINT = env("HF_ENDPOINT", "https://hf-mirror.com")
# huggingface_hub 新版默认走 Xet 协议，镜像不代理该域名，会报 401。
HF_HUB_DISABLE_XET = env("HF_HUB_DISABLE_XET", "1")

# bge-small-zh-v1.5 的硬约束：输出 512 维，最大输入 512 token。
# 中文约 1 字 ≈ 1 token，叠加标点与英文术语后 token 数通常大于字符数，
# 因此 chunk 上限取 420 汉字留出约 20% 余量，避免静默截断。
EMBEDDING_DIM = 512
EMBEDDING_MAX_TOKENS = 512
CHUNK_TARGET_CHARS = 350
CHUNK_MAX_CHARS = 420
CHUNK_MIN_CHARS = 150
CHUNK_OVERLAP_CHARS = 70

RAG_TOP_K = int(env("RAG_TOP_K", "4"))

# ---------------------------------------------------------------------------
# 成本估算假设（不是真实账单）
# ---------------------------------------------------------------------------

# Token 单价快照：DeepSeek-V4.1-Flash 官方公开价，核验日期 2026-09-18。
# 单位：美元 / 百万 token。off-peak 价；peak 时段为该值 2 倍，本项目按 off-peak 估算。
# 报告中必须标注「假设单价」而非「真实成本」。
PRICE_SNAPSHOT_DATE = "2026-09-18"
PRICE_USD_PER_MILLION_TOKENS = {
    "input_cache_miss": 0.15,
    "input_cache_hit": 0.003,
    "output": 0.60,
}


def estimate_cost_usd(input_tokens: int, output_tokens: int, cache_hit_rate: float = 0.0) -> float:
    """按假设单价估算成本（仅用于相对比较）。

    cache_hit_rate 取 0 表示全部按未命中缓存计价，是偏保守的上界估算。
    """
    miss = input_tokens * (1 - cache_hit_rate)
    hit = input_tokens * cache_hit_rate
    return (
        miss * PRICE_USD_PER_MILLION_TOKENS["input_cache_miss"]
        + hit * PRICE_USD_PER_MILLION_TOKENS["input_cache_hit"]
        + output_tokens * PRICE_USD_PER_MILLION_TOKENS["output"]
    ) / 1_000_000


# ---------------------------------------------------------------------------
# 指标口径常量
# ---------------------------------------------------------------------------

# 全项目统计主体统一为 account（企业账户），数据中不存在 user 级表。
METRIC_SUBJECT = "account"

# 激活观察窗口（天）
ACTIVATION_WINDOW_DAYS = 7
RETENTION_WINDOWS = (7, 30)

# ---------------------------------------------------------------------------
# 模拟数据（T2 / T3）的冻结参数
# ---------------------------------------------------------------------------
# 设计文档 §6.3 要求：种子、p1、p2、n 全部集中在此，模拟与检验脚本只读不写。
# 修改本区块等同于更换实验设计，必须在 docs 里留痕，禁止为了"跑出显著"而调参。

# 数据观察期（由阶段 1 实测确定：T1 五表的日期跨度）
OBSERVATION_START = "2023-01-01"
OBSERVATION_END = "2024-12-31"

# T3 实验：新手引导 checklist 对「7 日内完成首次有效 AI 任务」的影响
EXPERIMENT = {
    "name": "onboarding_checklist_vs_control",
    # 主指标基线。注意：这是**假设值**而非实测值——T1 数据无生命周期（C-10），
    # 无法测出真实的新客 7 日激活率。假设依据见 docs/data_simulation_method.md。
    "baseline_rate": 0.25,
    # 目标最小可检测效应（相对），由"运营上值得做的下限"倒推，不是由样本量倒推
    "target_relative_lift": 0.15,
    "alpha": 0.05,
    "power": 0.80,
    # 护栏指标（任一显著恶化即不可上线）
    "guardrails": {
        "post_7d_error_rate": {"control": 0.105, "treatment": 0.108, "direction": "lower_is_better"},
        "support_ticket_7d": {"control": 0.110, "treatment": 0.140, "direction": "lower_is_better"},
    },
}

# 由 baseline_rate 与 target_relative_lift 派生的设计参数（不手工填，避免不一致）
EXPERIMENT["treatment_rate"] = round(
    EXPERIMENT["baseline_rate"] * (1 + EXPERIMENT["target_relative_lift"]), 6
)

# T2 模拟 AI 请求日志的功能参数
# 说明：mix 为请求占比，success_rate 为基线成功率，latency 为中位延迟（毫秒），
# tokens 为中位 token 数。取值参照公开的 LLM 应用经验值，非真实服务测量值。
AI_FEATURES = {
    "AI 问答": {
        "mix": 0.50, "success_rate": 0.92,
        "latency_median_ms": 1150, "latency_sigma": 0.55,
        "input_tokens_median": 620, "output_tokens_median": 180,
    },
    "文档总结": {
        "mix": 0.30, "success_rate": 0.88,
        "latency_median_ms": 2400, "latency_sigma": 0.60,
        "input_tokens_median": 3800, "output_tokens_median": 260,
    },
    "AI 写作": {
        "mix": 0.20, "success_rate": 0.90,
        "latency_median_ms": 1750, "latency_sigma": 0.50,
        "input_tokens_median": 900, "output_tokens_median": 620,
    },
}

# 账户使用强度分层：决定月请求量的量级
AI_USAGE_TIERS = {
    "light": {"prob": 0.55, "monthly_requests": 3},
    "medium": {"prob": 0.33, "monthly_requests": 14},
    "heavy": {"prob": 0.12, "monthly_requests": 55},
}

# 月度自然增长系数（模拟产品整体用量增长，非 T1 数据实测）
AI_MONTHLY_GROWTH = 1.025

# AI 采用率：并非所有账户都会使用 AI 功能
AI_ADOPTION_RATE = 0.88

# 「有效任务」判定：请求成功，且输出 token 不低于该值。
# 取 50 是为了排除"空返回 / 一句话敷衍"这类技术成功但业务无价值的请求。
# 该值同时写入 metric_config 表供 SQL 视图读取，两处必须一致。
EFFECTIVE_TASK_MIN_OUTPUT_TOKENS = 50

# 模拟故障窗口：用于检验分析能否自动发现异常（而非只算平稳指标）
SIMULATED_INCIDENT = {
    "feature": "AI 问答",
    "start": "2024-06-03",
    "end": "2024-06-12",
    "degraded_success_rate": 0.68,
}
