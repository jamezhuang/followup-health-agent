"""仅给旧版固定返回序列测试增加隔离配置；不修改应用或.env。"""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil


MARKER = "# CAREBRIEF_LEGACY_V0_CONTRACT_ISOLATION"
BLOCK = '''

# CAREBRIEF_LEGACY_V0_CONTRACT_ISOLATION
import pytest as _carebrief_legacy_pytest


@_carebrief_legacy_pytest.fixture(autouse=True)
def carebrief_legacy_v0_contract_isolation(request, monkeypatch):
    # 旧v0验证最终validate阶段的错误码，模型替身没有新版输出修正序列。
    # 仅隔离这个旧合同；V1/V2/V3可靠性与网页工具测试继续启用新功能。
    if request.node.path.name == "test_v0.py":
        monkeypatch.setenv("AGENT_RELIABILITY_ENABLED", "false")
        monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "false")
        monkeypatch.setenv("AGENT_RELIABILITY_V3_ENABLED", "false")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    tests = root / "tests"
    if not tests.is_dir():
        raise ValueError("找不到apps/api/tests，请把脚本放在apps/api/scripts。")
    target = tests / "conftest.py"
    original = target.read_bytes() if target.exists() else b""
    text = original.decode("utf-8-sig")
    if MARKER in text:
        print("旧v0测试隔离配置已存在，无需重复修改。")
        return
    updated = text + BLOCK
    ast.parse(updated)
    if not args.apply:
        print("检查通过。加--apply会备份并追加tests/conftest.py；不改应用和.env。")
        return
    newline = "\r\n" if b"\r\n" in original else "\n"
    encoded = updated.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    if original.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    if target.exists():
        backup = root / ".cache" / "legacy_contract_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup.mkdir(parents=True)
        shutil.copy2(target, backup / "conftest.py")
        print("原conftest.py已备份：", backup / "conftest.py")
    staged = target.with_suffix(".py.tmp")
    staged.write_bytes(encoded)
    staged.replace(target)
    print("已追加测试隔离配置。线上可靠性开关保持原设置。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, SyntaxError) as error:
        raise SystemExit("未完成测试配置更新：" + str(error)) from None
