import os
import sys
import asyncio
import logging
import subprocess
from typing import Dict, Any
from datetime import datetime
import json

from dotenv import load_dotenv
from openai import OpenAI

from agents import Agent, Runner, SQLiteSession
from agents.mcp import MCPServerStdio

from my_agents import (
    get_available_agent_ids,
    create_agent_by_id,
    get_agents_router_metadata,
)
import tkinter as tk
from tkinter import ttk
import truststore
truststore.inject_into_ssl()

# ================== LOGGING ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("local_chat")


# ================== BACKEND CHAT ==================

class LocalChat:

    def __init__(self, agents: Dict[str, Agent], default_agent_id: str = "orders") -> None:
        load_dotenv()

        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logger.warning("OPENAI_API_KEY NON impostata nelle variabili d'ambiente!")
        else:
            masked = api_key[:8] + "..." + api_key[-4:]
            logger.info("OPENAI_API_KEY attiva (parziale): %s", masked)

        if not agents:
            raise RuntimeError("Nessun agent passato a LocalChat.")

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

        # Cambio agent in attesa di conferma esplicita dell'utente
        self.pending_agent_switch: Dict[str, str] = {}

        # Messaggio utente associato alla proposta di cambio agent (da riusare se l'utente dice "no")
        self.pending_agent_message: Dict[str, str] = {}

    # ---------- utility sessione ----------

    def _get_session(self, chat_id: str) -> SQLiteSession:
        if chat_id not in self.sessions:
            os.makedirs("database", exist_ok=True)
            sessions_path = os.path.join("database", "sessions.db")
            self.sessions[chat_id] = SQLiteSession(chat_id, sessions_path)
        return self.sessions[chat_id]

    def reset_session(self, chat_id: str) -> None:
        """
        Azzera lo stato in memoria per una determinata chat.

        - Dimentica la history associata a chat_id lato processo.
        - Se SQLiteSession ha un metodo reset()/clear(), prova a chiamarlo.
        """
        sess = self.sessions.get(chat_id)
        if sess is not None:
            try:
                reset_fn = getattr(sess, "reset", None) or getattr(sess, "clear", None)
                if callable(reset_fn):
                    try:
                        reset_fn()
                    except Exception:
                        logger.exception(
                            "Errore durante reset della SQLiteSession per chat_id=%s",
                            chat_id,
                        )
            except Exception:
                logger.exception(
                    "Errore inatteso durante reset_session(chat_id=%s)", chat_id
                )

        self.sessions.pop(chat_id, None)
        self.current_agent_id.pop(chat_id, None)
        self.pending_agent_switch.pop(chat_id, None)
        self.pending_agent_message.pop(chat_id, None)
        logger.info("Sessione azzerata per chat_id=%s", chat_id)

    # ---------- logging contesto LLM ----------

    def _log_llm_context(
        self,
        *,
        phase: str,
        agent: Agent | None,
        session: SQLiteSession | None,
        user_message: str | None,
        extra: dict | None = None,
    ) -> None:
        """
        Logga in modo strutturato il contesto che stiamo passando all'LLM.

        phase: stringa che indica dove siamo (es. 'router_llm', 'runner_call').
        agent: Agent corrente (può essere None per il router).
        session: SQLiteSession corrente (se disponibile).
        user_message: messaggio utente appena ricevuto o quello originale.
        extra: info extra da attaccare (es. output finale, agent_id, ecc.).
        """
        try:
            ctx: Dict[str, Any] = {
                "phase": phase,
                "user_message": user_message,
            }

            # Info sull'Agent (istruzioni, modello, ecc.)
            if agent is not None:
                ctx["agent_name"] = getattr(agent, "name", None)
                ctx["agent_model"] = getattr(agent, "model", None)
                ctx["agent_instructions"] = getattr(agent, "instructions", None)

            # Info sulla sessione (e, se possibile, history)
            if session is not None:
                ctx["session_repr"] = repr(session)
                # Prova a recuperare la history se la classe la espone
                for attr_name in ("dump_messages", "get_messages", "get_history"):
                    fn = getattr(session, attr_name, None)
                    if callable(fn):
                        try:
                            ctx["session_history"] = fn()
                        except Exception as e:
                            ctx["session_history_error"] = f"{attr_name}() -> {e}"
                        break

            if extra:
                ctx.update(extra)

            # Serializza in JSON per avere un log leggibile
            text = json.dumps(ctx, ensure_ascii=False, default=str)

            # Evitiamo di spaccare il log se diventa enorme
            max_len = 50000
            if len(text) > max_len:
                text = text[:max_len] + " ... [troncato]"

            logger.info("LLM CONTEXT: %s", text)

        except Exception:
            logger.exception("Errore durante il logging del contesto LLM")


    def _handle_switch_confirmation(self, chat_id: str, user_message: str) -> tuple[str, str | None]:
        """
        Se per questa chat c'è un cambio di Agent in attesa di conferma,
        interpreta il messaggio dell'utente come risposta (sì/no).

        Ritorna:
          - reply_text: testo da mostrare subito all'utente
          - original_message: se non è None, è il messaggio precedente da
            processare con l'agente corrente (caso in cui l'utente dice "no").
        """
        answer = (user_message or "").strip().lower()
        proposed_id = self.pending_agent_switch.get(chat_id)
        current_id = self.current_agent_id.get(chat_id)

        if not proposed_id:
            logger.warning(
                "Richiesta conferma cambio agent senza pending_agent_switch per chat_id=%s",
                chat_id,
            )
            return "Non ho alcun cambio di agente in sospeso per questa conversazione.", None

        yes_tokens = {"si", "sì", "ok", "va bene", "certo", "yes", "y"}
        no_tokens = {"no", "no grazie", "non cambiare", "resta", "rimani", "lascia così"}

        def matches(tokens: set[str], txt: str) -> bool:
            return any(txt == t or txt.startswith(t + " ") for t in tokens)

        # L'utente CONFERMA il cambio agent
        if matches(yes_tokens, answer):
            self.current_agent_id[chat_id] = proposed_id
            # Pulisco tutto lo stato pendente
            self.pending_agent_switch.pop(chat_id, None)
            self.pending_agent_message.pop(chat_id, None)
            logger.info(
                "Utente ha confermato cambio agent: chat_id=%s, nuovo_agent=%s",
                chat_id,
                proposed_id,
            )
            reply = (
                f"Perfetto, da ora userò l'agente {proposed_id} "
                "per questa conversazione.\nScrivi pure cosa vuoi fare."
            )
            return reply, None

        # L'utente RIFIUTA il cambio agent:
        # vogliamo comunque processare il messaggio originale con l'agente corrente
        if matches(no_tokens, answer):
            original_msg = self.pending_agent_message.pop(chat_id, None)
            self.pending_agent_switch.pop(chat_id, None)
            logger.info(
                "Utente ha rifiutato cambio agent: chat_id=%s, resto su agent=%s",
                chat_id,
                current_id,
            )
            reply = (
                f"Ok, continuo a usare l'agente {current_id} "
                "per questa conversazione."
            )
            return reply, original_msg

        # Risposta ambigua: non cambio niente, resto in attesa di un sì/no chiaro
        logger.info(
            "Risposta ambigua alla conferma cambio agent: chat_id=%s, answer=%r",
            chat_id,
            answer,
        )
        reply = (
            "Non ho capito se vuoi cambiare agente.\n"
            "Rispondi 'sì' per passare all'agente proposto oppure 'no' "
            "per restare con quello attuale."
        )
        return reply, None

    # ---------- router LLM per scegliere l'agent ----------

    async def _llm_choose_agent(self, user_text: str) -> str:

        available_ids = list(self.agents.keys())

        # Se c'è un solo agent, inutile chiamare l'LLM
        if len(available_ids) == 1:
            logger.info(
                "Router LLM: un solo agent disponibile (%s), lo uso senza chiamare LLM.",
                available_ids[0],
            )
            return available_ids[0]

        meta = self.router_agents_meta

        lines = [
            "Sei un router che deve instradare il messaggio dell'utente verso il giusto Agent.",
            "Devi scegliere a quale AGENT inoltrare il messaggio dell'utente.",
            "Hai a disposizione i seguenti agent_id:",
        ]

        for agent_id in available_ids:
            info = meta.get(agent_id, {})
            name = info.get("name") or agent_id
            description = info.get("description", "")
            role = info.get("role", "")
            # tools_usage = info.get("tools_usage", "")
            main_flows = info.get("main_flows", "")

            desc_parts: list[str] = []
            if description:
                desc_parts.append(description)
            if role:
                desc_parts.append(role)
            # if tools_usage:
            #    desc_parts.append("Uso dei tool: " + tools_usage)
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
        lines.append("Messaggio dell'utente in base al quale devi scegliere il giusto Agente:")
        lines.append(user_text)

        prompt = "\n".join(lines)

        # Log completo del contesto passato al router LLM
        try:
            max_len = 4000
            prompt_log = prompt if len(prompt) <= max_len else prompt[:max_len] + " ... [troncato]"
            logger.info("Router LLM - CONTEXT (prompt len=%d): %s", len(prompt), prompt_log)
        except Exception:
            logger.exception("Errore durante il logging del contesto del router LLM")

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

    async def _select_agent(self, chat_id: str, text: str) -> tuple[Agent, str | None]:

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
            return self.agents[agent_id], None

        # Chiedo al router LLM che agent scegliere
        proposed_id = await self._llm_choose_agent(text)

        if proposed_id not in self.agents:
            logger.warning(
                "Router LLM ha scelto un agent_id sconosciuto '%s', uso default_agent_id=%s",
                proposed_id,
                self.default_agent_id,
            )
            proposed_id = self.default_agent_id

        current_id = self.current_agent_id.get(chat_id)

        # Se esiste già un agent corrente e il router propone qualcosa di diverso,
        # prima di committare il cambio chiedo conferma esplicita all'utente.
        if current_id is not None and current_id != proposed_id:
            self.pending_agent_switch[chat_id] = proposed_id
            # Salvo il messaggio originale, così posso riusarlo se l'utente dice "no"
            self.pending_agent_message[chat_id] = text
            logger.info(
                "Router LLM propone cambio agent: chat_id=%s, corrente=%s, proposto=%s",
                chat_id,
                current_id,
                proposed_id,
            )
            confirm_msg = (
                f"Il tuo messaggio sembra più adatto all'agente {proposed_id} "
                f"invece dell'agente corrente {current_id}.\n"
                "Vuoi che cambi agente? Rispondi 'sì' per confermare "
                "oppure 'no' per restare con l'agente attuale."
            )
            # Restituisco comunque l'agent corrente, ma NON verrà usato
            # perché process_message intercetta confirm_msg e lo ritorna subito.
            return self.agents[current_id], confirm_msg

        # Caso normale: nessun cambio, oppure è il primo agent scelto
        self.current_agent_id[chat_id] = proposed_id
        logger.info(
            "Router: scelto agent_id='%s' per chat_id=%s, messaggio=%r",
            proposed_id,
            chat_id,
            text,
        )
        return self.agents[proposed_id], None

    # ---------- API principale da usare nella chat GUI ----------

    async def process_message(self, user_message: str, chat_id: str = "local") -> str:
        user_message = (user_message or "").strip()
        logger.info("Messaggio (chat_id=%s): %s", chat_id, user_message)

        # 1) Gestione conferma cambio agent (sì/no)
        if chat_id in self.pending_agent_switch:
            logger.info(
                "Messaggio trattato come conferma cambio agent per chat_id=%s",
                chat_id,
            )
            reply_text, original_msg = self._handle_switch_confirmation(chat_id, user_message)

            # Se l'utente ha detto "no", ho un messaggio originale da processare
            if original_msg:
                session = self._get_session(chat_id)
                agent_id = self.current_agent_id.get(chat_id) or self.default_agent_id
                agent = self.agents.get(agent_id, next(iter(self.agents.values())))
                logger.info(
                    "Processo il messaggio precedente con agent_id=%s per chat_id=%s",
                    agent_id,
                    chat_id,
                )

                # Recupero tutta la history dalla sessione
                try:
                    items = await session.get_items()
                except Exception as e:
                    items = f"Errore get_items(): {e}"

                # Log PRIMA della chiamata all'LLM
                self._log_llm_context(
                    phase="runner_call_from_switch_no",
                    agent=agent,
                    session=session,
                    user_message=original_msg,  # <-- quello che stai per mandare all'LLM
                    extra={
                        "chat_id": chat_id,
                        "agent_id": agent_id,
                        "session_items": items,
                    },
                )

                result = await Runner.run(
                    agent,
                    input=original_msg,
                    session=session,
                )

                agent_reply = result.final_output or "Non ho ottenuto alcuna risposta dall'agent."

                # Log DOPO la chiamata all'LLM
                self._log_llm_context(
                    phase="runner_call_from_switch_no_result",
                    agent=agent,
                    session=session,
                    user_message=original_msg,
                    extra={
                        "chat_id": chat_id,
                        "agent_id": agent_id,
                        "session_items": items,
                        "final_output": agent_reply,
                    },
                )

                return reply_text + "\n\n" + agent_reply

            # Se non ho messaggio originale (utente ha detto "sì" o risposta ambigua),
            # ritorno solo il testo di conferma / richiesta chiarimento.
            return reply_text

        # 2) Normale flusso: nessun cambio in sospeso
        session = self._get_session(chat_id)
        logger.info("Seleziono l'Agente in base al contenuto del messaggio")
        agent, confirm_msg = await self._select_agent(chat_id, user_message)

        if confirm_msg:
            return confirm_msg

        current_agent_id = None
        for aid, a in self.agents.items():
            if a is agent:
                current_agent_id = aid
                break

        # Recupero tutta la history dalla sessione
        try:
            items = await session.get_items()
        except Exception as e:
            items = f"Errore get_items(): {e}"

        # Log PRIMA della chiamata
        self._log_llm_context(
            phase="runner_call",
            agent=agent,
            session=session,
            user_message=user_message,
            extra={
                "chat_id": chat_id,
                "agent_id": current_agent_id,
                "session_items": items,
            },
        )

        result = await Runner.run(
            agent,
            input=user_message,
            session=session,
        )

        reply_text = result.final_output or "Non ho ottenuto alcuna risposta dall'agent."

        # Log DOPO la chiamata
        self._log_llm_context(
            phase="runner_call_result",
            agent=agent,
            session=session,
            user_message=user_message,
            extra={
                "chat_id": chat_id,
                "agent_id": current_agent_id,
                "session_items": items,
                "final_output": reply_text,
            },
        )

        return reply_text

        reply_text = result.final_output or "Non ho ottenuto alcuna risposta dall'agent."

        # (opzionale) log anche output finale LLM
        self._log_llm_context(
            phase="runner_call_result",
            agent=agent,
            session=session,
            user_message=user_message,
            extra={
                "chat_id": chat_id,
                "agent_id": current_agent_id,
                "final_output": reply_text,
            },
        )

        return reply_text





