import os
import sys
import asyncio
import logging
import subprocess
from typing import Dict
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI

from agents import Agent, Runner, SQLiteSession
from agents.mcp import MCPServerStdio

from my_agents import (
    get_available_agent_ids,
    create_agent_by_id,
    get_agents_router_metadata,
)

import truststore
truststore.inject_into_ssl()

import tkinter as tk
from tkinter import ttk


# ================== LOGGING ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("local_chat_gui")


# ================== BACKEND CHAT (stesso core di OrdersBot, ma senza Telegram) ==================

class LocalOrdersChat:
    """
    Backend "core" che replica la logica di OrdersBot, ma senza Telegram.

    - Usa gli stessi Agent (orders, customers, item, RdL, ecc.)
    - Usa lo stesso router LLM per scegliere l'agent
    - Usa SQLiteSession per memorizzare il contesto conversazionale
    """

    def __init__(self, agents: Dict[str, Agent], default_agent_id: str = "orders") -> None:
        load_dotenv()

        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logger.warning("OPENAI_API_KEY NON impostata nelle variabili d'ambiente!")
        else:
            masked = api_key[:8] + "..." + api_key[-4:]
            logger.info("OPENAI_API_KEY attiva (parziale): %s", masked)

        if not agents:
            raise RuntimeError("Nessun agent passato a LocalOrdersChat.")

        if default_agent_id not in agents:
            raise RuntimeError(
                f"default_agent_id='{default_agent_id}' non presente in agents: {list(agents.keys())}"
            )

        # Dizionario {agent_id: Agent}
        self.agents: Dict[str, Agent] = agents
        self.default_agent_id: str = default_agent_id

        # Metadati agent dal file XML (per il router LLM)
        # { agent_id: { name, description, role, tools_usage, main_flows } }
        self.router_agents_meta: Dict[str, dict] = get_agents_router_metadata()

        # Client OpenAI per il routing LLM
        self.router_client = OpenAI()

        # Sessioni (una per "chat"). Qui usiamo un solo chat_id ("local")
        self.sessions: Dict[str, SQLiteSession] = {}

        # agent_id corrente per chat
        self.current_agent_id: Dict[str, str] = {}

    # ---------- utility sessione ----------

    def _get_session(self, chat_id: str) -> SQLiteSession:
        if chat_id not in self.sessions:
            os.makedirs("database", exist_ok=True)
            sessions_path = os.path.join("database", "sessions.db")
            self.sessions[chat_id] = SQLiteSession(chat_id, sessions_path)
        return self.sessions[chat_id]

    # ---------- router LLM per scegliere l'agent ----------

    async def _llm_choose_agent(self, user_text: str) -> str:
        """
        Stessa logica del router LLM di OrdersBot, ma senza Telegram.
        """
        available_ids = list(self.agents.keys())

        # Se c'è un solo agent, inutile chiamare l'LLM
        if len(available_ids) == 1:
            logger.info(
                "Router LLM: un solo agent disponibile (%s), lo uso senza chiamare il modello.",
                available_ids[0],
            )
            return available_ids[0]

        meta = self.router_agents_meta

        lines = [
            "Sei un router che deve instradare il messaggio dell'utente verso il giusto Agent.",
            "",
            "Devi scegliere a quale AGENT inoltrare il messaggio dell'utente.",
            "Hai a disposizione i seguenti agent_id:",
        ]

        for agent_id in available_ids:
            info = meta.get(agent_id, {})
            name = info.get("name") or agent_id
            description = info.get("description", "")
            role = info.get("role", "")
            tools_usage = info.get("tools_usage", "")
            main_flows = info.get("main_flows", "")

            desc_parts: list[str] = []
            if description:
                desc_parts.append(description)
            if role:
                desc_parts.append(role)
            if tools_usage:
                desc_parts.append("Uso dei tool: " + tools_usage)
            if main_flows:
                desc_parts.append("Flussi principali: " + main_flows)

            desc_text = " ".join(desc_parts).strip()
            if len(desc_text) > 600:
                desc_text = desc_text[:600] + "..."

            lines.append(f"- {agent_id} ({name}): {desc_text}")

        lines.append("")
        lines.append("Regole:")
        lines.append("- Rispondi SOLO con uno dei seguenti id di agent, senza altre parole:")
        lines.append("  " + ", ".join(available_ids))
        lines.append("- Non spiegare la scelta, non aggiungere testo.")
        lines.append("")
        lines.append("Messaggio dell'utente:")
        lines.append(user_text)

        prompt = "\n".join(lines)

        def _call_openai() -> str:
            response = self.router_client.responses.create(
                model="gpt-4.1-mini",   # modello leggero per routing
                input=prompt,
                max_output_tokens=20,
            )
            return (response.output_text or "").strip()

        try:
            raw_answer = await asyncio.to_thread(_call_openai)
            answer = raw_answer.strip().lower()
            logger.info("Router LLM - risposta grezza: %r", raw_answer)
        except Exception:
            logger.exception("Errore durante la chiamata al router LLM; uso l'agent di default.")
            return self.default_agent_id

        # Match esatto
        for agent_id in available_ids:
            if answer == agent_id.lower():
                logger.info("Router LLM - match esatto: %s", agent_id)
                return agent_id

        # Match parziale
        for agent_id in available_ids:
            if agent_id.lower() in answer:
                logger.info("Router LLM - match parziale: %s in %r", agent_id, answer)
                return agent_id

        logger.warning(
            "Router LLM non ha restituito un id valido (%r), uso default_agent_id=%s",
            raw_answer,
            self.default_agent_id,
        )
        return self.default_agent_id

    async def _select_agent(self, chat_id: str, text: str) -> Agent:
        """
        Stessa logica di OrdersBot._select_agent:
        - per brevi risposte di conferma riusa l'agent corrente
        - altrimenti chiede al router LLM quale agent_id usare
        """
        t = text.lower().strip()

        # Se ho già un agent in corso e il messaggio è brevissimo (es. "sì", "ok"),
        # mantengo il contesto senza chiamare il router
        if chat_id in self.current_agent_id and len(t.split()) <= 3:
            agent_id = self.current_agent_id[chat_id]
            logger.info(
                "Messaggio breve, mantengo agent corrente '%s' per chat_id=%s",
                agent_id,
                chat_id,
            )
            return self.agents[agent_id]

        agent_id = await self._llm_choose_agent(text)

        if agent_id not in self.agents:
            logger.warning(
                "Router LLM ha scelto un agent_id sconosciuto '%s', uso default_agent_id=%s",
                agent_id,
                self.default_agent_id,
            )
            agent_id = self.default_agent_id

        self.current_agent_id[chat_id] = agent_id
        logger.info(
            "Router: scelto agent_id='%s' per chat_id=%s, messaggio=%r",
            agent_id,
            chat_id,
            text,
        )
        return self.agents[agent_id]

    # ---------- API principale da usare nella chat GUI ----------

    async def process_message(self, user_message: str, chat_id: str = "local") -> str:
        """
        Punto di ingresso principale: dato un testo utente,
        esegue tutta la pipeline Agent + Runner + MCP + REST.
        """
        logger.info("Messaggio (chat_id=%s): %s", chat_id, user_message)

        session = self._get_session(chat_id)
        agent = await self._select_agent(chat_id, user_message)

        result = await Runner.run(
            agent,
            input=user_message,
            session=session,
        )

        reply_text = result.final_output or "Non ho ottenuto alcuna risposta dall'agent."
        return reply_text


