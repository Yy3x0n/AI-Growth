#!/bin/bash
# 一次性安装并初始化 MySQL 8.4 LTS
#
# 用法（在 VS Code 终端里执行）：
#     cd ai-growth-data-ops-rag
#     bash scripts/setup_mysql.sh
#
# 为什么需要你手动执行：
#   Homebrew 在 macOS 上安装 bottle 时会调用 sandbox-exec 二次封装。
#   WorkBuddy 的执行环境本身已处于系统沙箱内，嵌套 sandbox 会失败；
#   而 Homebrew 明确拒在默认前缀 /opt/homebrew 下跳过沙箱（仅自定义前缀允许）。
#   因此这一步必须在不受沙箱限制的终端里完成。
#
# 脚本行为：
#   1. 安装 mysql@8.4（已装则跳过）
#   2. 启动 brew services
#   3. 等待 mysqld 就绪
#   4. 创建数据库 ai_growth_ops 与专用用户 ai_ops_user（密码取自 .env）
#   5. 打印验证结果
# 不做的事：不修改 root 密码、不删除任何数据库、不动其他 schema。

set -uo pipefail

BREW="/opt/homebrew/bin/brew"
MYSQL_BIN_DIR="/opt/homebrew/opt/mysql@8.4/bin"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 国内镜像：官方 API 索引 35MB，直连约 15KB/s，镜像约 1.4MB/s
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_ENV_HINTS=1
export HOMEBREW_API_DOMAIN="https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api"
export HOMEBREW_BOTTLE_DOMAIN="https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m  ×\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
[ -x "$BREW" ] || die "未找到 $BREW。Homebrew 尚未正确安装。"

[ -f "$PROJECT_DIR/.env" ] || die "未找到 $PROJECT_DIR/.env，请先执行 cp .env.example .env"

# 从 .env 读取密码与库名，不把密码打印到屏幕
MYSQL_PASSWORD="$(grep -E '^MYSQL_PASSWORD=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2-)"
MYSQL_DATABASE="$(grep -E '^MYSQL_DATABASE=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2-)"
MYSQL_USER="$(grep -E '^MYSQL_USER=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2-)"

MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
MYSQL_DATABASE="${MYSQL_DATABASE:-ai_growth_ops}"
MYSQL_USER="${MYSQL_USER:-ai_ops_user}"

if [ -z "$MYSQL_PASSWORD" ] || [ "$MYSQL_PASSWORD" = "replace_with_your_password" ]; then
  die ".env 中的 MYSQL_PASSWORD 仍为占位值。请先生成密码，例如：openssl rand -hex 16"
fi

# ---------------------------------------------------------------------------
# 1. 安装 mysql@8.4
# ---------------------------------------------------------------------------
if "$BREW" list --versions mysql@8.4 >/dev/null 2>&1; then
  ok "mysql@8.4 已安装，跳过安装步骤"
else
  info "安装 mysql@8.4（含 9 个依赖，约 200MB，视网速需数分钟）"
  "$BREW" install mysql@8.4 || die "brew install 失败，请查看上方完整报错"
  ok "安装完成"
fi

[ -x "$MYSQL_BIN_DIR/mysql" ] || die "安装后仍未找到 $MYSQL_BIN_DIR/mysql"

# ---------------------------------------------------------------------------
# 2. 启动服务
# ---------------------------------------------------------------------------
info "启动 MySQL 服务"
if "$BREW" services list 2>/dev/null | grep -q "^mysql@8.4.*started"; then
  ok "服务已在运行"
else
  "$BREW" services start mysql@8.4 || warn "brew services 启动失败，稍后将尝试手动检查"
fi

# ---------------------------------------------------------------------------
# 3. 等待就绪
# ---------------------------------------------------------------------------
info "等待 mysqld 就绪（最长 60 秒）"
READY=0
for i in $(seq 1 60); do
  if "$MYSQL_BIN_DIR/mysqladmin" --protocol=TCP -h 127.0.0.1 -P 3306 ping >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
[ "$READY" = "1" ] || die "60 秒内 MySQL 未就绪。排查：brew services list / 端口 3306 是否被占用"
ok "mysqld 已响应"

# ---------------------------------------------------------------------------
# 4. 创建数据库与专用用户
# ---------------------------------------------------------------------------
# 全新 brew 安装的 root 无密码；若你此前设置过 root 密码，这里会失败，
# 此时请手动执行下面三条 SQL，或临时改用 -p 传入。
info "创建数据库 $MYSQL_DATABASE 与用户 $MYSQL_USER"

SQL=$(cat <<EOSQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';

ALTER USER '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';

GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'localhost';
FLUSH PRIVILEGES;
EOSQL
)

if echo "$SQL" | "$MYSQL_BIN_DIR/mysql" --protocol=TCP -h 127.0.0.1 -u root 2>/tmp/mysql_setup_err.log; then
  ok "数据库与用户创建成功"
else
  warn "以 root 无密码连接失败，错误如下："
  sed 's/^/      /' /tmp/mysql_setup_err.log
  warn "若 root 已设密码，请手动执行："
  echo "$SQL" | sed 's/^/      /'
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. 以应用用户验证连接
# ---------------------------------------------------------------------------
info "以 $MYSQL_USER 验证连接"
if MYSQL_PWD="$MYSQL_PASSWORD" "$MYSQL_BIN_DIR/mysql" --protocol=TCP \
     -h 127.0.0.1 -P 3306 -u "$MYSQL_USER" -D "$MYSQL_DATABASE" \
     -e "SELECT VERSION() AS version, DATABASE() AS db, CURRENT_USER() AS user;" 2>/dev/null; then
  ok "连接验证通过"
else
  die "以应用用户连接失败，请检查 .env 中的 MYSQL_PASSWORD 与授权"
fi

echo
ok "MySQL 初始化完成"
echo "  版本路径: $MYSQL_BIN_DIR"
echo "  数据库  : $MYSQL_DATABASE"
echo "  用户    : $MYSQL_USER@127.0.0.1"
echo
echo "提示：mysql@8.4 为 keg-only，命令行调用 mysql 前需让 PATH 生效；"
echo "      ~/.zprofile 已写入 export PATH=\"$MYSQL_BIN_DIR:\$PATH\"，"
echo "      新开终端或执行 source ~/.zprofile 即可。"
