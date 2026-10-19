

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
