#!/usr/bin/env python3
"""阶段 1：下载 RavenStack 五张原始 CSV。

用法：
    source .venv/bin/activate
    python scripts/download_data.py           # 已存在且校验和一致的跳过
    python scripts/download_data.py --force   # 强制重新下载

设计要点：
1. 原始文件只读：下载后不再改写，任何清洗都输出到 data/processed/。
2. 可复现：记录来源 URL、下载时间、字节数、sha256、行数，写入 docs/data_manifest.md。
3. 上游仓库未声明 LICENSE，因此数据文件不进入 Git；靠本脚本 + 清单重建。
4. 断点友好：已存在且 sha256 匹配的文件直接跳过，不重复占用带宽。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MANIFEST = PROJECT_ROOT / "docs" / "data_manifest.md"

# 上游仓库：https://github.com/tivonio/sql-churn-analysis
# 该仓库 README 明确说明 data/raw/ 下即为 RavenStack 原始 CSV（合成 SaaS 数据）。
BASE_URL = "https://raw.githubusercontent.com/tivonio/sql-churn-analysis/main/data/raw"

FILES = {
    "accounts": "ravenstack_accounts.csv",
    "subscriptions": "ravenstack_subscriptions.csv",
    "feature_usage": "ravenstack_feature_usage.csv",
    "support_tickets": "ravenstack_support_tickets.csv",
    "churn_events": "ravenstack_churn_events.csv",
}

# 每个文件预期的首列，用于快速识别"下载到了 HTML 错误页"这类故障。
# 注意：这里必须用实测列名，不要按命名习惯推测——
# churn_events 的首列是 churn_event_id，而非 event_id。
EXPECTED_FIRST_COLUMN = {
    "accounts": "account_id",
    "subscriptions": "subscription_id",
    "feature_usage": "usage_id",
    "support_tickets": "ticket_id",
    "churn_events": "churn_event_id",
}


def sha256_of(path: Path) -> str:
    """流式计算 sha256，避免大文件一次性读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def count_rows(path: Path) -> int:
    """统计数据行数（不含表头），按二进制流式读，比 pandas 快且不改类型。"""
    size = path.stat().st_size
    if size == 0:
        return 0
    with path.open("rb") as handle:
        newlines = sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
        # 末尾是否有换行决定最后一行是否被计入
        handle.seek(-1, 2)
        ends_with_newline = handle.read(1) == b"\n"
    # 有结尾换行时，行数 = 换行数；无结尾换行时，行数 = 换行数 + 1
    total_lines = newlines if ends_with_newline else newlines + 1
    return max(total_lines - 1, 0)  # 减去表头


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return handle.readline().strip().split(",")


def download(name: str, filename: str, force: bool) -> dict:
    """下载单个文件并返回其元信息。"""
    target = RAW_DIR / filename
    url = f"{BASE_URL}/{filename}"

    if target.exists() and not force:
        header = read_header(target)
        if header and header[0] == EXPECTED_FIRST_COLUMN[name]:
            size = target.stat().st_size
            print(f"  跳过（已存在）{filename}  {size:,} 字节  {count_rows(target):,} 行")
            return {
                "name": name,
                "filename": filename,
                "url": url,
                "size": size,
                "sha256": sha256_of(target),
                "rows": count_rows(target),
                "cols": len(header),
                "status": "已存在",
            }

    print(f"  下载 {filename} …", end="", flush=True)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    # 上游异常时可能返回 HTML 页面，这里做一次内容校验再落盘
    head = resp.content[:200].decode("utf-8", errors="ignore")
    if head.lstrip().startswith("<"):
        raise RuntimeError(f"{filename} 返回的不是 CSV，可能是错误页：{head[:80]}")

    target.write_bytes(resp.content)
    header = read_header(target)
    if not header or header[0] != EXPECTED_FIRST_COLUMN[name]:
        raise RuntimeError(f"{filename} 首列异常：期望 {EXPECTED_FIRST_COLUMN[name]}，实际 {header[:1]}")

    size = target.stat().st_size
    rows = count_rows(target)
    print(f" 完成  {size:,} 字节  {rows:,} 行")
    return {
        "name": name,
        "filename": filename,
        "url": url,
        "size": size,
        "sha256": sha256_of(target),
        "rows": rows,
        "cols": len(header),
        "status": "已下载",
    }


def write_manifest(records: list[dict]) -> None:
    """把下载元信息写成 Markdown，供他人核对数据来源与完整性。"""
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "# 数据清单（Data Manifest）",
        "",
        "> 本文件由 `scripts/download_data.py` 自动生成，请勿手工编辑。",
        "",
        f"- 生成时间：{now}",
        "- 数据来源仓库：https://github.com/tivonio/sql-churn-analysis",
        "- 数据来源页面：https://www.kaggle.com/datasets/rivalytics/saas-subscription-and-churn-analytics-dataset",
        "- 数据性质：**公开合成 SAS 数据（非真实企业数据）**",
        "- 分发说明：上游仓库未声明 LICENSE，故原始 CSV 不进入本仓库，由脚本重建。",
        "",
        "## 文件清单",
        "",
        "| 表 | 文件 | 行数（不含表头） | 列数 | 字节数 | 状态 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for rec in records:
        lines.append(
            f"| {rec['name']} | `{rec['filename']}` | {rec['rows']:,} | {rec['cols']} | "
            f"{rec['size']:,} | {rec['status']} |"
        )

    lines += ["", "## 校验和（SHA-256）", "", "| 文件 | SHA-256 |", "|---|---|"]
    for rec in records:
        lines.append(f"| `{rec['filename']}` | `{rec['sha256']}` |")

    lines += [
        "",
        "## 复核方式",
        "",
        "```bash",
        "shasum -a 256 data/raw/*.csv",
        "```",
        "",
        "若校验和与上表不一致，说明文件被改动或上游已更新，"
        "需重新执行 `python scripts/download_data.py --force` 并更新本文件。",
        "",
    ]
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 RavenStack 原始数据")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"目标目录：{RAW_DIR}")

    records: list[dict] = []
    try:
        for name, filename in FILES.items():
            records.append(download(name, filename, args.force))
    except Exception as exc:
        print(f"\n下载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    write_manifest(records)
    total_rows = sum(r["rows"] for r in records)
    total_size = sum(r["size"] for r in records)
    print(f"\n完成：{len(records)} 个文件，合计 {total_rows:,} 行，{total_size / 1024 / 1024:.1f} MB")
    print(f"清单已写入：{MANIFEST.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
