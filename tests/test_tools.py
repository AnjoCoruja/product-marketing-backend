import pytest

from tools import BaseTool, ToolRegistry, ToolResult


class DummyTool(BaseTool):
    name = "dummy"
    description = "tool de teste"

    def run(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, data={"echo": kwargs})


def test_tool_result_contract():
    r = ToolResult(success=False, error="boom", retryable=True)
    assert not r.success and r.retryable


def test_registry_register_and_get():
    reg = ToolRegistry()
    reg.register(DummyTool())
    tool = reg.get("dummy")
    result = tool.run(value=42)
    assert result.success
    assert result.data["echo"]["value"] == 42
    assert reg.names() == ["dummy"]


def test_registry_missing_tool():
    with pytest.raises(KeyError):
        ToolRegistry().get("inexistente")


def test_base_tool_abstract():
    # ABC impede instanciar tool sem implementar run()
    class Incomplete(BaseTool):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()
