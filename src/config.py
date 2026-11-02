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
