"""全局测试夹具。

项目发现会读「历史线索」（~/.claude.json、编辑器最近工作区等）——那是**开发机**
的真实数据，开着会让 discover 的结果里混进开发者自己的项目，把「发现结果应当
只有这些」这类断言全部打歪。所以测试默认关闭线索发现；要验证线索逻辑的用例
自己 monkeypatch 对应提取器。
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_project_hints(monkeypatch):
    monkeypatch.setenv("A4AGENT_PROJECT_HINTS", "0")
