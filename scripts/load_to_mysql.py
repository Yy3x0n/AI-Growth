#!/usr/bin/env python3
"""阶段 2：把五张原始 CSV 装载进 MySQL。

用法：
    source .venv/bin/activate
    python scripts/load_to_mysql.py              # 全量重建
    python scripts/load_to_mysql.py --verify-only  # 只做行数核对

流程（三阶段，顺序不可颠倒）：
    ① sql/01_create_tables.sql        建表（DROP + CREATE）
    ② LOAD DATA LOCAL INFILE → 暂存表  客户端灌入原始字符串
    ③ sql/01b_load_from_staging.sql   转强类型 + 清空暂存

为什么不一键跑：LOAD DATA LOCAL INFILE 必须由客户端发起，SQL 文件跑不了；
而"转换"放在装载之后，才能真正读到数据。

安全约束：
- 只操作 ai_growth_ops 库，不触碰同实例上的其他库。
- 装载前打印各表将写入的行数，装载后逐表核对，不一致即非零退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402

SQL_DIR = PROJECT_ROOT / "sql"
RAW_DIR = config.RAW_DIR

# 暂存表 ← CSV 的对应关系（列顺序必须与 CSV 表头一致）
STAGING_LOAD = [
    ("raw_accounts_stg", "ravenstack_accounts.csv"),
    ("raw_subscriptions_stg", "ravenstack_subscriptions.csv"),
    ("raw_feature_usage_stg", "ravenstack_feature_usage.csv"),
    ("raw_support_tickets_stg", "ravenstack_support_tickets.csv"),
    ("raw_churn_events_stg", "ravenstack_churn_events.csv"),
]

TARGET_TABLES = [
    "raw_accounts",
    "raw_subscriptions",
    "raw_feature_usage",
    "raw_support_tickets",
    "raw_churn_events",
]


def split_statements(sql_text: str) -> list[str]:
    """按分号拆分 SQL 语句，跳过行注释并尊重单引号内的分号。

    不能用 str.split(';')：注释和字符串里都可能出现分号，
    一旦误切，报错信息会指向无关行，排查成本很高。
    """
    statements: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    length = len(sql_text)

    while i < length:
        char = sql_text[i]

        # 行注释：跳到行尾
        if not in_quote and char == "-" and i + 1 < length and sql_text[i + 1] == "-":
            while i < length and sql_text[i] != "\n":
                i += 1
            continue

        if char == "'" :
            # 连续两个单引号是转义，不切换状态
            if in_quote and i + 1 < length and sql_text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_quote = not in_quote
            buf.append(char)
            i += 1
            continue

        if char == ";" and not in_quote:
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(char)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def run_sql_file(cursor, path: Path) -> int:
    """执行一个 SQL 文件，返回执行的语句数。"""
    statements = split_statements(path.read_text(encoding="utf-8"))
    for statement in statements:
        cursor.execute(statement)
    return len(statements)


def load_csv_to_staging(cursor, table: str, csv_path: Path) -> int:
    """用 LOAD DATA LOCAL INFILE 把 CSV 灌进暂存表，返回实际导入行数。"""
    if not csv_path.is_file():
        raise FileNotFoundError(f"缺少数据文件 {csv_path}，请先运行 scripts/download_data.py")

    # 路径里可能有空格与中文，用参数化不行（LOAD DATA 不支持），只能转义单引号
    safe_path = str(csv_path).replace("\\", "\\\\").replace("'", "\\'")
    cursor.execute(
        f"""
        LOAD DATA LOCAL INFILE '{safe_path}'
        INTO TABLE {table}
        FIELDS TERMINATED BY ',' ENCLOSED BY '"'
        LINES TERMINATED BY '\\n'
        IGNORE 1 LINES
        """
    )
    return cursor.rowcount


def count_rows(cursor, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cursor.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="装载原始数据到 MySQL")
    parser.add_argument("--verify-only", action="store_true", help="只核对行数，不重建")
    args = parser.parse_args()

    print(f"目标库: {config.MYSQL_DATABASE} @ {config.MYSQL_HOST}:{config.MYSQL_PORT}")

    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
        local_infile=True,   # LOAD DATA LOCAL 必需
        autocommit=False,
    )

    try:
        with conn.cursor() as cursor:
            if args.verify_only:
                print("\n仅核对模式")
            else:
                print("\n① 建表")
                n = run_sql_file(cursor, SQL_DIR / "01_create_tables.sql")
                print(f"   执行 {n} 条语句")

                print("\n② 装载到暂存表")
                for table, filename in STAGING_LOAD:
                    csv_path = RAW_DIR / filename
                    inserted = load_csv_to_staging(cursor, table, csv_path)
                    print(f"   {table:26s} ← {filename:32s} {inserted:>7,} 行")

                print("\n③ 转强类型")
                n = run_sql_file(cursor, SQL_DIR / "01b_load_from_staging.sql")
                print(f"   执行 {n} 条语句")

            conn.commit()

            print("\n④ 行数核对")
            print(f"   {'表':24s} {'MySQL':>9s} {'CSV':>9s}  结果")
            all_ok = True
            for table, filename in [
                ("raw_accounts", "ravenstack_accounts.csv"),
                ("raw_subscriptions", "ravenstack_subscriptions.csv"),
                ("raw_feature_usage", "ravenstack_feature_usage.csv"),
                ("raw_support_tickets", "ravenstack_support_tickets.csv"),
                ("raw_churn_events", "ravenstack_churn_events.csv"),
            ]:
                db_count = count_rows(cursor, table)
                with (RAW_DIR / filename).open("rb") as handle:
                    csv_count = sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b"")) - 1
                ok = db_count == csv_count
                all_ok = all_ok and ok
                print(f"   {table:24s} {db_count:>9,} {csv_count:>9,}  {'一致' if ok else '不一致 !!'}")

            if not all_ok:
                print("\n行数不一致，装载可能被静默截断，请检查 sql_mode 与字段类型")
                return 1

            print("\n装载完成，行数全部一致。")
            return 0
    except Exception as exc:
        conn.rollback()
        print(f"\n装载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
