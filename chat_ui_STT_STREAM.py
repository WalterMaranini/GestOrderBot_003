import tkinter as tk
from tkinter import ttk
from datetime import datetime
import threading
import queue
import sys

import os
import tempfile
import wave
import base64
import time

from openai import OpenAI

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


# ---------------- Speech-to-Text (microfono -> testo) ----------------

class MicrophoneRecorder:
    def __init__(self, samplerate: int = 16000, channels: int = 1, dtype: str = "int16") -> None:
        if sd is None or np is None:
            raise RuntimeError("Dipendenze mancanti: installa 'sounddevice' e 'numpy'.")
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self._frames = []
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._frames.clear()

        def _callback(indata, frames, time, status):  # noqa: ANN001
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype=self.dtype,
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

        fd, path = tempfile.mkstemp(prefix="ordersbot_stt_", suffix=".wav")
        os.close(fd)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.samplerate)
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
    """
    Streaming STT su WebSocket (Realtime API).
    - Invia PCM16 base64 con input_audio_buffer.append
    - Fa commit a intervalli regolari (chunked) per ottenere trascrizioni "quasi realtime"
    """

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
        self.on_event = on_event  # callable(kind:str, payload:str)

        self._ws = None
        self._ws_thread = None
        self._sender_thread = None
        self._stop_flag = threading.Event()
        self._connected = threading.Event()

        self._mic: MicrophoneStreamer | None = None

        # Stato trascrizione
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
            # Config sessione trascrizione
            cfg = {
                "type": "transcription_session.update",
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": self.model,
                    "prompt": "",
                    "language": self.language or "",
                },
                # turn_detection = null -> gestiamo noi i commit (più "streaming")
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
            # Alla chiusura emettiamo comunque il testo finale assemblato
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

        self._ws_thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._ws_thread.start()

        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()

    # ---------------- internals ----------------

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
        # Attendi connessione
        self._connected.wait(timeout=5.0)
        if not self._connected.is_set() or self._ws is None or self._mic is None:
            self._emit("bot", "❌ Streaming STT: connessione non riuscita.")
            return

        last_commit = time.time()

        bytes_per_ms = int(24000 * 2 / 1000)  # PCM16 mono @24kHz => 48 bytes/ms
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

        # Flush finale: commit se c'è abbastanza audio
        try:
            if self._bytes_since_commit >= min_commit_bytes:
                self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            pass

        # Piccolo grace per ricevere gli ultimi eventi
        time.sleep(0.8)

        try:
            self._ws.close()
        except Exception:
            pass


