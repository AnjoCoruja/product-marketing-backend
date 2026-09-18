from agents import ProductAgent
from schemas.extraction import ProductExtraction
from tools import ValidateProductTool
from tools.analyze_product_image import AnalyzeProductImageTool

from .test_product_agent import FakeChatModel


def test_validate_ok():
    tool = ValidateProductTool()
    result = tool.run(
        extraction={
            "name": "Camisa feminina",
            "description": "Camisa feminina azul",
            "color": "Azul",
            "size": None,
            "wholesale_price": 35,
            "retail_price": 59.90,
            "missing_fields": [],
        }
    )
    assert result.success
    assert result.data["product_id"]
    assert result.data["is_complete"] is False
    assert result.data["missing_fields"] == ["size"]


def test_validate_negative_price_rejected():
    result = ValidateProductTool().run(
        extraction={"name": "X", "retail_price": -10}
    )
    assert not result.success
    assert not result.retryable


def test_validate_complete_extraction():
    result = ValidateProductTool().run(
        extraction={
            "name": "Tênis",
            "description": "Tênis esportivo",
            "color": "Preto",
            "size": "42",
            "wholesale_price": 80,
            "retail_price": 149.90,
        }
    )
    assert result.success
    assert result.data["is_complete"] is True
    assert result.data["missing_fields"] == []


def test_product_agent_process_end_to_end():
    extraction = ProductExtraction(
        name="Camisa feminina",
        description="Camisa feminina azul",
        color="Azul",
        size=None,
        wholesale_price=35.0,
        retail_price=59.90,
    )
    agent = ProductAgent(
        analyze_tool=AnalyzeProductImageTool(chat_model=FakeChatModel(extraction))
    )
    result = agent.process(text="Camisa feminina azul, atacado 35, varejo 59.90")

    assert result["success"] is True
    assert result["product_id"]
    assert result["name"] == "Camisa feminina"
    assert result["color"] == "Azul"
    assert result["size"] is None
    assert result["wholesale_price"] == 35.0
    assert result["retail_price"] == 59.90
    assert result["missing_fields"] == ["size"]
    assert result["is_complete"] is False
