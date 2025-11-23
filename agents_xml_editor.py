import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import xml.etree.ElementTree as ET

XML_FILE = Path("my_agents.xml")
ALT_XML_FILE = Path("my_agents.xlm")


class AgentsXmlEditor(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Editor my_agents.xml")
        self.geometry("1000x600")

        # Lista agent in memoria: ogni elemento è un dict con chiavi: id, name, description, instructions
        self.agents = []
        self.current_index = None  # indice dell'agente selezionato
        self.dirty = False         # True se ci sono modifiche non salvate

        self._create_widgets()
        self._load_agents()
        self._refresh_tree()

        # Se esiste almeno un agente, seleziona il primo
        if self.agents:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self._load_agent_to_form(0)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===================== UI =====================

    def _create_widgets(self):
        # Ribbon bar
        ribbon = ttk.Frame(self, padding=5, relief="raised")
        ribbon.pack(side=tk.TOP, fill=tk.X)

        btn_varia = ttk.Button(ribbon, text="Varia", command=self._on_varia)
        btn_salva = ttk.Button(ribbon, text="Salva", command=self._on_salva)
        btn_abbandona = ttk.Button(ribbon, text="Abbandona", command=self._on_abbandona)
        btn_inserisci = ttk.Button(ribbon, text="Inserisci Agente", command=self._on_inserisci)
        btn_elimina = ttk.Button(ribbon, text="Elimina Agente", command=self._on_elimina)

        for b in (btn_varia, btn_salva, btn_abbandona, btn_inserisci, btn_elimina):
            b.pack(side=tk.LEFT, padx=5)

        # Corpo: sinistra (griglia), destra (dettaglio)
        body = ttk.Frame(self, padding=5)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Sinistra: elenco agent
        left_frame = ttk.Frame(body)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        lbl_list = ttk.Label(left_frame, text="Elenco agenti")
        lbl_list.pack(side=tk.TOP, anchor="w")

        columns = ("id", "name", "description")
        self.tree = ttk.Treeview(
            left_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=15,
        )
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="Nome")
        self.tree.heading("description", text="Descrizione")

        self.tree.column("id", width=80, anchor="w")
        self.tree.column("name", width=180, anchor="w")
        self.tree.column("description", width=280, anchor="w")

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)

        # Binding: selezione, Invio per "edit", doppio click
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Return>", self._on_tree_edit_request)
        self.tree.bind("<Double-1>", self._on_tree_edit_request)

        # Destra: dettaglio agente
        right_frame = ttk.Frame(body, padding=5, relief="groove")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        ttk.Label(right_frame, text="Dettaglio agente").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))

        ttk.Label(right_frame, text="ID:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.var_id = tk.StringVar()
        self.entry_id = ttk.Entry(right_frame, textvariable=self.var_id)
        self.entry_id.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(right_frame, text="Nome:").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        self.var_name = tk.StringVar()
        self.entry_name = ttk.Entry(right_frame, textvariable=self.var_name)
        self.entry_name.grid(row=2, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(right_frame, text="Descrizione:").grid(row=3, column=0, sticky="ne", padx=5, pady=2)
        self.txt_description = tk.Text(right_frame, height=3, wrap="word")
        self.txt_description.grid(row=3, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(right_frame, text="Instructions:").grid(row=4, column=0, sticky="ne", padx=5, pady=2)
        self.txt_instructions = tk.Text(right_frame, height=15, wrap="word")
        self.txt_instructions.grid(row=4, column=1, sticky="nsew", padx=5, pady=2)

        # Scrollbar per instructions
        instr_scroll = ttk.Scrollbar(right_frame, orient="vertical", command=self.txt_instructions.yview)
        instr_scroll.grid(row=4, column=2, sticky="ns")
        self.txt_instructions.configure(yscrollcommand=instr_scroll.set)

        # Layout weight per allargare bene
        right_frame.columnconfigure(1, weight=1)
        right_frame.rowconfigure(4, weight=1)

    # ===================== CARICAMENTO / SALVATAGGIO XML =====================

    def _load_agents(self):
        """Carica gli agent dal file XML, se presente."""
        path_to_use = None

        if XML_FILE.exists():
            path_to_use = XML_FILE
        elif ALT_XML_FILE.exists():
            path_to_use = ALT_XML_FILE

        if path_to_use is None:
            # Nessun file: lista vuota
            self.agents = []
            self.dirty = False
            return

        try:
            tree = ET.parse(path_to_use)
            root = tree.getroot()
        except Exception as e:
            messagebox.showerror("Errore XML", f"Impossibile leggere {path_to_use}:\n{e}")
            self.agents = []
            self.dirty = False
            return

        agents = []
        for agent_el in root.findall("Agent"):
            agent_id = (agent_el.get("id") or "").strip()
            name = (agent_el.get("name") or "").strip()

            desc_el = agent_el.find("Description")
            instr_el = agent_el.find("Instructions")

            description = (desc_el.text or "") if desc_el is not None else ""
            instructions = (instr_el.text or "") if instr_el is not None else ""

            agents.append(
                {
                    "id": agent_id,
                    "name": name,
                    "description": description,
                    "instructions": instructions,
                }
            )

        self.agents = agents
        self.dirty = False

    def _save_agents(self):
        """Scrive gli agent nel file my_agents.xml."""
        root = ET.Element("Agents")

        for agent in self.agents:
            agent_id = agent.get("id", "").strip()
            name = agent.get("name", "").strip()
            description = agent.get("description", "")
            instructions = agent.get("instructions", "")

            agent_el = ET.SubElement(root, "Agent")
            if agent_id:
                agent_el.set("id", agent_id)
            if name:
                agent_el.set("name", name)

            desc_el = ET.SubElement(agent_el, "Description")
            desc_el.text = description

            instr_el = ET.SubElement(agent_el, "Instructions")
            instr_el.text = instructions

        tree = ET.ElementTree(root)
        try:
            tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
        except Exception as e:
            messagebox.showerror("Errore salvataggio", f"Impossibile scrivere {XML_FILE}:\n{e}")
            return False

        self.dirty = False
        messagebox.showinfo("Salvataggio", f"File salvato correttamente:\n{XML_FILE}")
        return True

    # ===================== GESTIONE TREEVIEW =====================

    def _refresh_tree(self):
        """Aggiorna la griglia degli agent a partire da self.agents."""
        self.tree.delete(*self.tree.get_children())

        for idx, agent in enumerate(self.agents):
            agent_id = agent.get("id", "")
            name = agent.get("name", "")
            desc = (agent.get("description", "") or "").replace("\n", " ")
            if len(desc) > 100:
                desc = desc[:97] + "..."
            self.tree.insert("", "end", iid=str(idx), values=(agent_id, name, desc))

    def _on_tree_select(self, event=None):
        """Quando seleziono una riga della griglia."""
        sel = self.tree.selection()
        if not sel:
            self.current_index = None
            return

        idx = int(sel[0])
        self.current_index = idx
        self._load_agent_to_form(idx)

    def _on_tree_edit_request(self, event=None):
        """
        Richiesta di "configurare" un agente (Enter o doppio click).
        Di fatto, carica i dati nel pannello a destra (già fatto su select),
        quindi qui ci limitiamo a assicurare il focus.
        """
        if self.current_index is None:
            return
        self.entry_id.focus_set()

    # ===================== FORM DETTAGLIO =====================

    def _clear_form(self):
        self.var_id.set("")
        self.var_name.set("")
        self.txt_description.delete("1.0", tk.END)
        self.txt_instructions.delete("1.0", tk.END)

    def _load_agent_to_form(self, index: int):
        """Carica i dati dell'agente indicato nell'area di dettaglio."""
        if index < 0 or index >= len(self.agents):
            self._clear_form()
            return

        agent = self.agents[index]
        self.var_id.set(agent.get("id", ""))
        self.var_name.set(agent.get("name", ""))
        self.txt_description.delete("1.0", tk.END)
        self.txt_description.insert("1.0", agent.get("description", "") or "")
        self.txt_instructions.delete("1.0", tk.END)
        self.txt_instructions.insert("1.0", agent.get("instructions", "") or "")

    def _apply_form_to_current_agent(self):
        """Scrive i dati del form nell'agente selezionato."""
        if self.current_index is None:
            messagebox.showwarning("Nessun agente selezionato", "Seleziona un agente dalla griglia.")
            return False

        if self.current_index < 0 or self.current_index >= len(self.agents):
            return False

        agent = self.agents[self.current_index]
        agent["id"] = self.var_id.get().strip()
        agent["name"] = self.var_name.get().strip()
        agent["description"] = self.txt_description.get("1.0", tk.END).rstrip("\n")
        agent["instructions"] = self.txt_instructions.get("1.0", tk.END).rstrip("\n")

        self.dirty = True
        self._refresh_tree()
        # Reseleziona la riga
        self.tree.selection_set(str(self.current_index))
        self.tree.focus(str(self.current_index))
        return True

    # ===================== AZIONI RIBBON =====================

    def _on_varia(self):
        """Pulsante 'Varia': applica modifiche dell'area di dettaglio all'agente selezionato."""
        self._apply_form_to_current_agent()

    def _on_salva(self):
        """Pulsante 'Salva': scrive my_agents.xml."""
        # Prima sincronizza eventuali modifiche nel form sull'agente selezionato
        if self.current_index is not None:
            self._apply_form_to_current_agent()
        self._save_agents()

    def _on_abbandona(self):
        """Pulsante 'Abbandona': chiude il programma (chiede conferma se ci sono modifiche)."""
        self._on_close()

    def _on_inserisci(self):
        """Pulsante 'Inserisci Agente': crea un nuovo agente vuoto."""
        new_agent = {
            "id": "",
            "name": "",
            "description": "",
            "instructions": "",
        }
        self.agents.append(new_agent)
        self.dirty = True
        self._refresh_tree()

        new_index = len(self.agents) - 1
        self.current_index = new_index
        self.tree.selection_set(str(new_index))
        self.tree.focus(str(new_index))
        self._load_agent_to_form(new_index)

    def _on_elimina(self):
        """Pulsante 'Elimina Agente': elimina l'agente selezionato."""
        if self.current_index is None:
            messagebox.showwarning("Nessun agente selezionato", "Seleziona un agente da eliminare.")
            return

        if self.current_index < 0 or self.current_index >= len(self.agents):
            return

        agent = self.agents[self.current_index]
        name = agent.get("name") or agent.get("id") or "(senza nome)"

        if not messagebox.askyesno("Conferma eliminazione", f"Vuoi eliminare l'agente:\n{name}?"):
            return

        del self.agents[self.current_index]
        self.dirty = True
        self.current_index = None
        self._clear_form()
        self._refresh_tree()

    # ===================== CHIUSURA =====================

    def _on_close(self):
        """Gestisce la chiusura della finestra."""
        if self.dirty:
            res = messagebox.askyesnocancel(
                "Modifiche non salvate",
                "Ci sono modifiche non salvate.\nVuoi salvare prima di uscire?"
            )
            if res is None:
                # Annulla
                return
            elif res:
                # Salva e poi esci
                if not self._save_agents():
                    return  # se salvataggio fallisce, non chiudere
        self.destroy()


def main():
    app = AgentsXmlEditor()
    app.mainloop()


if __name__ == "__main__":
    main()
