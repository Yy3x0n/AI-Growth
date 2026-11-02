#!/usr/bin/env python
"""密钥泄露检查：在提交前拦住真实凭据。

背景：本仓库是公开仓库。`.env` 虽然已在 `.gitignore` 中，但 git 有两种常见绕过方式：
  1. `git add -f .env`（强制添加会忽略 .gitignore）；
  2. 把 key 直接粘进 README / 脚本 / 文档里。
这两种都能让密钥进历史，而一旦推送，即便删除文件，历史里依然可检出。

用法：
    python scripts/check_no_secrets.py            # 检查暂存区（pre-commit 默认行为）
    python scripts/check_no_secrets.py --all      # 检查全部已跟踪文件
    python scripts/check_no_secrets.py a.py b.md  # 检查指定文件

退出码：0 通过；1 发现疑似密钥。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 明确的密钥格式。这些误报率极低，命中即视为泄露。
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("智谱 GLM", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16,}\b")),
    ("OpenAI/DeepSeek", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("私钥文件头", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
]

# 键值对形式的通用检测（如 "password = 'xxxxxx'"）。误报率较高，故排除占位符。
GENERIC_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b
    \s*[:=]\s*
    ['"]?([A-Za-z0-9_\-\.]{16,})['"]?
    """
)

# 占位符特征：命中即跳过通用检测（但仍会过 SECRET_PATTERNS）
PLACEHOLDER_HINTS = (
    "replace", "your", "example", "placeholder", "xxxx", "todo",
    "changeme", "dummy", "sample", "***", "redacted", "<", "占位",
)

# 禁止进入仓库的文件（无论内容）
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

# 允许的例外：示例文件本身就是给人看格式的
ALLOWLIST_SUFFIXES = (".example", ".sample", ".template")


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if f.strip()]


def is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in chunk


def _looks_like_code_expression(value: str) -> bool:
    """判断右值是不是代码引用而非字面量。

    不加这个判断会出现大量误报：`password=config.MYSQL_PASSWORD` 会被当成硬编码密码。
    实测在 4 个脚本里各命中一次（全是正常的变量引用）。
    真实凭据通常是"无点号、无括号、无 $ 的随机串"，因此这几条足以区分。
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+", value):
        return True  # config.MYSQL_PASSWORD / os.environ.X
    return any(ch in value for ch in "()[]{}<>$")


def check_path(path: Path) -> list[str]:
    """返回问题描述列表，空列表表示通过。"""
    problems: list[str] = []

    name = path.name
    if name in FORBIDDEN_NAMES:
        return [f"{path}：该文件不允许提交（密钥文件），请加入 .gitignore"]
    if name.endswith(FORBIDDEN_SUFFIXES):
        return [f"{path}：私钥/证书文件不允许提交"]

    if not path.is_file() or is_binary(path):
        return problems

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return problems

    allowlisted = name.endswith(ALLOWLIST_SUFFIXES)

    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            problems.append(
                f"{path}：疑似 {label} —— 「{_mask(match.group(0))}」"
            )

    if not allowlisted:
        for match in GENERIC_ASSIGNMENT.finditer(text):
            value = match.group(1)
            if any(hint in value.lower() for hint in PLACEHOLDER_HINTS):
                continue
            if _looks_like_code_expression(value):
                continue
            line_no = text[: match.start()].count("\n") + 1
            problems.append(
                f"{path}:{line_no}：疑似硬编码凭据 —— 「{match.group(0)[:40]}」"
            )

    return problems


def _mask(secret: str) -> str:
    """报告里只显示前 6 位，避免把密钥又打印到日志/终端。"""
    return secret[:6] + "…" + f"（共 {len(secret)} 字符）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查提交中是否含密钥")
    parser.add_argument("--all", action="store_true", help="检查全部已跟踪文件，而非暂存区")
    parser.add_argument("files", nargs="*", help="指定文件（优先于 --all / 暂存区）")
    args = parser.parse_args(argv)

    if args.files:
        targets = args.files
    elif args.all:
        targets = tracked_files()
    else:
        targets = staged_files()

    if not targets:
        print("没有需要检查的文件，跳过。")
        return 0

    all_problems: list[str] = []
    for item in targets:
        all_problems.extend(check_path(Path(item)))

    if all_problems:
        print("✗ 检测到疑似密钥，已阻止提交：\n", file=sys.stderr)
        for problem in all_problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\n处置建议：\n"
            "  1. 真实密钥只放 .env（已在 .gitignore）；\n"
            "  2. 文档/示例里用占位符，如 replace_with_your_key；\n"
            "  3. 若密钥已进历史，删除文件不够，必须轮换密钥（历史仍可检出）。",
            file=sys.stderr,
        )
        return 1

    print(f"✓ 已检查 {len(targets)} 个文件，未发现明文密钥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
