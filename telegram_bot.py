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

from agents import Agent, Runner, SQLiteSession

logger = logging.getLogger(__name__)


class OrdersBot:
    """Bot Telegram che delega la logica a uno o più Agent OpenAI.

    - orders_agent: gestisce ORDINI / prezzi / listini
    - customers_agent: gestisce CLIENTI (anagrafica) se presente
    """

    def __init__(self, orders_agent: Agent, customers_agent: Agent | None = None) -> None:
        # Carica variabili da .env (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ecc.)
        load_dotenv()

        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN non impostato nelle variabili d'ambiente")

        # Agent
        self.orders_agent = orders_agent
        self.customers_agent = customers_agent

        # Sessioni per memorizzare la conversazione (una per chat Telegram)
        self.sessions: Dict[int, SQLiteSession] = {}

        # Agent "corrente" per ogni chat (ordini o clienti)
        self.current_agent: Dict[int, Agent] = {}

        # Application di python-telegram-bot
        self.application: Application | None = None

    # ---------- utility sessione per chat ----------

    def _get_session(self, chat_id: int) -> SQLiteSession:
        if chat_id not in self.sessions:
            # usa un DB locale 'db/sessions.db'
            sessions_path = os.path.join("database", "sessions.db")
            self.sessions[chat_id] = SQLiteSession(str(chat_id), sessions_path)
        return self.sessions[chat_id]

    def _select_agent(self, chat_id: int, text: str) -> Agent:
        """Sceglie quale agent usare in base al contenuto del messaggio + stato precedente."""
        t = text.lower().strip()

        # --- regole per passare esplicitamente alla gestione CLIENTI ---
        if self.customers_agent is not None:
            if t.startswith("/cliente") or t.startswith("/nuovocliente"):
                self.current_agent[chat_id] = self.customers_agent
                return self.customers_agent

            if (
                "nuovo cliente" in t
                or "inserisci un nuovo cliente" in t
                or "inserisci cliente" in t
                or "crea cliente" in t
                or "registrare un cliente" in t
            ):
                self.current_agent[chat_id] = self.customers_agent
                return self.customers_agent

        # --- regole per passare esplicitamente alla gestione ORDINI ---
        if (
            "nuovo ordine" in t
            or "inserisci un ordine" in t
            or "inserire un ordine" in t
            or "ordine" in t
            or "ordini" in t
        ):
            self.current_agent[chat_id] = self.orders_agent
            return self.orders_agent

        # --- se non ci sono keyword, riuso l'agent già in uso per quella chat ---
        if chat_id in self.current_agent:
            return self.current_agent[chat_id]

        # --- default assoluto: ordini ---
        self.current_agent[chat_id] = self.orders_agent
        return self.orders_agent

    # ---------- handlers comandi ----------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gestisce /start"""
        if not update.message:
            return

        chat_id = update.message.chat_id
        # di default mettiamo la chat in modalità "ordini"
        self.current_agent[chat_id] = self.orders_agent

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
        if chat_id in self.current_agent:
            del self.current_agent[chat_id]

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

            # Scegli l'agent in base al testo + stato della chat
            agent = self._select_agent(chat_id, user_message)

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
