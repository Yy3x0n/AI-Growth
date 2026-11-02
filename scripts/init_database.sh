#!/bin/bash
# 初始化项目数据库与专用用户（幂等，可重复执行）
#
# 用法：
#     cd ai-growth-data-ops-rag
#     bash scripts/init_database.sh
#
# 前提：
#   本机 MySQL 已安装并在 3306 运行。本机实际为 Oracle 官方 tarball 8.4.7，
#   位于 ~/mysql/mysql-8.4.7-macos15-arm64，由 LaunchAgent 开机自启。
#   若改用其它安装方式，只需保证 mysql 客户端在 PATH 中即可。
#
# root 凭据来源（按优先级）：
#   1. 环境变量 MYSQL_ROOT_PASSWORD
#   2. 文件 ~/mysql/ROOT_PASSWORD.txt
#   3. 交互式输入
#
# 不做的事：不删除任何库、不改 root 密码、不动其他 schema。

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m  ×\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 定位客户端
# ---------------------------------------------------------------------------
MYSQL="$(command -v mysql || true)"
if [ -z "$MYSQL" ]; then
  for candidate in "$HOME"/mysql/mysql-*/bin/mysql /opt/homebrew/opt/mysql/bin/mysql; do
    [ -x "$candidate" ] && MYSQL="$candidate" && break
  done
fi
[ -n "$MYSQL" ] || die "未找到 mysql 客户端。请把安装目录加入 PATH 后重试。"
MYSQL_BIN_DIR="$(dirname "$MYSQL")"
ok "客户端: $MYSQL"

# ---------------------------------------------------------------------------
# 读取项目配置
# ---------------------------------------------------------------------------
[ -f "$ENV_FILE" ] || die "未找到 .env，请先执行 cp .env.example .env"

get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

DB="$(get_env MYSQL_DATABASE)"; DB="${DB:-ai_growth_ops}"
USER_NAME="$(get_env MYSQL_USER)"; USER_NAME="${USER_NAME:-ai_ops_user}"
PW="$(get_env MYSQL_PASSWORD)"
HOST="$(get_env MYSQL_HOST)"; HOST="${HOST:-127.0.0.1}"
PORT="$(get_env MYSQL_PORT)"; PORT="${PORT:-3306}"

[ -n "$PW" ] && [ "$PW" != "replace_with_your_password" ] \
  || die ".env 中 MYSQL_PASSWORD 仍为占位值，请先生成：openssl rand -hex 16"

# ---------------------------------------------------------------------------
# 取得 root 凭据
# ---------------------------------------------------------------------------
ROOT_PW="${MYSQL_ROOT_PASSWORD:-}"
if [ -z "$ROOT_PW" ] && [ -f "$HOME/mysql/ROOT_PASSWORD.txt" ]; then
  ROOT_PW="$(cat "$HOME/mysql/ROOT_PASSWORD.txt")"
fi
if [ -z "$ROOT_PW" ]; then
  read -r -s -p "请输入 MySQL root 密码: " ROOT_PW; echo
fi

MYSQL_ROOT() { MYSQL_PWD="$ROOT_PW" "$MYSQL" --protocol=TCP -h "$HOST" -P "$PORT" -u root "$@"; }

MYSQL_ROOT -e "SELECT 1" >/dev/null 2>&1 || die "root 认证失败，请确认密码正确"

# ---------------------------------------------------------------------------
# 建库建用户
# ---------------------------------------------------------------------------
info "初始化 ${DB} 与 ${USER_NAME}"
umask 077
SQL_FILE="$(mktemp)"
cat > "$SQL_FILE" <<EOSQL
CREATE DATABASE IF NOT EXISTS \`${DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '${USER_NAME}'@'127.0.0.1' IDENTIFIED BY '${PW}';
CREATE USER IF NOT EXISTS '${USER_NAME}'@'localhost' IDENTIFIED BY '${PW}';
ALTER USER '${USER_NAME}'@'127.0.0.1' IDENTIFIED BY '${PW}';
ALTER USER '${USER_NAME}'@'localhost' IDENTIFIED BY '${PW}';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER_NAME}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER_NAME}'@'localhost';
FLUSH PRIVILEGES;
EOSQL
MYSQL_ROOT < "$SQL_FILE" || { rm -f "$SQL_FILE"; die "建库失败"; }
rm -f "$SQL_FILE"
ok "库与用户已就绪"

# ---------------------------------------------------------------------------
# 用应用用户验证
# ---------------------------------------------------------------------------
info "以 ${USER_NAME} 验证连接"
MYSQL_PWD="$PW" "$MYSQL" --protocol=TCP -h "$HOST" -P "$PORT" -u "$USER_NAME" -D "$DB" \
  -e "SELECT VERSION() AS version, DATABASE() AS db, CURRENT_USER() AS user;" --table \
  || die "应用用户连接失败，请检查 .env 中的 MYSQL_PASSWORD"

ok "完成"
