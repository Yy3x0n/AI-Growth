"""原始数据加载：统一入口，接住编码、类型与日期解析。

设计原则：
1. 只读：任何函数都不写回 data/raw/。
2. 类型显式：日期列在加载时就转成 datetime，避免后续各脚本各自 pd.to_datetime
   导致口径漂移（例如有的地方 errors='coerce'，有的地方不处理）。
3. 布尔列显式转换：CSV 里是 "True"/"False" 字符串，不转会变成 object 类型，
   后续做 .sum() 会得到字符串拼接而不是计数。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src import config

# 各表的日期列：加载时统一转换为 datetime
DATE_COLUMNS = {
    "accounts": ["signup_date"],
    "subscriptions": ["start_date", "end_date"],
    "feature_usage": ["usage_date"],
    "support_tickets": ["submitted_at", "closed_at"],
    "churn_events": ["churn_date"],
}

# 各表的布尔列
BOOL_COLUMNS = {
    "accounts": ["is_trial", "churn_flag"],
    "subscriptions": ["is_trial", "upgrade_flag", "downgrade_flag", "churn_flag", "auto_renew_flag"],
    "feature_usage": ["is_beta_feature"],
    "support_tickets": ["escalation_flag"],
    "churn_events": ["preceding_upgrade_flag", "preceding_downgrade_flag", "is_reactivation"],
}


def _read_csv(path: Path) -> pd.DataFrame:
    """读取 CSV 并打印基础信息，便于在日志里回溯用了哪个文件。"""
    if not path.is_file():
        raise FileNotFoundError(
            f"未找到 {path}。请先运行：python scripts/download_data.py"
        )
    # encoding='utf-8-sig' 兼容可能存在的 BOM，比 'utf-8' 更稳
    return pd.read_csv(path, encoding="utf-8-sig")


def load_raw(name: str) -> pd.DataFrame:
    """加载单张原始表并做类型规整。

    参数 name 取值：accounts / subscriptions / feature_usage / support_tickets / churn_events
    """
    if name not in config.RAW_FILES:
        raise KeyError(f"未知表名 {name}，可选：{list(config.RAW_FILES)}")

    path = config.RAW_DIR / config.RAW_FILES[name]
    df = _read_csv(path)

    for col in DATE_COLUMNS.get(name, []):
        if col in df.columns:
            # errors='coerce'：解析失败的置 NaT，由质量检查环节统计，不静默丢弃
            df[col] = pd.to_datetime(df[col], errors="coerce")

    for col in BOOL_COLUMNS.get(name, []):
        if col in df.columns and df[col].dtype == object:
            df[col] = df[col].map({"True": True, "False": False, "true": True, "false": False})

    return df


def load_all() -> dict[str, pd.DataFrame]:
    """一次加载五张表，键为表名。"""
    return {name: load_raw(name) for name in config.RAW_FILES}


def load_usage_with_account() -> pd.DataFrame:
    """返回把 feature_usage 补上 account_id 后的明细表。

    这一步是必须的：feature_usage 只有 subscription_id，没有 account_id，
    而全项目统计主体是 account，任何"账户级活跃"都必须先经过这层映射，
    否则会把订阅级记录误当成账户级。
    """
    usage = load_raw("feature_usage")
    subs = load_raw("subscriptions")[["subscription_id", "account_id"]]
    merged = usage.merge(subs, on="subscription_id", how="left")
    unmatched = int(merged["account_id"].isna().sum())
    if unmatched:
        raise ValueError(f"{unmatched} 条 usage 记录无法匹配到 account_id，请先检查外键完整性")
    return merged
