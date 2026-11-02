#!/usr/bin/env python3
"""阶段 0 环境自检。

用法：
    source .venv/bin/activate
    python scripts/check_environment.py

设计原则：
1. 只读探测：不安装、不修改任何系统配置、不读取 .env 内容（只判断文件是否存在）。
2. 三态判定：PASS 正常 / WARN 可继续但需注意 / FAIL 阻塞当前阶段。
3. 退出码：0 = 无阻塞项；1 = 存在阻塞项。供阶段门禁直接调用。

判定口径说明：
- Python 版本是硬门槛，必须 3.11.x；不符即 FAIL。
- 是否处于虚拟环境是硬门槛；在系统解释器里跑出 PASS 会误导后续操作。
- MySQL 缺失只降级为 WARN（任务书 §3.6 允许先 CSV + SQLite 完成分析）。
- DeepSeek Key 缺失只降级为 WARN（可用 Mock 模式验证流程）。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# 收集结果，最后统一输出，避免中途失败导致报告不完整
results: list[dict] = []


def record(item: str, status: str, detail: str, action: str = "") -> None:
    """登记单项检查结果。

    action 用于给 FAIL / WARN 项直接给出可执行的下一步，避免用户再去查文档。
    """
    results.append({"item": item, "status": status, "detail": detail, "action": action})


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """执行外部命令并返回 (退出码, 合并输出)。命令不存在时返回 (127, 错误信息)。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:  # 兜底：任何探测失败都不应让脚本崩掉
        return 1, f"{type(exc).__name__}: {exc}"


def first_line(text: str) -> str:
    """取多行输出的第一行非空内容，用于报告里保持整洁。"""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "(空)"


# ---------------------------------------------------------------------------
# 1. 系统与硬件
# ---------------------------------------------------------------------------


def check_system() -> None:
    record("操作系统", PASS, f"{platform.system()} {platform.release()} ({platform.machine()})")

    code, out = run(["sw_vers", "-productVersion"])
    if code == 0:
        record("macOS 版本", PASS, out)

    code, out = run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if code == 0:
        record("芯片型号", PASS, out)

    # 内存：用于判断是否足以跑 embedding 推理（本项目不跑本地大模型）
    code, out = run(["sysctl", "-n", "hw.memsize"])
    if code == 0:
        gb = int(out) / (1024**3)
        status = PASS if gb >= 8 else WARN
        record("内存", status, f"{gb:.0f} GB", "" if status == PASS else "低于 8GB，建议缩小分批规模")

    # 磁盘：embedding 模型缓存 + 依赖 + 向量库，预留 8-10GB
    usage = shutil.disk_usage("/")
    free_gb = usage.free / (1024**3)
    status = PASS if free_gb >= 10 else WARN
    record(
        "磁盘可用空间",
        status,
        f"{free_gb:.0f} GB",
        "" if status == PASS else "建议清理至 10GB 以上，模型与依赖需要空间",
    )


# ---------------------------------------------------------------------------
# 2. Python 运行时
# ---------------------------------------------------------------------------


def check_python() -> None:
    version = platform.python_version()
    if version.startswith("3.11."):
        record("Python 版本", PASS, version)
    else:
        record(
            "Python 版本",
            FAIL,
            version,
            "项目要求 3.11.x；用 uv 安装：uv python install 3.11.16，再 uv venv --python 3.11.16 .venv",
        )

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        record("虚拟环境", PASS, sys.prefix)
    else:
        record(
            "虚拟环境",
            FAIL,
            "当前不在 .venv 内",
            "执行 source .venv/bin/activate 后重跑本脚本",
        )

    # 依赖是可选项：未安装时只提示，不阻塞阶段 0 的文件初始化
    for mod in ("pandas", "numpy", "sqlalchemy", "pymysql", "dotenv"):
        try:
            __import__(mod)
            record(f"依赖 {mod}", PASS, "已安装")
        except ImportError:
            record(
                f"依赖 {mod}",
                WARN,
                "未安装",
                "pip install -r requirements.txt",
            )


# ---------------------------------------------------------------------------
# 3. 外部工具链
# ---------------------------------------------------------------------------


