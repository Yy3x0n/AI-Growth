#!/usr/bin/env python3
"""阶段 3：把 T2 模拟 AI 日志装入 MySQL，并建立指标视图。

用法：
    python scripts/load_simulated_to_mysql.py
    python scripts/load_simulated_to_mysql.py --verify-only

流程：
    ① sql/04_ai_usage.sql   建表 + 配置表 + 视图（DROP/CREATE，幂等）
    ② TRUNCATE 明细表
    ③ LOAD DATA LOCAL INFILE 灌入
    ④ 空字符串 error_type 转 NULL（LOAD DATA 不会自动做这件事）
    ⑤ 行数 + P95 抽样核对

为什么这段数据可以直接进强类型表、不走暂存表：
    T2 是脚本生成的，没有 T1 那种 "True"/"" 混杂的脏值，字段类型在生成时已经确定。
    T1 必须走暂存表，是因为 CSV 里的布尔和空串直接灌进 TINYINT/DOUBLE 会静默变 0。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_to_mysql import split_statements  # noqa: E402
from scripts.run_sql import connect  # noqa: E402
from src import ai_usage, config  # noqa: E402

SQL_FILE = PROJECT_ROOT / "sql" / "04_ai_usage.sql"


def count_csv_rows(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b"")) - 1


def main() -> int:
    parser = argparse.ArgumentParser(description="装载模拟 AI 日志到 MySQL")
    parser.add_argument("--verify-only", action="store_true", help="只核对，不重建")
    args = parser.parse_args()

    csv_path = config.SIMULATED_DIR / ai_usage.AI_LOG_FILE
    if not csv_path.is_file():
        print(f"缺少 {csv_path}，请先运行 scripts/simulate_data.py", file=sys.stderr)
        return 1

    print(f"目标库: {config.MYSQL_DATABASE} @ {config.MYSQL_HOST}:{config.MYSQL_PORT}")
    conn = connect()
    try:
        with conn.cursor() as cursor:
            if not args.verify_only:
                print("\n① 建表与视图")
                statements = split_statements(SQL_FILE.read_text(encoding="utf-8"))
                for index, statement in enumerate(statements, start=1):
                    try:
                        cursor.execute(statement)
                    except Exception as exc:
                        preview = " ".join(statement.split())[:140]
                        print(f"\n第 {index} 条语句失败：{preview}", file=sys.stderr)
                        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
                        return 1
                print(f"   执行 {len(statements)} 条语句")

                print("\n② 清空明细表")
                cursor.execute("TRUNCATE TABLE raw_ai_request_logs")

                print("\n③ 装载")
                safe_path = str(csv_path).replace("\\", "\\\\").replace("'", "\\'")
                cursor.execute(
                    f"""
                    LOAD DATA LOCAL INFILE '{safe_path}'
                    INTO TABLE raw_ai_request_logs
                    FIELDS TERMINATED BY ',' ENCLOSED BY '"'
                    LINES TERMINATED BY '\\n'
                    IGNORE 1 LINES
                    """
                )
                print(f"   导入 {cursor.rowcount:,} 行")

                print("\n④ 空字符串 error_type 转 NULL")
                cursor.execute(
                    "UPDATE raw_ai_request_logs SET error_type = NULL WHERE error_type = ''"
                )
                print(f"   处理 {cursor.rowcount:,} 行")

            print("\n⑤ 核对")
            cursor.execute("SELECT COUNT(*) FROM raw_ai_request_logs")
            db_count = int(cursor.fetchone()[0])
            csv_count = count_csv_rows(csv_path)
            ok = db_count == csv_count
            print(f"   行数：MySQL {db_count:,} / CSV {csv_count:,}  {'一致' if ok else '不一致 !!'}")

            # 抽样核对 P95：SQL 侧最近秩法与 Pandas 侧必须给出同一个整数
            cursor.execute(
                "SELECT month_start, p95_latency_ms FROM v_ai_monthly ORDER BY month_start LIMIT 3"
            )
            sql_p95 = cursor.fetchall()

            df = ai_usage.load_logs(csv_path)
            monthly = ai_usage.monthly_summary(df)
            p95_ok = True
            for month_start, sql_value in sql_p95:
                expected = int(
                    monthly.loc[monthly["month_start"] == pd_timestamp(month_start), "p95_latency_ms"].iloc[0]
                )
                if expected != int(sql_value):
                    p95_ok = False
                    print(f"   P95 不一致 {month_start}: SQL={sql_value} Pandas={expected}")
            print(f"   P95（前 3 个月，最近秩法）：{'一致' if p95_ok else '不一致 !!'}")

            if not (ok and p95_ok):
                return 1

            print("\n装载完成。")
            return 0
    finally:
        conn.close()


def pd_timestamp(value):
    import pandas as pd

    return pd.Timestamp(value)


if __name__ == "__main__":
    raise SystemExit(main())
