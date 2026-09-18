from langgraph.checkpoint.memory import MemorySaver

from agents import MarketingAgent, ProductAgent
from graphs import build_graph, create_initial_state
from graphs.routers import route_after_approval, route_by_run_type
from schemas import AgentState, RunType, Stage


def test_router_new_product():
    s = AgentState(run_type=RunType.NEW_PRODUCT)
    assert route_by_run_type(s) == "product_agent"


def test_router_recampaign():
    s = AgentState(run_type=RunType.RECAMPAIGN)
    assert route_by_run_type(s) == "marketing_agent"


def test_router_approval():
    assert route_after_approval(AgentState(human_decision="approved")) == "finish"
    assert route_after_approval(AgentState(human_decision="rejected")) == "marketing_agent"
    assert route_after_approval(AgentState(human_decision=None)) == "finish"


def test_product_agent_node():
    from schemas.extraction import ProductExtraction
    from tools.analyze_product_image import AnalyzeProductImageTool
    from tests.test_product_agent import FakeChatModel

    extraction = ProductExtraction(name="Tênis", retail_price=149.9)
    agent = ProductAgent(
        analyze_tool=AnalyzeProductImageTool(chat_model=FakeChatModel(extraction))
    )
    out = agent.run(AgentState(intake_text="Tênis azul 42", telegram_message_id=7))
    assert out["product_id"]
    assert out["current_stage"] == Stage.CATALOGING


def test_marketing_agent_node():
    agent = MarketingAgent()
    out = agent.run(AgentState(product_id="p1"))
    assert out["campaign_id"]
    assert out["current_stage"] == Stage.APPROVAL
    assert out["pending_human_action"] == "campaign_approval"


def test_full_graph_new_product():
    graph = build_graph(checkpointer=MemorySaver())
    state = create_initial_state(intake_text="Bolsa de couro", telegram_chat_id=1)
    final = graph.invoke(state, config={"configurable": {"thread_id": state.run_id}})

    assert final["current_stage"] == Stage.DONE
    assert final["product_id"] == state.product_id or final["product_id"]
    assert final["campaign_id"]
    assert final["pending_human_action"] is None


def test_full_graph_recampaign_skips_product_agent():
    graph = build_graph(checkpointer=MemorySaver())
    state = create_initial_state(run_type=RunType.RECAMPAIGN, product_id="prod-existente")
    final = graph.invoke(state, config={"configurable": {"thread_id": state.run_id}})

    assert final["current_stage"] == Stage.DONE
    assert final["product_id"] == "prod-existente"  # não foi sobrescrito
