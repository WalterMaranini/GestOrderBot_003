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
    file di configurazione XML (my_agents.xlm / my_agents.xml).
    """

    def __init__(self, agents: Dict[str, Agent], default_agent_id: str = "orders") -> None:
        # Carica variabili da .env (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ecc.)
        load_dotenv()

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
            sessions_path = os.path.join("database", "sessions.db")
            self.sessions[chat_id] = SQLiteSession(str(chat_id), sessions_path)
        return self.sessions[chat_id]

    # ---------- router LLM per scegliere l'agent ----------

    async def _llm_choose_agent(self, user_text: str) -> str:
        """
        Usa l'LLM per decidere quale agent_id usare tra quelli disponibili.

        Restituisce uno degli id presenti in self.agents oppure,
        in caso di errore, self.default_agent_id.
        """
        available_ids = list(self.agents.keys())

        # Se c'è un solo agent, è inutile chiamare l'LLM
        if len(available_ids) == 1:
            logger.info(
                "Router LLM: un solo agent disponibile (%s), lo uso senza chiamare il modello.",
                available_ids[0],
            )
            return available_ids[0]

        # Costruisco una descrizione compatta degli agent noti
        lines = [
            "Sei un router per un gestionale ordini.",
            "",
            "Devi scegliere a quale AGENT inoltrare il messaggio dell'utente.",
            "Hai a disposizione i seguenti agent_id:",
        ]

        for agent_id in available_ids:
            if agent_id == "orders":
                desc = (
                    "gestione ordini, righe d'ordine, listini, prezzi articoli legati agli ordini, "
                    "stato avanzamento ordini."
                )
            elif agent_id == "customers":
                desc = (
                    "gestione clienti, anagrafiche, codici cliente, indirizzi, elenco clienti."
                )
            else:
                desc = f"dominio applicativo legato al nome '{agent_id}'."
            lines.append(f"- {agent_id}: {desc}")

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
                "Router: riuso agent_id='%s' per chat_id=%s (messaggio breve: %r)",
                agent_id,
                chat_id,
                text,
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
            "Ciao! 👋 Sono il tuo assistente ordini e anagrafica clienti.\n\n"
            "Esempi:\n"
            "- *Inserisci un nuovo ordine per il cliente CLI_001 con consegna domani*\n"
            "- *Mostrami lo stato dell'ordine 10*\n"
            "- *Registra un nuovo cliente con codice CLI_999 e ragione sociale ...*\n"
            "- *Che prezzi abbiamo per l'articolo mela?*\n\n"
            "Scrivi in linguaggio naturale e penserò io a parlare con il gestionale. 😉"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

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

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resetta la memoria della conversazione per quella chat."""
        if not update.message:
            return

        chat_id = update.message.chat_id

        if chat_id in self.sessions:
            session = self.sessions[chat_id]
            # pulizia contenuto sessione
            await session.clear_session()
            del self.sessions[chat_id]

        # reset anche dell'agent corrente
        if chat_id in self.current_agent_id:
            del self.current_agent_id[chat_id]

        await update.message.reply_text(
            "✅ Ho azzerato la memoria della conversazione per questa chat."
        )

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
            await update.message.reply_text(reply_text, parse_mode="Markdown")

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
            self.application = (
                Application.builder()
                .token(self.telegram_token)
                .build()
            )

            # Handler comandi
            self.application.add_handler(CommandHandler("start", self.start))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("reset", self.reset_command))

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
