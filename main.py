import os
import sys
import asyncio
import logging
import subprocess
from typing import Dict

from dotenv import load_dotenv

from agents import Agent
from agents.mcp import MCPServerStdio
from dotenv import load_dotenv
load_dotenv()

from my_agents import get_available_agent_ids, create_agent_by_id
from telegram_bot import OrdersBot

import truststore  # <--- AGGIUNGI QUESTO
truststore.inject_into_ssl()  # <--- E QUESTO, SUBITO DOPO L'IMPORT

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("main")

class SkipTelegramPollingFilter(logging.Filter):
    """
    Filtra le chiamate di polling verso Telegram (getUpdates),
    così non intasano il log quando non ci sono nuovi messaggi.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # I log di httpx hanno forma:
        # "HTTP Request: GET https://api.telegram.org/bot.../getUpdates "HTTP/1.1 200 OK""
        if "api.telegram.org" in msg and "getUpdates" in msg:
            return False  # NON loggare questa riga
        return True       # tutto il resto passa


# Applica il filtro solo ai log HTTP di httpx
httpx_logger = logging.getLogger("httpx")
httpx_logger.addFilter(SkipTelegramPollingFilter())



async def main() -> None:
    """Punto di ingresso principale dell'applicazione.

    Sequenza:
    1) Carica variabili d'ambiente
    2) Avvia la REST API locale (uvicorn rest_api:app)
    3) Avvia il MCP server (mcp_server.py)
    4) Carica dinamicamente gli agent dal file my_agents.xlm/xml
    5) Avvia il bot Telegram che usa gli agent
    """
    load_dotenv()

    # ================== AVVIO REST API (uvicorn) ==================
    rest_proc: subprocess.Popen | None = None
    try:
        # Se vuoi personalizzare il comando, puoi usare la variabile d'ambiente
        # ORDERS_REST_COMMAND, altrimenti uso python -m uvicorn rest_api:app ...
        rest_cmd_env = os.getenv("ORDERS_REST_COMMAND")
        if rest_cmd_env:
            # esempio: ORDERS_REST_COMMAND="python -m uvicorn rest_api:app --host 127.0.0.1 --port 8001 --reload"
            rest_cmd = rest_cmd_env.split()
        else:
            # Comando di default per avviare la REST API
            rest_cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "rest_api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--reload",  # se non ti serve il reload, puoi toglierlo
            ]

        logger.info("Avvio REST API con comando: %s", " ".join(rest_cmd))
        rest_proc = subprocess.Popen(
            rest_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("REST API avviata con PID=%s", rest_proc.pid)

        # ================== AVVIO MCP SERVER ==================

        mcp_command = os.getenv("ORDERS_MCP_COMMAND", sys.executable)
        mcp_script = os.getenv("ORDERS_MCP_SCRIPT", "mcp_server.py")

        # Il context manager avvia il processo MCP e lo chiude alla fine
        async with MCPServerStdio(
            name="Orders MCP Server",
            params={
                "command": mcp_command,
                "args": [mcp_script],
            },
            cache_tools_list=True,  # evita di richiedere i tool ad ogni run
            client_session_timeout_seconds=30.0,  # timeout più lungo per il Raspberry
        ) as orders_mcp_server:
            logger.info("MCP server avviato, carico gli agent dal file XML...")

            # ================== CREAZIONE AGENT DAL FILE XML ==================
            agent_ids = get_available_agent_ids()
            if not agent_ids:
                raise RuntimeError("Nessun <Agent> definito nel file my_agents.xml.")

            logger.info("ID agent disponibili: %s", agent_ids)

            agents: Dict[str, Agent] = {}
            for agent_id in agent_ids:
                agents[agent_id] = create_agent_by_id(agent_id, orders_mcp_server)
                logger.info("Creato agent id='%s' dal file XML.", agent_id)

            # --- Scelta dinamica dell'agent di default ---
            # 1) Provo a leggerlo da variabile d'ambiente (facoltativa)
            default_agent_id = os.getenv("ORDERS_DEFAULT_AGENT_ID")

            # 2) Se non impostata o non valida, uso il primo definito nel file XML
            if not default_agent_id or default_agent_id not in agents:
                if default_agent_id and default_agent_id not in agents:
                    logger.warning(
                        "ORDERS_DEFAULT_AGENT_ID='%s' non è presente tra gli agent (%s). "
                        "Uso il primo definito nel file XML.",
                        default_agent_id,
                        list(agents.keys()),
                    )
                default_agent_id = agent_ids[0]

            logger.info(
                "Creo il bot OrdersBot con gli agent: %s (default='%s')",
                list(agents.keys()),
                default_agent_id,
            )

            bot = OrdersBot(agents=agents, default_agent_id=default_agent_id)

            try:
                await bot.run()
            except Exception:
                logger.exception("Il bot si è fermato a causa di un errore")
            finally:
                logger.info("Chiusura OrdersBot completata.")

    finally:
        # ================== ARRESTO REST API ==================
        if rest_proc is not None:
            if rest_proc.poll() is None:
                logger.info("Invio terminate() alla REST API (PID=%s)...", rest_proc.pid)
                rest_proc.terminate()
                try:
                    rest_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("REST API non si è chiusa in tempo, forzo kill")
                    rest_proc.kill()
            else:
                logger.info("REST API già terminata (returncode=%s)", rest_proc.returncode)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interruzione da tastiera, arresto applicazione...")