def check_tools() -> None:
    # ---- Git ----
    code, out = run(["git", "--version"])
    if code == 0:
        record("Git", PASS, out)
        # 提交身份缺失会导致 commit 失败，归类为 WARN 而非 FAIL：阶段 0 尚未提交
        _, name = run(["git", "config", "user.name"])
        _, email = run(["git", "config", "user.email"])
        if name and email:
            record("Git 提交身份", PASS, f"{name} <{email}>")
        else:
            record(
                "Git 提交身份",
                WARN,
                f"user.name={'空' if not name else name}, user.email={'空' if not email else email}",
                "提交前需配置：git config user.name / git config user.email",
            )
    else:
        record("Git", FAIL, "未安装", "安装 Xcode Command Line Tools：xcode-select --install")

    # ---- Homebrew ----
    brew_path = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else "")
    if brew_path:
        code, out = run([brew_path, "--version"])
        record("Homebrew", PASS if code == 0 else WARN, first_line(out) if code == 0 else out)
    else:
        record(
            "Homebrew",
            WARN,
            "未找到",
            "见 docs/00_requirements_and_design.md 第 9.5 节",
        )

    # ---- MySQL ----
    # 客户端可能来自三处：PATH、Homebrew 的 keg-only 目录、Oracle tarball 解压目录
    mysql_path = shutil.which("mysql") or resolve_mysql_bin("mysql")
    if mysql_path:
        code, out = run([mysql_path, "--version"])
        record("MySQL 客户端", PASS if code == 0 else WARN, first_line(out) if code == 0 else out)

        # 服务是否真的在跑。这里用 TCP 连通性判断，而不是 `mysqladmin ping`：
        # mysqladmin 需要鉴权，未带凭据时会以 "connect to server failed" 回报，
        # 与"服务没启动"无法区分，容易误报。鉴权是否正常由 check_database 负责验证。
        try:
            with socket.create_connection(("127.0.0.1", 3306), timeout=3):
                pass
            record("MySQL 服务", PASS, "127.0.0.1:3306 可连接")
        except OSError as exc:
            record(
                "MySQL 服务",
                WARN,
                f"端口 3306 不可连接（{exc.__class__.__name__}）",
                "本机由 LaunchAgent 托管：launchctl kickstart -k gui/$(id -u)/com.local.mysql",
            )
    else:
        record(
            "MySQL 客户端",
            WARN,
            "未在 PATH、/opt/homebrew/opt/mysql@8.4/bin、~/mysql/*/bin 中找到",
            "把客户端目录加入 ~/.zprofile 的 PATH",
        )


