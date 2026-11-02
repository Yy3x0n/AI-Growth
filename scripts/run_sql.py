#!/usr/bin/env python3
"""执行 sql/ 下的脚本。

用法：
    python scripts/run_sql.py sql/02_kpi_analysis.sql
    python scripts/run_sql.py sql/02_kpi_analysis.sql --quiet

为什么单独做一个工具：
建表脚本和视图脚本都需要重复执行（改口径时尤其频繁），
写在各个业务脚本里会导致"改一处、漏一处"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_to_mysql import split_statements  # noqa: E402  复用分句逻辑
from src import config  # noqa: E402


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
        local_infile=True,
        autocommit=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 SQL 文件")
    parser.add_argument("files", nargs="+", help="SQL 文件路径")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = parser.parse_args()

    conn = connect()
    total = 0
    try:
        with conn.cursor() as cursor:
            for file_arg in args.files:
                path = Path(file_arg)
                if not path.is_absolute():
                    path = PROJECT_ROOT / path
                if not path.is_file():
                    print(f"未找到 {path}", file=sys.stderr)
                    return 1

                statements = split_statements(path.read_text(encoding="utf-8"))
                for index, statement in enumerate(statements, start=1):
                    try:
                        cursor.execute(statement)
                    except Exception as exc:
                        preview = " ".join(statement.split())[:160]
                        print(f"\n第 {index} 条语句失败：{preview}", file=sys.stderr)
                        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
                        return 1
                total += len(statements)
                if not args.quiet:
                    print(f"✓ {path.relative_to(PROJECT_ROOT)}  执行 {len(statements)} 条语句")
        print(f"完成，共执行 {total} 条语句")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
