"""Entrypoint de demonstração da fundação.

Executa uma run mínima do grafo em memória:
    telegram (simulado) -> product_agent -> marketing_agent -> approval -> finish

Uso:
    python main.py
    python main.py --text "Tênis esportivo azul, tamanho 42"
"""

import argparse

from config import configure_logging, get_logger, get_settings
from graphs import build_graph, create_initial_state
from schemas import RunType, TriggerSource


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    logger = get_logger("main")

    parser = argparse.ArgumentParser(description="Run de fundação do sistema multiagente")
    parser.add_argument("--text", default="Produto de exemplo", help="Texto de intake")
    parser.add_argument("--run-type", default="new_product", choices=["new_product", "recampaign"])
    args = parser.parse_args()

    graph = build_graph()
    state = create_initial_state(
        run_type=RunType(args.run_type),
        trigger_source=TriggerSource.TELEGRAM,
        intake_text=args.text,
        telegram_chat_id=123,
        telegram_message_id=1,
    )

    final = graph.invoke(state, config={"configurable": {"thread_id": state.run_id}})
    logger.info(
        "run concluída",
        extra={
            "run_id": final["run_id"] if isinstance(final, dict) else final.run_id,
            "stage": (final.get("current_stage") if isinstance(final, dict) else final.current_stage),
        },
    )
    print("\n--- Estado final ---")
    print(final)


if __name__ == "__main__":
    main()