def resolve_mysql_bin(name: str) -> str:
    """在常见安装位置查找 MySQL 可执行文件，返回绝对路径或空串。

    本机实际安装是 Oracle 官方 tarball（~/mysql/mysql-<版本>-<平台>/bin），
    不在 Homebrew 前缀下，因此不能只依赖 shutil.which。
    """
    candidates = [
        Path("/opt/homebrew/opt/mysql@8.4/bin") / name,
        Path("/opt/homebrew/opt/mysql/bin") / name,
        Path("/usr/local/mysql/bin") / name,
    ]
    # Oracle tarball：~/mysql/mysql-8.4.7-macos15-arm64/bin
    candidates.extend(sorted(Path.home().glob(f"mysql/mysql-*/bin/{name}"), reverse=True))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def check_database() -> None:
    """用项目配置真实连一次数据库。

    这一步的价值在于：它验证的是"应用能不能连上"，而不只是"客户端装没装"。
    失败只降级为 WARN——阶段 1 之前允许数据库尚未就绪。
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src import config  # noqa: PLC0415 延迟导入，避免依赖缺失时整个脚本崩掉

        from sqlalchemy import create_engine, text  # noqa: PLC0415

        engine = create_engine(config.mysql_url(), pool_pre_ping=True, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            database = conn.execute(text("SELECT DATABASE()")).scalar()
        record("数据库连接", PASS, f"{database} @ MySQL {version}（用户 {config.MYSQL_USER}）")
    except ModuleNotFoundError as exc:
        record("数据库连接", WARN, f"缺少依赖: {exc.name}", "pip install -r requirements.txt")
    except Exception as exc:  # 连接/鉴权/库不存在都归到这里
        record(
            "数据库连接",
            WARN,
            f"{type(exc).__name__}: {str(exc)[:120]}",
            "检查 .env 中的 MYSQL_* 配置与 ~/mysql/ROOT_PASSWORD.txt",
        )

    # ---- VS Code ----
    vscode_app = Path("/Applications/Visual Studio Code.app")
    if vscode_app.exists():
        record("VS Code", PASS, "已安装")
    else:
        record("VS Code", WARN, "未在 /Applications 找到", "从官网安装")

    # ---- gh（推送用，可选）----
    gh = shutil.which("gh") or str(Path.home() / "bin" / "gh")
    if Path(gh).exists() if gh else False:
        code, out = run([gh, "auth", "status"], timeout=15)
        status = PASS if code == 0 else WARN
        record("GitHub CLI 认证", status, "已登录" if code == 0 else first_line(out))
    else:
        record("GitHub CLI 认证", WARN, "未找到 gh", "可改用 HTTPS + Token 推送")


# ---------------------------------------------------------------------------
# 4. 项目结构
# ---------------------------------------------------------------------------


def check_project() -> None:
    required_dirs = [
        "data/raw",
        "data/processed",
        "data/simulated",
        "knowledge_base",
        "docs",
        "notebooks",
        "sql",
        "src",
        "src/rag",
        "scripts",
        "tests",
        "outputs/tables",
    ]
    missing = [d for d in required_dirs if not (PROJECT_ROOT / d).is_dir()]
    if missing:
        record("目录结构", FAIL, f"缺少 {len(missing)} 个目录: {', '.join(missing)}")
    else:
        record("目录结构", PASS, f"{len(required_dirs)} 个必需目录齐备")

    key_files = [".gitignore", ".env.example", "requirements.txt", "README.md"]
    missing_files = [f for f in key_files if not (PROJECT_ROOT / f).is_file()]
    if missing_files:
        record("关键文件", WARN, f"缺少: {', '.join(missing_files)}")
    else:
        record("关键文件", PASS, "齐备")

    # .env 是否存在：存在即说明已配置密钥；这里只判断存在性，绝不读取内容
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        record(".env", PASS, "存在（内容未读取）")
    else:
        record(
            ".env",
            WARN,
            "不存在，当前仅有 .env.example",
            "需要真正调用 DeepSeek 时再复制：cp .env.example .env 并填入密钥",
        )

    # 防泄漏：.gitignore 必须覆盖 .env 与 .venv
    gitignore = PROJECT_ROOT / ".gitignore"
    if gitignore.is_file():
        text = gitignore.read_text(encoding="utf-8")
        checks = {".env": ".env" in text, ".venv": ".venv" in text, "data/chroma": "chroma" in text}
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            record("防泄漏检查", FAIL, f".gitignore 未覆盖: {', '.join(bad)}")
        else:
            record("防泄漏检查", PASS, ".env / .venv / data/chroma 均已忽略")
    else:
        record("防泄漏检查", FAIL, ".gitignore 不存在")

    # .env 若已被 Git 跟踪，属于严重泄漏，直接 FAIL
    code, out = run(["git", "ls-files", ".env"])
    if code == 0 and out.strip():
        record("敏感文件追踪", FAIL, ".env 已被 Git 追踪", "git rm --cached .env")
    else:
        record("敏感文件追踪", PASS, ".env 未被追踪")

    # Git 仓库与远程
    code, out = run(["git", "rev-parse", "--is-inside-work-tree"])
    if code == 0 and out.strip() == "true":
        record("Git 仓库", PASS, "已初始化")
        _, remote = run(["git", "remote", "-v"])
        if remote.strip():
            record("Git 远程", PASS, first_line(remote))
        else:
            record("Git 远程", WARN, "未配置 remote", "git remote add origin <仓库地址>")
    else:
        record("Git 仓库", WARN, "尚未初始化", "git init -b main")


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------


def render() -> None:
    symbols = {PASS: "✓", WARN: "!", FAIL: "×"}
    width = max(len(r["item"]) for r in results) + 2

    print()
    print("=" * 78)
    print("阶段 0 环境自检报告")
    print(f"项目根目录: {PROJECT_ROOT}")
    print("=" * 78)
    for r in results:
        print(f"[{symbols[r['status']]}] {r['item']:<{width}} {r['status']:<5} {r['detail']}")
        if r["action"]:
            print(f"    {'':<{width}} → {r['action']}")

    counts = {s: sum(1 for r in results if r["status"] == s) for s in (PASS, WARN, FAIL)}
    print("-" * 78)
    print(f"合计: PASS {counts[PASS]} 项 / WARN {counts[WARN]} 项 / FAIL {counts[FAIL]} 项")
    print("=" * 78)


def main() -> int:
    check_system()
    check_python()
    check_tools()
    check_database()
    check_project()
    render()

    summary = {
        "project_root": str(PROJECT_ROOT),
        "results": results,
        "counts": {s: sum(1 for r in results if r["status"] == s) for s in (PASS, WARN, FAIL)},
    }
    # 机器可读输出，便于后续脚本或 CI 直接消费
    (PROJECT_ROOT / "outputs").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "outputs" / "environment_check.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    has_blocker = any(r["status"] == FAIL for r in results)
    print()
    if has_blocker:
        print("结论：存在阻塞项，请按上述 → 提示处理后再进入下一阶段。")
        return 1
    print("结论：无阻塞项。可进入阶段 1（数据获取与质量审查）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