class ChatWindow(tk.Tk):
    """
    Finestra di chat locale che potrà essere collegata al tuo chatbot.
    Per ora usa una funzione stub `call_chatbot_backend`, che dovrai
    sostituire con la chiamata reale al tuo motore (Agents/MCP/etc.).
    """

    def __init__(self):
        super().__init__()

        self.title("OrdersBot - Chat locale")
        self.geometry("800x600")

        # Coda per ricevere le risposte dal thread di backend
        self.response_queue = queue.Queue()

        # --- Voice dictation (Speech-to-Text) ---
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

        self.voice_recorder = None
        if sd is not None and np is not None:
            try:
                self.voice_recorder = MicrophoneRecorder()
            except Exception:
                self.voice_recorder = None

        self.streaming_available = (self.voice_recorder is not None and websocket is not None)
        # --- Layout principale ---
        self._create_widgets()
        self._configure_grid()

        # Polling della coda risposte ogni 100ms
        self.after(100, self._poll_response_queue)

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------
    def _create_widgets(self):
        # Frame principale
        main_frame = ttk.Frame(self)
        main_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # Area chat (testo + scrollbar)
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

        # Tag di stile per differenziare utente/bot
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

        # 🎤 Push-to-talk: tieni premuto per registrare, rilascia per trascrivere
        self.mic_button = ttk.Button(input_frame, text="🎤")
        self.mic_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.mic_button.bind("<ButtonPress-1>", self._on_mic_press)
        self.mic_button.bind("<ButtonRelease-1>", self._on_mic_release)
        if self.voice_recorder is None:
            self.mic_button.state(["disabled"])

        stream_cb = ttk.Checkbutton(input_frame, text="Streaming", variable=self.streaming_var)
        stream_cb.grid(row=0, column=2, sticky="e", padx=(8, 0))
        if not getattr(self, "streaming_available", False):
            stream_cb.state(["disabled"])
            self.streaming_var.set(False)

        autosend_cb = ttk.Checkbutton(input_frame, text="Auto-invia", variable=self.autosend_var)
        autosend_cb.grid(row=0, column=3, sticky="e", padx=(8, 0))

        send_button = ttk.Button(input_frame, text="Invia", command=self.on_send_clicked)
        send_button.grid(row=0, column=4, sticky="e", padx=(8, 0))

        # Binding tasto Invio (Invio = invia, Shift+Invio = a capo)
        self.input_text.bind("<Return>", self._on_enter)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter)

        # Messaggio iniziale
        self._append_system_message("Chat locale pronta. Scrivi un messaggio per interagire con il chatbot.")

    def _configure_grid(self):
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        main_frame = self.children[list(self.children.keys())[0]]
        main_frame.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)

        chat_frame = main_frame.children[list(main_frame.children.keys())[0]]
        chat_frame.rowconfigure(0, weight=1)
        chat_frame.columnconfigure(0, weight=1)

        input_frame = main_frame.children[list(main_frame.children.keys())[1]]
        input_frame.columnconfigure(0, weight=1)

    # -------------------------------------------------------------------------
    # Gestione input utente
    # -------------------------------------------------------------------------
    def _on_enter(self, event):
        self.on_send_clicked()
        return "break"  # evita l'andare a capo

    def _on_shift_enter(self, event):
        # Permette di andare a capo nel box di input
        self.input_text.insert("insert", "\n")
        return "break"

    # ---------------- Speech-to-Text (dettatura) ----------------

    def _on_mic_press(self, event=None):
        self._mic_active = False
        self._mic_mode = None

        if self.voice_recorder is None:
            self._append_system_message(
                "🎤 Dittatura non disponibile su questa macchina.\\nInstalla le dipendenze: pip install sounddevice numpy")
            return

        use_streaming = bool(self.streaming_var.get()) and getattr(self, "streaming_available", False)

        if use_streaming:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                self._append_system_message("❌ OPENAI_API_KEY non impostata. Impossibile usare lo streaming STT.")
                return

            try:
                if self._streaming_mic is None:
                    self._streaming_mic = MicrophoneStreamer(samplerate=24000)
                else:
                    while not self._streaming_mic.audio_queue.empty():
                        self._streaming_mic.audio_queue.get_nowait()

                self._input_before_dictation = self.input_text.get("1.0", "end-1c")
                self._live_transcript = ""

                self._streaming_client = RealtimeTranscriber(
                    api_key=api_key,
                    model=self.stt_model,
                    language=self.streaming_language,
                    commit_interval_s=self.streaming_commit_interval,
                    on_event=lambda k, p: self.response_queue.put((k, p)),
                )

                self._streaming_mic.start()
                self._streaming_client.start(self._streaming_mic)

                self._mic_active = True
                self._mic_mode = "stream"
                self._append_system_message("🎙️ Streaming attivo... (parla: il testo apparirà quasi in tempo reale)")
            except Exception as e:
                try:
                    if self._streaming_mic is not None:
                        self._streaming_mic.stop()
                except Exception:
                    pass
                try:
                    if self._streaming_client is not None:
                        self._streaming_client.stop()
                except Exception:
                    pass
                self._streaming_client = None
                self._append_system_message(f"❌ Errore avvio streaming STT: {e}")
            return

        try:
            self.voice_recorder.start()
            self._mic_active = True
            self._mic_mode = "wav"
            self._append_system_message("🎙️ Registrazione... (rilascia il tasto per trascrivere)")
        except Exception as e:
            self._append_system_message(f"❌ Errore avvio microfono: {e}")

    def _on_mic_release(self, event=None):
        if not getattr(self, "_mic_active", False):
            return

        mode = getattr(self, "_mic_mode", None)
        self._mic_active = False
        self._mic_mode = None

        if mode == "stream":
            try:
                if self._streaming_mic is not None:
                    self._streaming_mic.stop()
            except Exception:
                pass
            try:
                if self._streaming_client is not None:
                    self._streaming_client.stop()
            except Exception:
                pass
            self._append_system_message("⏹️ Fine dettatura (finalizzo la trascrizione)...")
            return

        if self.voice_recorder is None:
            return

        try:
            wav_path = self.voice_recorder.stop_and_save_wav()
            if not wav_path:
                return
        except Exception as e:
            self._append_system_message(f"❌ Errore stop microfono: {e}")
            return

        self._append_system_message("📝 Trascrivo la dettatura...")
        threading.Thread(target=self._stt_worker, args=(wav_path,), daemon=True).start()

    def _stt_worker(self, wav_path: str):
        try:
            text = self._transcribe_file(wav_path)
            self.response_queue.put(("stt", text))
        except Exception as e:
            self.response_queue.put(("bot", f"❌ Errore trascrizione: {e}"))
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass

    def _apply_transcript(self, text: str):
        if not text:
            self._append_system_message("⚠️ Nessun testo rilevato dalla dettatura.")
            return

        if str(self.input_text.cget("state")) != "normal":
            self._pending_transcript = (self._pending_transcript + " " + text).strip()
            self._append_system_message("✅ Trascrizione pronta: verrà inserita appena l'input torna disponibile.")
            return

        self.input_text.insert("end", text + " ")
        self.input_text.focus_set()

        if bool(self.autosend_var.get()):
            self.on_send_clicked()

    def _apply_live_transcript(self, text: str, final: bool) -> None:
        # Aggiorna l'input in tempo quasi reale durante la dettatura streaming.
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

    def on_send_clicked(self):
        user_text = self.input_text.get("1.0", "end").strip()
        if not user_text:
            return

        # Pulisci input
        self.input_text.delete("1.0", "end")

        # Mostra il messaggio dell'utente
        self._append_user_message(user_text)

        # Disabilita temporaneamente l'input finché non arriva la risposta
        self._set_input_state("disabled")

        # Lancia la chiamata al backend in un thread separato
        threading.Thread(
            target=self._backend_worker,
            args=(user_text,),
            daemon=True,
        ).start()

    # -------------------------------------------------------------------------
    # Backend (punto da collegare al tuo chatbot)
    # -------------------------------------------------------------------------
    def _backend_worker(self, user_text: str):
        """
        Esegue la chiamata al "motore" del chatbot in un thread separato,
        così da non bloccare la GUI.
        """
        try:
            response_text = call_chatbot_backend(user_text)
        except Exception as e:
            response_text = f"[ERRORE BACKEND] {e}"
            print("Errore nel backend:", e, file=sys.stderr)

        # Metti la risposta nella coda, sarà letta dal thread principale (GUI)
        self.response_queue.put(("bot", response_text))

    def _poll_response_queue(self):
        """
        Legge periodicamente la coda per verificare se è arrivata
        una risposta dal backend.
        """
        try:
            while True:
                item = self.response_queue.get_nowait()

                # item può essere:
                #   ("bot", testo)  -> risposta backend
                #   ("stt", testo)  -> trascrizione dettatura
                if isinstance(item, tuple) and len(item) == 2:
                    kind, payload = item
                else:
                    kind, payload = "bot", item

                if kind == "stt":
                    self._apply_transcript(str(payload))
                elif kind == "stt_live":
                    self._apply_live_transcript(str(payload), final=False)
                elif kind == "stt_final":
                    self._apply_live_transcript(str(payload), final=True)
                else:
                    self._append_bot_message(str(payload))
                    self._set_input_state("normal")
        except queue.Empty:
            pass

        # Ripeti tra 100ms
        self.after(100, self._poll_response_queue)

    # -------------------------------------------------------------------------
    # Append messaggi in chat
    # -------------------------------------------------------------------------
    def _append_user_message(self, text: str):
        self._append_message(sender="Tu", text=text, tag="user")

    def _append_bot_message(self, text: str):
        self._append_message(sender="Bot", text=text, tag="bot")

    def _append_system_message(self, text: str):
        self._append_message(sender="Sistema", text=text, tag="bot")

    def _append_message(self, sender: str, text: str, tag: str):
        self.chat_text.config(state="normal")

        timestamp = datetime.now().strftime("%H:%M:%S")

        # Intestazione (mittente + ora)
        self.chat_text.insert("end", f"{sender} ", (tag,))
        self.chat_text.insert("end", f"[{timestamp}]\n", ("time",))

        # Corpo del messaggio
        self.chat_text.insert("end", text + "\n\n", ("body",))

        self.chat_text.config(state="disabled")
        self.chat_text.see("end")  # scroll in basso

    def _set_input_state(self, state: str):
        self.input_text.config(state=state)
        if state == "normal":
            if getattr(self, "_pending_transcript", ""):
                self.input_text.insert("end", self._pending_transcript + " ")
                self._pending_transcript = ""
            self.input_text.focus_set()


# -------------------------------------------------------------------------
# PUNTO DI INTEGRAZIONE COL TUO CHATBOT
# -------------------------------------------------------------------------
def call_chatbot_backend(user_text: str) -> str:
    """
    QUI devi integrare la chiamata al tuo chatbot reale.

    Per ora è un semplice echo di test. Sostituiscilo con:
      - una chiamata a una funzione del tuo core (es. handle_message(...))
      - oppure una chiamata HTTP a un endpoint FastAPI /chat
      - oppure l'uso diretto degli Agent creati da my_agents.py
    """

    # ESEMPIO PLACEHOLDER (da cambiare)
    simulated_response = f"(DEMO) Hai scritto: {user_text}"
    return simulated_response


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------
if __name__ == "__main__":
    app = ChatWindow()
    app.mainloop()
