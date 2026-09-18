"""Testes do Product Agent com LLM falso (fake chat model)."""

from schemas.extraction import ProductExtraction
from tools import AnalyzeProductImageTool, ToolResult


class FakeStructuredModel:
    """Simula model.with_structured_output(ProductExtraction).invoke()."""

    def __init__(self, extraction: ProductExtraction):
        self.extraction = extraction

    def invoke(self, messages):
        return self.extraction


class FakeChatModel:
    def __init__(self, extraction: ProductExtraction):
        self.extraction = extraction

    def with_structured_output(self, schema):
        return FakeStructuredModel(self.extraction)


SAMPLE_EXTRACTION = ProductExtraction(
    name="Camisa feminina",
    description="Camisa feminina azul",
    color="Azul",
    size=None,
    wholesale_price=35.0,
    retail_price=59.90,
)


def make_tool(extraction: ProductExtraction = SAMPLE_EXTRACTION) -> AnalyzeProductImageTool:
    return AnalyzeProductImageTool(chat_model=FakeChatModel(extraction))


def test_analyze_returns_missing_fields_computed():
    tool = make_tool()
    result = tool.run(text="Camisa feminina azul, atacado 35, varejo 59.90")
    assert result.success
    assert result.data["name"] == "Camisa feminina"
    assert result.data["wholesale_price"] == 35.0
    # size estava null no LLM -> deve aparecer em missing_fields
    assert result.data["missing_fields"] == ["size"]


def test_analyze_ignores_llm_declared_missing_and_recomputes():
    # LLM "alucinou" missing_fields errado; a tool recalcula
    wrong = SAMPLE_EXTRACTION.model_copy(update={"missing_fields": ["name", "color"]})
    result = make_tool(wrong).run(text="texto")
    assert result.success
    assert result.data["missing_fields"] == ["size"]


def test_analyze_requires_input():
    result = make_tool().run()
    assert not result.success
    assert not result.retryable


def test_analyze_complete_product():
    complete = SAMPLE_EXTRACTION.model_copy(update={"size": "M"})
    result = make_tool(complete).run(text="Camisa feminina azul M, 35/59.90")
    assert result.success
    assert result.data["missing_fields"] == []


def test_analyze_llm_failure_returns_retryable():
    class BrokenModel:
        def with_structured_output(self, schema):
            raise RuntimeError("timeout da API")

    tool = AnalyzeProductImageTool(chat_model=BrokenModel())
    result = tool.run(text="qualquer")
    assert not result.success
    assert result.retryable
    assert isinstance(result, ToolResult)