# ================== INTERFACCIA GRAFICA (Tkinter) ==================

class ChatWindow(tk.Tk):
    """
    Finestra di chat locale che usa LocalOrdersChat come backend.
    """

    def __init__(self, core: LocalOrdersChat):
        super().__init__()

        self.core = core

        self.title("OrdersBot - Chat locale")
        self.geometry("900x600")

        self._create_widgets()
        self._configure_grid()

        # Messaggio iniziale
        self._append_system_message(
            "Chat locale pronta.\nScrivi un messaggio per interagire con OrdersBot (senza Telegram)."
        )

    # ---------------- UI ----------------

    def _create_widgets(self):
        main_frame = ttk.Frame(self)
        main_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Frame chat
        chat_frame = ttk.Frame(main_frame)
        chat_frame.grid(row=0, column=0, sticky="nsew")

        self.chat_text = tk.Text(
            chat_frame,
            wrap="word",
            state="disabled",
            bg="#1e1e1e",
            fg="#ffffff",
            insertbackground="#ffffff",
        )
        self.chat_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(chat_frame, orient="vertical", command=self.chat_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_text["yscrollcommand"] = scrollbar.set

        # Tag di stile
        self.chat_text.tag_configure("user", foreground="#4fc3f7", font=("Consolas", 10, "bold"))
        self.chat_text.tag_configure("bot", foreground="#a5d6a7", font=("Consolas", 10, "bold"))
        self.chat_text.tag_configure("time", foreground="#9e9e9e", font=("Consolas", 8, "italic"))
        self.chat_text.tag_configure("body", foreground="#ffffff", font=("Consolas", 10))

        # Frame input
        input_frame = ttk.Frame(main_frame)
        input_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.input_text = tk.Text(
            input_frame,
            height=3,
            wrap="word",
        )
        self.input_text.grid(row=0, column=0, sticky="ew")

        send_button = ttk.Button(input_frame, text="Invia", command=self.on_send_clicked)
        send_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # Invio = manda, Shift+Invio = a capo
        self.input_text.bind("<Return>", self._on_enter)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter)

    def _configure_grid(self):
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        main_frame = next(iter(self.children.values()))
        main_frame.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)

        chat_frame = next(iter(main_frame.children.values()))
        chat_frame.rowconfigure(0, weight=1)
        chat_frame.columnconfigure(0, weight=1)

        input_frame = list(main_frame.children.values())[1]
        input_frame.columnconfigure(0, weight=1)

    # ---------------- gestione input ----------------

    def _on_enter(self, event):
        self.on_send_clicked()
        return "break"

    def _on_shift_enter(self, event):
        self.input_text.insert("insert", "\n")
        return "break"

    def on_send_clicked(self):
        user_text = self.input_text.get("1.0", "end").strip()
        if not user_text:
            return

        self.input_text.delete("1.0", "end")

        self._append_user_message(user_text)
        self._set_input_state("disabled")

        # Avvia la chiamata al backend in modo asincrono
        asyncio.create_task(self._process_message_async(user_text))

    async def _process_message_async(self, user_text: str):
        try:
            reply = await self.core.process_message(user_text, chat_id="local")
        except Exception as e:
            logger.exception("Errore durante l'elaborazione del messaggio")
            reply = f"❌ Errore interno: {e}"

        self._append_bot_message(reply)
        self._set_input_state("normal")

    # ---------------- append messaggi ----------------

    def _append_user_message(self, text: str):
        self._append_message(sender="Tu", text=text, tag="user")

    def _append_bot_message(self, text: str):
        self._append_message(sender="Bot", text=text, tag="bot")

    def _append_system_message(self, text: str):
        self._append_message(sender="Sistema", text=text, tag="bot")

    def _append_message(self, sender: str, text: str, tag: str):
        self.chat_text.config(state="normal")

        timestamp = datetime.now().strftime("%H:%M:%S")

        self.chat_text.insert("end", f"{sender} ", (tag,))
        self.chat_text.insert("end", f"[{timestamp}]\n", ("time",))
        self.chat_text.insert("end", text + "\n\n", ("body",))

        self.chat_text.config(state="disabled")
        self.chat_text.see("end")

    def _set_input_state(self, state: str):
        self.input_text.config(state=state)
        if state == "normal":
            self.input_text.focus_set()