# ================== INTERFACCIA GRAFICA (Tkinter) ==================

class ChatWindow(tk.Tk):
    """
    Finestra di chat locale che usa LocalChat come backend.
    """

    def __init__(self, core: LocalChat):
        super().__init__()

        self.core = core

        # Gestione sessioni logiche (per non avere storia fra una chat e l'altra)
        self.chat_index = 1
        self.chat_id = f"local_{self.chat_index}"

        self.title("ESOLVER CHAT")
        self.geometry("900x600")

        self._create_widgets()
        self._configure_grid()

        # Messaggio iniziale
        self._append_system_message(
            "Chat locale pronta.\nScrivi un messaggio per interagire con ESOLVER."
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
        self.chat_text.tag_configure("user", foreground="#4fc3f7", font=("Consolas", 12, "bold"))
        self.chat_text.tag_configure("bot", foreground="#a5d6a7", font=("Consolas", 12, "bold"))
        self.chat_text.tag_configure("time", foreground="#9e9e9e", font=("Consolas", 10, "italic"))
        self.chat_text.tag_configure("body", foreground="#ffffff", font=("Consolas", 12))

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

        # Bottone per azzerare la sessione e ripartire da zero
        reset_button = ttk.Button(
            input_frame,
            text="Nuova sessione",
            command=self.on_reset_session,
        )
        reset_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

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

    def on_reset_session(self):
        """
        Azzera la sessione corrente e ne crea una nuova,
        così l'LLM non vede più la storia precedente.
        """
        # Reset lato backend (se supportato)
        try:
            if hasattr(self.core, "reset_session"):
                self.core.reset_session(self.chat_id)
        except Exception:
            logger.exception("Errore durante il reset della sessione sul core")

        # Nuovo id di sessione logica
        self.chat_index += 1
        self.chat_id = f"local_{self.chat_index}"

        # Pulisci area chat
        self.chat_text.config(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_text.config(state="disabled")

        # Pulisci input
        self.input_text.delete("1.0", "end")

        # Messaggio di sistema
        self._append_system_message(
            f"Nuova sessione avviata (sessione #{self.chat_index}). "
            "La chat non ha più accesso ai messaggi precedenti."
        )




    def on_send_clicked(self):
        user_text = self.input_text.get("1.0", "end").strip()
        if not user_text:
            return

        self.input_text.delete("1.0", "end")

        self._append_user_message(user_text)
        self._set_input_state("disabled")

        # Avvia la chiamata al backend in modo asincrono
        # e passa il chat_id corrente
        asyncio.create_task(self._process_message_async(user_text, self.chat_id))

    async def _process_message_async(self, user_text: str, chat_id: str):
        try:
            reply = await self.core.process_message(user_text, chat_id=chat_id)
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
    2) Avvia eventualmente la REST API (solo in LOCAL di default)
    3) Avvia MCP server (mcp_server.py)
    4) Carica gli Agent da my_agents.xml
    5) Avvia interfaccia grafica di chat
    """
    load_dotenv()

    # --- Modalità LOCAL / ERP ---
    erp_mode = os.getenv("ORDERS_ERP_MODE", "LOCAL").upper()
    if erp_mode not in ("LOCAL", "ERP"):
        logger.warning(
            "ORDERS_ERP_MODE='%s' non valido. Uso 'LOCAL' come default.",
            erp_mode,
        )
        erp_mode = "LOCAL"
    logger.info("Modalità ORDERS_ERP_MODE attiva: %s", erp_mode)

    rest_proc: subprocess.Popen | None = None
    try:
        # --- AVVIO REST API (solo se serve) ---
        rest_cmd_env = os.getenv("ORDERS_REST_COMMAND")

        if erp_mode == "LOCAL":
            # In LOCAL vogliamo SEMPRE una REST (mini-ERP locale)
            if rest_cmd_env:
                # esempio: ORDERS_REST_COMMAND="python -m uvicorn rest_api:app --host 127.0.0.1 --port 8001 --reload"
                rest_cmd = rest_cmd_env.split()
                logger.info(
                    "Modalità LOCAL: avvio REST API con comando da variabile ORDERS_REST_COMMAND: %s",
                    rest_cmd_env,
                )
            else:
                # Comando di default per avviare la REST API locale
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
                logger.info(
                    "Modalità LOCAL: avvio REST API locale con comando di default: %s",
                    " ".join(rest_cmd),
                )

            rest_proc = subprocess.Popen(
                rest_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("REST API avviata con PID=%s", rest_proc.pid)

        else:
            # Modalità ERP
            if rest_cmd_env:
                # Caso avanzato: vuoi comunque avviare un gateway REST custom
                rest_cmd = rest_cmd_env.split()
                logger.info(
                    "Modalità ERP: avvio REST API esterna/gateway con comando da ORDERS_REST_COMMAND: %s",
                    rest_cmd_env,
                )
                rest_proc = subprocess.Popen(
                    rest_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                logger.info("REST API (ERP/gateway) avviata con PID=%s", rest_proc.pid)
            else:
                # Caso standard ERP: niente REST locale, uso solo gli endpoint di my_services_erp.xml
                logger.info(
                    "Modalità ERP ESOLVER"
                    "(uso solo gli endpoint definiti in my_services_erp.xml)."
                )

        # --- AVVIO MCP SERVER ---
        mcp_command = os.getenv("ORDERS_MCP_COMMAND", sys.executable)
        mcp_script = os.getenv("ORDERS_MCP_SCRIPT", "mcp_server.py")

        async with MCPServerStdio(
            name="MCP Server",
            params={
                "command": mcp_command,
                "args": [mcp_script],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=30.0,
        ) as mcp_server:
            logger.info("MCP server avviato, carico gli Agent dal file XML.")

            # --- CREAZIONE AGENT DAL FILE XML ---
            agent_ids = get_available_agent_ids()
            if not agent_ids:
                raise RuntimeError("Nessun <Agent> definito nel file my_agents.xml.")

            logger.info("ID agent disponibili: %s", agent_ids)

            agents: Dict[str, Agent] = {}
            for agent_id in agent_ids:
                agents[agent_id] = create_agent_by_id(agent_id, mcp_server)
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

            core = LocalChat(agents=agents, default_agent_id=default_agent_id)

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
