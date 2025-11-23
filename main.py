import os
import sys
import asyncio
import logging
import subprocess

from dotenv import load_dotenv

from agents.mcp import MCPServerStdio

from my_agents import load_bot_agents
from telegram_bot import OrdersBot

# ================== LOGGING ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """
    Punto di ingresso dell'applicazione.

    - Legge .env (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ecc.)
    - Avvia il server REST locale (rest_api.py via uvicorn)
    - Avvia il server MCP locale (mcp_server.py via MCPServerStdio)
    - Crea due Agent (ordini + clienti) collegati al MCP server
    - Crea il bot Telegram e lo avvia
    """
    load_dotenv()

    # ================== AVVIO REST API ==================
    rest_proc = None
    try:
        # Comando per avviare uvicorn come modulo:
        # python -m uvicorn orders_rest_api:app --host 127.0.0.1 --port 8001 --reload
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

        # piccolo delay per dare tempo a uvicorn di mettersi in ascolto
        await asyncio.sleep(1.5)

        # ================== AVVIO MCP SERVER ==================
        mcp_command = os.getenv("ORDERS_MCP_COMMAND", sys.executable)
        mcp_script = os.getenv("ORDERS_MCP_SCRIPT", "mcp_server.py")

        logger.info("Avvio MCPServerStdio: %s %s", mcp_command, mcp_script)

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
            logger.info("MCP server avviato, creo gli agent...")

            # ================== CREAZIONE AGENT DAL FILE XML ==================
            logger.info("Carico gli agent per il bot dal file XML (my_agents.xlm/xml)...")
            orders_agent, customers_agent = load_bot_agents(orders_mcp_server)

            logger.info("Creo il bot OrdersBot collegato agli agent...")
            bot = OrdersBot(orders_agent=orders_agent, customers_agent=customers_agent)
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
                logger.info("Arresto REST API (PID=%s)...", rest_proc.pid)
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
