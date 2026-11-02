"""密钥检查脚本的测试。

注意：本文件刻意不写出"完整可匹配的密钥字面量"。
    如果直接写一个符合智谱格式的假 key，`check_no_secrets.py` 会把它当成真泄露，
    导致提交被自己的钩子拦下。所以用拼接方式构造测试数据——这不是为了绕过检测，
    而是让"测试数据"与"真实凭据"在源码层面就不同形。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_no_secrets as cns  # noqa: E402

# 拼接构造：运行时是合法格式，源码里不是
FAKE_GLM = "0123456789abcdef" * 2 + "." + "ABCDEFGHIJKLMNOP"
FAKE_OPENAI = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture
def workdir():
    """不用 pytest 的 tmp_path：当前沙箱里它在部分场景会创建目录失败。"""
    path = Path(tempfile.mkdtemp(prefix="secprobe_"))
    yield path
    for item in path.iterdir():
        item.unlink(missing_ok=True)
    path.rmdir()


def _write(directory: Path, name: str, content: str) -> Path:
    target = directory / name
    target.write_text(content, encoding="utf-8")
    return target


class TestSecretPatterns:
    def test_detects_zhipu_style_key(self, workdir):
        path = _write(workdir, "doc.md", f"GLM_API_KEY={FAKE_GLM}\n")
        problems = cns.check_path(path)
        assert problems
        assert "智谱" in problems[0]

    def test_detects_openai_style_key(self, workdir):
        path = _write(workdir, "doc.md", f"key: {FAKE_OPENAI}\n")
        problems = cns.check_path(path)
        assert problems
        assert any("OpenAI" in p or "DeepSeek" in p for p in problems)

    def test_detects_private_key_header(self, workdir):
        # 拼接构造，理由同上：源码里不能出现完整的私钥头
        header = "-----BEGIN RSA" + " PRIVATE KEY-----"
        path = _write(workdir, "id.pem", f"{header}\nabc\n")
        problems = cns.check_path(path)
        assert problems

    def test_detects_hardcoded_password(self, workdir):
        # 同样拼接构造：源码里不出现「password = "长字面量"」的完整形态，
        # 否则钩子会在提交测试文件时把它当成真泄露（实际发生过一次）。
        fake = "Pa55w0rd" + "VerySecret123"
        path = _write(workdir, "cfg.py", f'password = "{fake}"\n')
        problems = cns.check_path(path)
        assert problems
        assert any("硬编码" in p for p in problems)


class TestFalsePositives:
    """误报会让人直接关掉钩子，因此这些用例和检测用例同等重要。"""

    def test_placeholder_allowed(self, workdir):
        path = _write(workdir, "a.md", "GLM_API_KEY=replace_with_your_glm_key\n")
        assert cns.check_path(path) == []

    def test_env_var_reference_allowed(self, workdir):
        """password=config.MYSQL_PASSWORD 是变量引用，不是硬编码。

        实测这条在 4 个脚本里各命中一次，不加区分会让钩子完全不可用。
        """
        path = _write(workdir, "s.py", "conn(password=config.MYSQL_PASSWORD)\n")
        assert cns.check_path(path) == []

    def test_attribute_chain_allowed(self, workdir):
        path = _write(workdir, "s.py", "x = os.getenv('A'); y = settings.api_key\n")
        assert cns.check_path(path) == []

    def test_short_value_allowed(self, workdir):
        path = _write(workdir, "s.py", 'password = "abc"\n')
        assert cns.check_path(path) == []


class TestForbiddenFiles:
    def test_env_file_rejected_by_name(self, workdir):
        """即使内容是占位符，.env 本身也不允许进仓库。"""
        path = _write(workdir, ".env", "GLM_API_KEY=\n")
        problems = cns.check_path(path)
        assert problems
        assert "不允许提交" in problems[0]

    def test_pem_suffix_rejected(self, workdir):
        path = _write(workdir, "server.pem", "irrelevant\n")
        assert cns.check_path(path)


class TestMasking:
    def test_mask_hides_most_of_secret(self):
        masked = cns._mask(FAKE_GLM)
        assert masked.startswith(FAKE_GLM[:6])
        assert FAKE_GLM[10:] not in masked, "报告里不得回显完整密钥"


class TestRepositoryItself:
    def test_current_tracked_files_are_clean(self):
        """仓库自身必须零告警，否则钩子会被当成噪音。"""
        codes = cns.main(["--all"])
        assert codes == 0