# ================== MAIN: AVVIO REST + MCP + GUI ==================

async def main() -> None:
    """
    Sequenza:
    1) Carica variabili d'ambiente
    2) Avvia la REST API (uvicorn rest_api:app)
    3) Avvia MCP server (mcp_server.py)
    4) Carica gli Agent da my_agents.xml
    5) Avvia interfaccia grafica di chat
    """
    load_dotenv()

    rest_proc: subprocess.Popen | None = None
    try:
        # --- AVVIO REST API (come in main.py) ---
        rest_cmd_env = os.getenv("ORDERS_REST_COMMAND")
        if rest_cmd_env:
            rest_cmd = rest_cmd_env.split()
        else:
            rest_cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "rest_api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--reload",
            ]

        logger.info("Avvio REST API con comando: %s", " ".join(rest_cmd))
        rest_proc = subprocess.Popen(
            rest_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("REST API avviata con PID=%s", rest_proc.pid)

        # --- AVVIO MCP SERVER ---
        mcp_command = os.getenv("ORDERS_MCP_COMMAND", sys.executable)
        mcp_script = os.getenv("ORDERS_MCP_SCRIPT", "mcp_server.py")

        async with MCPServerStdio(
            name="Orders MCP Server",
            params={
                "command": mcp_command,
                "args": [mcp_script],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=30.0,
        ) as orders_mcp_server:
            logger.info("MCP server avviato, carico gli Agent dal file XML.")

            # --- CREAZIONE AGENT DAL FILE XML ---
            agent_ids = get_available_agent_ids()
            if not agent_ids:
                raise RuntimeError("Nessun <Agent> definito nel file my_agents.xml.")

            logger.info("ID agent disponibili: %s", agent_ids)

            agents: Dict[str, Agent] = {}
            for agent_id in agent_ids:
                agents[agent_id] = create_agent_by_id(agent_id, orders_mcp_server)
                logger.info("Creato Agent id='%s' dal file XML.", agent_id)

            default_agent_id = "orders" if "orders" in agents else agent_ids[0]
            if default_agent_id != "orders":
                logger.warning(
                    "Nessun Agent con id='orders' trovato, uso '%s' come default.",
                    default_agent_id,
                )

            logger.info(
                "LocalOrdersChat: Agent disponibili: %s (default='%s')",
                list(agents.keys()),
                default_agent_id,
            )

            core = LocalOrdersChat(agents=agents, default_agent_id=default_agent_id)

            # --- AVVIO GUI ---
            app = ChatWindow(core)

            logger.info("Interfaccia grafica avviata. In attesa dell'utente...")

            # Integrazione Tkinter + asyncio: ciclo manuale
            try:
                while True:
                    app.update()
                    await asyncio.sleep(0.01)
            except tk.TclError:
                logger.info("Finestra chiusa, termino l'applicazione.")

    finally:
        # --- ARRESTO REST API ---
        if rest_proc is not None:
            if rest_proc.poll() is None:
                logger.info("Invio terminate() alla REST API (PID=%s).", rest_proc.pid)
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
        logger.info("Interruzione da tastiera, arresto applicazione.")
