import tkinter as tk
from tkinter import ttk
from datetime import datetime
import threading
import queue
import sys


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

        send_button = ttk.Button(input_frame, text="Invia", command=self.on_send_clicked)
        send_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

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
        self.response_queue.put(response_text)

    def _poll_response_queue(self):
        """
        Legge periodicamente la coda per verificare se è arrivata
        una risposta dal backend.
        """
        try:
            while True:
                response_text = self.response_queue.get_nowait()
                self._append_bot_message(response_text)
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
