import os
import logging
import asyncio
from typing import Dict

from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

from agents import Agent, Runner, SQLiteSession
from my_agents import get_agents_router_metadata

from telegram.request import HTTPXRequest  # se ti serve in futuro

logger = logging.getLogger(__name__)


class OrdersBot:
    """Bot Telegram che delega la logica a uno o più Agent OpenAI.

    Gli agent disponibili vengono passati come dizionario, ad esempio:
        {
            "orders":    Agent(...),
            "customers": Agent(...),
            ...
        }

    Il legame tra id e comportamento (ordini, clienti, ecc.) è definito nel
    file di configurazione XML (my_agents.xml).
    """

    def __init__(self, agents: Dict[str, Agent], default_agent_id: str = "orders") -> None:
        # Carica variabili da .env (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ecc.)
        load_dotenv()

        # Log "sicuro" della API key OpenAI usata
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logger.warning("OPENAI_API_KEY NON impostata nelle variabili d'ambiente!")
        else:
            masked = api_key[:8] + "..." + api_key[-4:]
            logger.info("OPENAI_API_KEY attiva (parziale): %s", masked)

        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN non impostato nelle variabili d'ambiente")

        if not agents:
            raise RuntimeError("Nessun agent passato a OrdersBot.")

        if default_agent_id not in agents:
            raise RuntimeError(
                f"default_agent_id='{default_agent_id}' non presente in agents: {list(agents.keys())}"
            )

        # Dizionario {agent_id: Agent}
        self.agents: Dict[str, Agent] = agents
        self.default_agent_id: str = default_agent_id

        # Dizionario con i metadati degli agent caricati da my_agents.xml
        # { agent_id: { name, description, role, tools_usage, main_flows } }
        self.router_agents_meta: Dict[str, dict] = get_agents_router_metadata()

        # Client OpenAI per il routing LLM
        # Usa OPENAI_API_KEY dalle variabili d'ambiente
        self.router_client = OpenAI()

        # Sessioni per memorizzare la conversazione (una per chat Telegram)
        self.sessions: Dict[int, SQLiteSession] = {}

        # agent_id "corrente" per ogni chat (ordini, clienti, ecc.)
        self.current_agent_id: Dict[int, str] = {}

        # Application di python-telegram-bot
        self.application: Application | None = None

    # ---------- utility sessione per chat ----------

    def _get_session(self, chat_id: int) -> SQLiteSession:
        if chat_id not in self.sessions:
            # usa un DB locale 'database/sessions.db'
            os.makedirs("database", exist_ok=True)
            sessions_path = os.path.join("database", "sessions.db")
            self.sessions[chat_id] = SQLiteSession(str(chat_id), sessions_path)
        return self.sessions[chat_id]

    # ---------- router LLM per scegliere l'agent ----------

    async def _llm_choose_agent(self, user_text: str) -> str:
        """
        Usa l'LLM per decidere quale agent_id usare tra quelli disponibili.

        La lista e le descrizioni degli agent vengono derivate
        direttamente dal file my_agents.xml, tramite my_agents.py.
        """
        available_ids = list(self.agents.keys())

        # Se c'è un solo agent, è inutile chiamare l'LLM
        if len(available_ids) == 1:
            logger.info(
                "Router LLM: un solo agent disponibile (%s), lo uso senza chiamare il modello.",
                available_ids[0],
            )
            return available_ids[0]

        # Metadati derivati dal file XML
        meta = self.router_agents_meta

        # Costruisco il prompt per il router
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

            # Per sicurezza non facciamo diventare il prompt infinito
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

        # Cerco un match esatto con uno degli id disponibili
        for agent_id in available_ids:
            if answer == agent_id.lower():
                logger.info("Router LLM - match esatto: %s", agent_id)
                return agent_id

        # Se la risposta contiene uno degli id come parola, lo uso
        for agent_id in available_ids:
            if agent_id.lower() in answer:
                logger.info("Router LLM - match parziale: %s in %r", agent_id, answer)
                return agent_id

        # Ultimo fallback
        logger.warning(
            "Router LLM non ha restituito un id valido (%r), uso default_agent_id=%s",
            raw_answer,
            self.default_agent_id,
        )
        return self.default_agent_id

    async def _select_agent(self, chat_id: int, text: str) -> Agent:
        """
        Sceglie quale agent usare:
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

        # Usa il router LLM per scegliere l'agent_id
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

    # ---------- handlers comandi ----------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestisce /start"""
        if not update.message:
            return

        chat_id = update.message.chat_id
        # di default mettiamo la chat in modalità agent di default (es. 'orders')
        self.current_agent_id[chat_id] = self.default_agent_id

        text = (
            "Ciao! Sono OrdersBot 🤖\n\n"
            "Posso aiutarti a lavorare con gli ordini e gli altri domini gestiti dagli agent configurati in my_agents.xml.\n\n"
            "Scrivimi cosa vuoi fare, ad esempio:\n"
            "- *Inserisci un nuovo ordine per il cliente 123...*\n"
            "- *Mostrami lo stato dell'ordine 456...*\n"
            "- *Registra un nuovo cliente con questi dati...*\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resetta la memoria della conversazione per quella chat."""
        if not update.message:
            return

        chat_id = update.message.chat_id
        if chat_id in self.sessions:
            del self.sessions[chat_id]
        if chat_id in self.current_agent_id:
            del self.current_agent_id[chat_id]

        await update.message.reply_text("🔁 Ho azzerato la memoria della chat (contesto e sessione).")

    async def agent_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Permette di selezionare manualmente l'agent: /agent orders, /agent customers, ecc."""
        if not update.message:
            return

        chat_id = update.message.chat_id
        args = context.args or []

        if not args:
            available = ", ".join(self.agents.keys())
            await update.message.reply_text(
                f"Specificare l'agent, ad es. /agent orders\n"
                f"Agent disponibili: {available}"
            )
            return

        requested = args[0].strip().lower()
        if requested not in self.agents:
            available = ", ".join(self.agents.keys())
            await update.message.reply_text(
                f"Agent '{requested}' non trovato.\n"
                f"Agent disponibili: {available}"
            )
            return

        self.current_agent_id[chat_id] = requested
        await update.message.reply_text(f"✅ D'ora in poi userò l'agent *{requested}* per questa chat.", parse_mode="Markdown")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestisce /help"""
        if not update.message:
            return

        text = (
            "Posso aiutarti a:\n"
            "- Inserire nuovi ordini\n"
            "- Consultare lo stato avanzamento ordini\n"
            "- Recuperare prezzi/listini articoli\n"
            "- Inserire nuovi clienti in anagrafica\n\n"
            "Esempi:\n"
            "- *Vorrei inserire un ordine per il cliente CLI_001 per 10 pezzi di mela.*\n"
            "- *Registra un nuovo cliente con codice CLI_050: Frutta & Co, via Roma 10 Torino.*\n"
            "- *Elencami i clienti registrati.*\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    # ---------- handler messaggi normali ----------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestisce tutti i messaggi di testo non comandi."""
        if not update.message or not update.message.text:
            return

        user_message = update.message.text.strip()
        chat_id = update.message.chat_id

        logger.info("Messaggio da %s: %s", chat_id, user_message)

        # Mostra "sta scrivendo..."
        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            session = self._get_session(chat_id)

            # Scegli l'agent in base al testo usando il router LLM
            agent = await self._select_agent(chat_id, user_message)

            # Chiama l'Agent (che a sua volta userà MCP quando serve)
            result = await Runner.run(
                agent,
                input=user_message,
                session=session,
            )

            reply_text = result.final_output or "Non ho ottenuto alcuna risposta dall'agent."
            await update.message.reply_text(reply_text)

        except Exception:
            logger.exception("Errore durante l'elaborazione del messaggio")
            await update.message.reply_text(
                "❌ Mi spiace, ho avuto un errore interno mentre processavo la tua richiesta."
            )

    # ---------- avvio bot ----------

    async def run(self) -> None:
        """Avvia il bot Telegram dentro un event loop già esistente (niente run_polling)."""
        logger.info("Inizializzo OrdersBot...")

        # Crea l'application se non esiste ancora
        if self.application is None:
            # NESSUN HTTPXRequest qui, lasciamo che PTB usi httpx di default
            self.application = (
                Application.builder()
                .token(self.telegram_token)
                .build()
            )

            # Handler comandi
            self.application.add_handler(CommandHandler("start", self.start))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("reset", self.reset_command))
            self.application.add_handler(CommandHandler("agent", self.agent_command))

            # Handler messaggi di testo
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
            )

        app = self.application

        # Sequenza consigliata quando NON si usa run_polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        logger.info("Bot in esecuzione (polling)…")

        # Tieni vivo il processo finché non viene terminato
        await asyncio.Event().wait()
