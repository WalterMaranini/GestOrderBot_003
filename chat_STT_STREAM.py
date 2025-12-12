import os
import sys
import asyncio
import logging
import subprocess
from typing import Dict, Any
from datetime import datetime
import json

import tempfile
import wave
import base64
import time
import threading
from dataclasses import dataclass

# --- Optional deps for microphone capture (install: pip install sounddevice numpy) ---
try:
    import numpy as np  # type: ignore
except Exception:
    np = None  # type: ignore

try:
    import sounddevice as sd  # type: ignore
except Exception:
    sd = None  # type: ignore

# --- Optional dep for realtime STT (install: pip install websocket-client) ---
try:
    import websocket  # type: ignore
except Exception:
    websocket = None  # type: ignore

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
                model="gpt-4.1-mini",  # modello leggero per routing
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


# ================== SPEECH-TO-TEXT (microfono -> testo) ==================

@dataclass
class AudioConfig:
    samplerate: int = 16000
    channels: int = 1
    dtype: str = "int16"


class MicrophoneRecorder:
    """Registratore semplice 'push-to-talk' basato su sounddevice.

    - start(): avvia la cattura dal microfono
    - stop_and_save_wav(): ferma la cattura e salva un WAV temporaneo (path restituito)

    NOTE:
      - richiede `sounddevice` + `numpy`
      - su Windows potrebbe essere necessario installare PortAudio (sounddevice lo segnala).
    """

    def __init__(self, cfg: AudioConfig | None = None) -> None:
        if sd is None or np is None:
            raise RuntimeError("Dipendenze mancanti: installa 'sounddevice' e 'numpy'.")
        self.cfg = cfg or AudioConfig()
        self._frames: list["np.ndarray"] = []
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return

        self._frames.clear()

        def _callback(indata, frames, time, status):  # noqa: ANN001
            if status:
                # Non interrompo: log a console
                logger.warning("Audio status: %s", status)
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.cfg.samplerate,
            channels=self.cfg.channels,
            dtype=self.cfg.dtype,
            callback=_callback,
        )
        self._stream.start()

    def stop_and_save_wav(self) -> str | None:
        if self._stream is None:
            return None

        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

        if not self._frames:
            return None

        audio = np.concatenate(self._frames, axis=0)

        # Salvo su file temporaneo (WAV PCM16)
        fd, path = tempfile.mkstemp(prefix="esolver_stt_", suffix=".wav")
        os.close(fd)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(self.cfg.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.cfg.samplerate)
            wf.writeframes(audio.tobytes())

        return path


# ---------------- Realtime streaming Speech-to-Text (WebSocket) ----------------

class MicrophoneStreamer:
    """Cattura audio PCM16 mono e lo espone come stream di bytes."""

    def __init__(self, samplerate: int = 24000, channels: int = 1, dtype: str = "int16", block_ms: int = 50) -> None:
        if sd is None or np is None:
            raise RuntimeError("Dipendenze mancanti: installa 'sounddevice' e 'numpy'.")
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.block_ms = block_ms
        self.audio_queue: "queue.Queue[bytes]" = queue.Queue()
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        blocksize = int(self.samplerate * (self.block_ms / 1000.0))

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            try:
                self.audio_queue.put(indata.copy().tobytes())
            except Exception:
                pass

        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype=self.dtype,
            blocksize=blocksize,
            callback=_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None


class RealtimeTranscriber:
    """Streaming STT su WebSocket (Realtime API)."""

    WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"

    def __init__(
            self,
            *,
            api_key: str,
            model: str = "gpt-4o-mini-transcribe",
            language: str = "it",
            commit_interval_s: float = 0.8,
            min_commit_ms: int = 250,
            on_event=None,
    ) -> None:
        if websocket is None:
            raise RuntimeError("Dipendenza mancante: installa 'websocket-client'.")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.commit_interval_s = commit_interval_s
        self.min_commit_ms = min_commit_ms
        self.on_event = on_event

        self._ws = None
        self._stop_flag = threading.Event()
        self._connected = threading.Event()
        self._mic: MicrophoneStreamer | None = None

        self._item_order: list[str] = []
        self._item_text: dict[str, str] = {}
        self._bytes_since_commit = 0

    def start(self, mic: MicrophoneStreamer) -> None:
        self._mic = mic
        self._stop_flag.clear()
        self._connected.clear()

        headers = [f"Authorization: Bearer {self.api_key}"]

        def _on_open(ws):
            self._connected.set()
            cfg = {
                "type": "transcription_session.update",
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": self.model,
                    "prompt": "",
                    "language": self.language or "",
                },
                "turn_detection": None,
                "input_audio_noise_reduction": {"type": "near_field"},
            }
            try:
                ws.send(json.dumps(cfg))
            except Exception as e:
                self._emit("bot", f"❌ Errore configurazione streaming STT: {e}")

        def _on_message(ws, message):
            try:
                data = json.loads(message)
            except Exception:
                return
            t = data.get("type")
            if t == "input_audio_buffer.committed":
                item_id = data.get("item_id")
                if item_id:
                    self._item_order.append(item_id)
                    self._item_text.setdefault(item_id, "")
            elif t == "conversation.item.input_audio_transcription.delta":
                item_id = data.get("item_id")
                delta = data.get("delta", "")
                if item_id and isinstance(delta, str):
                    self._item_text[item_id] = (self._item_text.get(item_id, "") + delta)
                    self._emit_live()
            elif t == "conversation.item.input_audio_transcription.completed":
                item_id = data.get("item_id")
                tr = data.get("transcript", "")
                if item_id and isinstance(tr, str):
                    self._item_text[item_id] = tr
                    self._emit_live()
            elif t == "conversation.item.input_audio_transcription.failed":
                self._emit("bot", f"❌ Trascrizione fallita: {data}")
            elif t == "error":
                self._emit("bot", f"❌ Realtime error: {data}")

        def _on_error(ws, error):
            self._emit("bot", f"❌ Errore WebSocket STT: {error}")

        def _on_close(ws, status_code, msg):
            final = self._assemble_text().strip()
            if final:
                self._emit("stt_final", final)

        self._ws = websocket.WebSocketApp(
            self.WS_URL,
            header=headers,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        threading.Thread(target=self._ws.run_forever, daemon=True).start()
        threading.Thread(target=self._sender_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop_flag.set()

    def _emit(self, kind: str, payload: str) -> None:
        if callable(self.on_event):
            try:
                self.on_event(kind, payload)
            except Exception:
                pass

    def _assemble_text(self) -> str:
        parts = []
        for item_id in self._item_order:
            s = (self._item_text.get(item_id) or "").strip()
            if s:
                parts.append(s)
        return " ".join(parts)

    def _emit_live(self) -> None:
        self._emit("stt_live", self._assemble_text())

    def _sender_loop(self) -> None:
        self._connected.wait(timeout=5.0)
        if not self._connected.is_set() or self._ws is None or self._mic is None:
            self._emit("bot", "❌ Streaming STT: connessione non riuscita.")
            return

        last_commit = time.time()
        bytes_per_ms = int(24000 * 2 / 1000)
        min_commit_bytes = bytes_per_ms * max(100, int(self.min_commit_ms))

        while not self._stop_flag.is_set():
            try:
                chunk = self._mic.audio_queue.get(timeout=0.2)
            except Exception:
                chunk = b""

            if chunk:
                try:
                    b64 = base64.b64encode(chunk).decode("ascii")
                    self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                    self._bytes_since_commit += len(chunk)
                except Exception as e:
                    self._emit("bot", f"❌ Errore invio audio: {e}")
                    break

            now = time.time()
            if (now - last_commit) >= self.commit_interval_s and self._bytes_since_commit >= min_commit_bytes:
                try:
                    self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    self._bytes_since_commit = 0
                except Exception:
                    pass
                last_commit = now

        try:
            if self._bytes_since_commit >= min_commit_bytes:
                self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            pass

        time.sleep(0.8)
        try:
            self._ws.close()
        except Exception:
            pass


# ================== INTERFACCIA GRAFICA (Tkinter) ==================

class ChatWindow(tk.Tk):
    """
    Finestra di chat locale che usa LocalChat come backend.
    """

    def __init__(self, core: LocalChat):
        super().__init__()

        self.core = core

        # --- Voice dictation (Speech-to-Text) ---
        # Modello STT configurabile via env var STT_MODEL (default: gpt-4o-mini-transcribe)
        self.stt_client = OpenAI()
        self.stt_model = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
        self.autosend_var = tk.BooleanVar(value=False)
        self._pending_transcript: str = ""

        # --- Streaming STT (Realtime WebSocket) ---
        self.streaming_var = tk.BooleanVar(value=True)
        self.streaming_language = os.getenv("STT_LANGUAGE", "it")
        self.streaming_commit_interval = float(os.getenv("STT_COMMIT_INTERVAL", "0.8"))
        self._streaming_mic: MicrophoneStreamer | None = None
        self._streaming_client: RealtimeTranscriber | None = None
        self._input_before_dictation: str = ""
        self._live_transcript: str = ""

        # Se sounddevice/numpy non sono installati, la dettatura viene disabilitata.
        self.voice_recorder = None
        if sd is not None and np is not None:
            try:
                self.voice_recorder = MicrophoneRecorder()
            except Exception:
                logger.exception("Impossibile inizializzare MicrophoneRecorder")

        self.streaming_available = (self.voice_recorder is not None and websocket is not None)
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

        # 🎤 Push-to-talk: tieni premuto per registrare, rilascia per trascrivere
        self.mic_button = ttk.Button(input_frame, text="🎤")
        self.mic_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.mic_button.bind("<ButtonPress-1>", self._on_mic_press)
        self.mic_button.bind("<ButtonRelease-1>", self._on_mic_release)
        if self.voice_recorder is None:
            # sounddevice/numpy non presenti -> disabilito la dettatura
            self.mic_button.state(["disabled"])

        stream_cb = ttk.Checkbutton(input_frame, text="Streaming", variable=self.streaming_var)
        stream_cb.grid(row=0, column=3, sticky="e", padx=(8, 0))
        if not getattr(self, "streaming_available", False):
            stream_cb.state(["disabled"])
            self.streaming_var.set(False)

        autosend_cb = ttk.Checkbutton(input_frame, text="Auto-invia", variable=self.autosend_var)
        autosend_cb.grid(row=0, column=4, sticky="e", padx=(8, 0))

        # Bottone per azzerare la sessione e ripartire da zero
        reset_button = ttk.Button(
            input_frame,
            text="Nuova sessione",
            command=self.on_reset_session,
        )
        reset_button.grid(row=0, column=5, sticky="e", padx=(8, 0))
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

    # ---------------- Speech-to-Text (dettatura) ----------------

    def _on_mic_press(self, event=None):
        if self.voice_recorder is None:
            self._append_system_message(
                "🎤 Dittatura non disponibile su questa macchina.\n"
                "Installa le dipendenze: pip install sounddevice numpy"
            )
            return

        if bool(self.streaming_var.get()) and getattr(self, "streaming_available", False):
            try:
                if self._streaming_mic is None:
                    self._streaming_mic = MicrophoneStreamer(samplerate=24000)
                else:
                    while not self._streaming_mic.audio_queue.empty():
                        self._streaming_mic.audio_queue.get_nowait()

                api_key = os.getenv("OPENAI_API_KEY", "").strip()
                if not api_key:
                    self._append_system_message("❌ OPENAI_API_KEY non impostata. Impossibile usare lo streaming STT.")
                    return

                self._input_before_dictation = self.input_text.get("1.0", "end-1c")
                self._live_transcript = ""

                def _on_evt(kind: str, payload: str) -> None:
                    # callback da thread -> torniamo sul thread Tk
                    if kind == "stt_live":
                        self.after(0, lambda: self._apply_live_transcript(payload, final=False))
                    elif kind == "stt_final":
                        self.after(0, lambda: self._apply_live_transcript(payload, final=True))
                    else:
                        self.after(0, lambda: self._append_system_message(str(payload)))

                self._streaming_client = RealtimeTranscriber(
                    api_key=api_key,
                    model=self.stt_model,
                    language=self.streaming_language,
                    commit_interval_s=self.streaming_commit_interval,
                    on_event=_on_evt,
                )

                self._streaming_mic.start()
                self._streaming_client.start(self._streaming_mic)

                self._append_system_message("🎙️ Streaming attivo... (parla: il testo apparirà quasi in tempo reale)")
            except Exception as e:
                self._append_system_message(f"❌ Errore avvio streaming STT: {e}")
            return

        try:
            self.voice_recorder.start()
            self._append_system_message("🎙️ Registrazione... (tieni premuto il microfono e parla)")
        except Exception as e:
            logger.exception("Errore avvio registrazione audio")
            self._append_system_message(f"❌ Errore avvio microfono: {e}")

    def _on_mic_release(self, event=None):
        # Streaming: stop e flush
        if self._streaming_client is not None and self._streaming_mic is not None:
            try:
                self._streaming_mic.stop()
            except Exception:
                pass
            try:
                self._streaming_client.stop()
            except Exception:
                pass
            self._append_system_message("⏹️ Fine dettatura (finalizzo la trascrizione)...")
            return

        # Fallback: trascrizione da file
        if self.voice_recorder is None:
            return

        try:
            wav_path = self.voice_recorder.stop_and_save_wav()
            if not wav_path:
                # Release arrivato ma nessuna registrazione valida
                return
        except Exception as e:
            logger.exception("Errore stop registrazione audio")
            self._append_system_message(f"❌ Errore stop microfono: {e}")
            return

        self._append_system_message("📝 Trascrivo la dettatura...")

        # Non bloccare GUI: faccio la chiamata STT in async
        asyncio.create_task(self._transcribe_and_fill_async(wav_path))

    def _apply_live_transcript(self, text: str, final: bool) -> None:
        if not text:
            if final:
                self._append_system_message("⚠️ Nessun testo rilevato dalla dettatura.")
                self._streaming_client = None
                self._streaming_mic = None
            return

        if str(self.input_text.cget("state")) != "normal":
            if final:
                self._pending_transcript = (self._pending_transcript + " " + text).strip()
                self._append_system_message("✅ Trascrizione pronta: verrà inserita appena l'input torna disponibile.")
                self._streaming_client = None
                self._streaming_mic = None
            return

        self._live_transcript = text
        composed = (self._input_before_dictation + " " + self._live_transcript).strip()

        self.input_text.delete("1.0", "end")
        self.input_text.insert("end", composed + " ")
        self.input_text.focus_set()

        if final:
            self._streaming_client = None
            self._streaming_mic = None
            if bool(self.autosend_var.get()):
                self.on_send_clicked()

    def _transcribe_file(self, wav_path: str) -> str:
        with open(wav_path, "rb") as audio_file:
            tr = self.stt_client.audio.transcriptions.create(
                model=self.stt_model,
                file=audio_file,
            )
        text = getattr(tr, "text", None)
        if not text and isinstance(tr, dict):
            text = tr.get("text")
        return (text or "").strip()

    async def _transcribe_and_fill_async(self, wav_path: str):
        if not wav_path:
            return
        try:
            text = await asyncio.to_thread(self._transcribe_file, wav_path)
        except Exception as e:
            logger.exception("Errore durante STT")
            self._append_system_message(f"❌ Errore trascrizione: {e}")
            text = ""
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass

        if not text:
            self._append_system_message("⚠️ Nessun testo rilevato dalla dettatura.")
            return

        # Se l'input è disabilitato (in attesa della risposta), accodo la trascrizione
        if str(self.input_text.cget("state")) != "normal":
            self._pending_transcript = (self._pending_transcript + " " + text).strip()
            self._append_system_message("✅ Trascrizione pronta: verrà inserita appena l'input torna disponibile.")
            return

        self.input_text.insert("end", text + " ")
        self.input_text.focus_set()

        if bool(self.autosend_var.get()):
            self.on_send_clicked()

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
            if getattr(self, "_pending_transcript", ""):
                self.input_text.insert("end", self._pending_transcript + " ")
                self._pending_transcript = ""
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
